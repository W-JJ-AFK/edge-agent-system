"""设备状态缓存：遥测历史、告警去重、在线判定。

设计要点：
1. **字段级更新**：遥测只覆盖 last_data / last_seen / seq，不重建整个记录，
   否则会把 last_alert 冲掉（这正是旧版本的 bug）。
2. **告警幂等**：按 alert_id 去重，因为 QoS1 会重复投递，且设备在故障持续期间会重发。
3. **在线判定**：优先看 status 主题（LWT 遗嘱），其次看 last_seen 是否超时。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_TTL_S = 6.0
HISTORY_SIZE = 300
ALERT_HISTORY_SIZE = 300
ALERT_DEDUPE_TTL_S = 300.0
#: alert_id 去重表的硬上限。必须同时有"按时间清"和"按数量淘汰"，
#: 否则告警风暴期间 512 条都在窗口内，时间清理一条也删不掉。
ALERT_ID_MAX = 512


@dataclass
class DeviceState:
    device_id: str
    device_type: str = "unknown"
    last_data: dict[str, Any] = field(default_factory=dict)
    last_seen: float = 0.0
    seq: int | None = None
    status: str = "unknown"  # online | offline | unknown
    status_ts: float = 0.0
    last_alert: dict[str, Any] | None = None
    telemetry_count: int = 0


@dataclass(frozen=True)
class Trend:
    metric: str
    samples: int
    mean: float
    minimum: float
    maximum: float
    slope_per_s: float
    direction: str

    def as_text(self) -> str:
        return (
            f"{self.metric}: 均值 {self.mean:.3f}, 范围 {self.minimum:.3f}~{self.maximum:.3f}, "
            f"趋势 {self.direction} ({self.slope_per_s:+.4f}/s, {self.samples} 点)"
        )


class DeviceCache:
    """线程安全的内存缓存。"""

    def __init__(
        self,
        ttl_by_device: dict[str, float] | None = None,
        default_ttl_s: float = DEFAULT_TTL_S,
        history_size: int = HISTORY_SIZE,
        type_dedupe_window_s: float = 30.0,
    ) -> None:
        self._lock = threading.RLock()
        self._devices: dict[str, DeviceState] = {}
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._alerts: deque[dict[str, Any]] = deque(maxlen=ALERT_HISTORY_SIZE)
        self._alert_ids: dict[str, float] = {}
        self._type_ts: dict[tuple[str, str], float] = {}
        self._ttl_by_device = dict(ttl_by_device or {})
        self._default_ttl = default_ttl_s
        self._type_dedupe_window = type_dedupe_window_s

    # ---------------- 写入 ----------------

    def update_telemetry(
        self,
        device_id: str,
        device_type: str,
        data: dict[str, Any],
        *,
        ts: float | None = None,
        seq: int | None = None,
    ) -> DeviceState:
        now = ts if ts is not None else time.time()
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                state = DeviceState(device_id=device_id, device_type=device_type)
                self._devices[device_id] = state

            # 字段级更新：保留 last_alert / status
            state.device_type = device_type or state.device_type
            state.last_data = dict(data)
            state.last_seen = now
            state.seq = seq
            state.telemetry_count += 1
            # 收到遥测就是"活着"的最硬证据。
            # 这里必须把 offline 翻回 online：mark_offline() 是按 last_seen 超时
            # 打的离线标记，而 is_online() 一看到 status=="offline" 就直接返回 False。
            # 早期实现只更新 last_seen 不动 status，于是设备"掉线 6 秒再恢复"之后
            # 会永久显示离线（数据在刷新、状态却是离线），只有等设备重连时
            # 重新发布 retained status 才恢复——而 simulator/固件在稳态下不会再发。
            if state.status != "online":
                state.status = "online"
            self._history[device_id].append(
                {"ts": now, "data": dict(data), "seq": seq}
            )
            return state

    def update_alert(self, payload: dict[str, Any], *, ts: float | None = None) -> bool:
        """记录告警。

        两层幂等：
        1. alert_id 完全相同 -> 重复
        2. 同一设备同一类型在去重窗口内再次出现 -> 视为同一故障的重发（设备会周期重发以防丢包）

        返回 False 表示未新增记录。
        """
        now = ts if ts is not None else time.time()
        alert_id = str(payload.get("alert_id") or "")
        device_id = str(payload.get("device_id") or "")
        alert_type = str(payload.get("alert_type") or "")

        with self._lock:
            if alert_id:
                self._prune_alert_ids(now)
                if alert_id in self._alert_ids:
                    return False

            key = (device_id, alert_type)
            last_ts = self._type_ts.get(key)
            is_repeat = last_ts is not None and (now - last_ts) < self._type_dedupe_window

            if alert_id:
                self._alert_ids[alert_id] = now

            if is_repeat:
                # 仍然刷新设备的最新告警字段，但不新增历史记录；
                # 注意不更新 _type_ts，否则设备周期重发会让窗口无限延长
                state = self._devices.get(device_id)
                if state is not None:
                    state.last_alert = dict(payload)
                return False

            self._type_ts[key] = now

            record = dict(payload)
            record.setdefault("alert_id", alert_id or f"{device_id}-{int(now)}")
            record["received_at"] = now
            self._alerts.append(record)

            if device_id:
                state = self._devices.get(device_id)
                if state is None:
                    state = DeviceState(device_id=device_id, device_type="unknown")
                    self._devices[device_id] = state
                state.last_alert = record
            return True

    def restore_alerts(self, records: list[dict[str, Any]]) -> int:
        """把持久化的告警回填进内存。

        进程重启后如果只依赖实时消息，`--once "查看告警"` 永远是空的，
        用户会以为系统没在记录。回填时同样登记 alert_id 与 (device, type) 时间戳，
        这样设备「重发」的同一条告警依然被去重，不会因为重启就重复计数。
        """
        restored = 0
        with self._lock:
            known = {rec.get("alert_id") for rec in self._alerts if rec.get("alert_id")}
            # 存储层按时间倒序返回（最新在前），而 _alerts 是「旧→新」的 deque，
            # 这里反转后依次追加，保证 recent_alerts() 的顺序语义不变。
            for payload in reversed(records):
                alert_id = str(payload.get("alert_id") or "")
                if alert_id and (alert_id in known or alert_id in self._alert_ids):
                    continue

                record = dict(payload)
                # 存储层用 ts，缓存层用 received_at/timestamp，这里统一补齐
                ts = float(record.get("ts") or record.get("timestamp") or time.time())
                record.setdefault("timestamp", int(ts))
                record["received_at"] = float(record.get("received_at") or ts)
                record.setdefault("alert_id", alert_id or f"restored-{restored}")

                self._alerts.append(record)
                if alert_id:
                    self._alert_ids[alert_id] = record["received_at"]
                    known.add(alert_id)
                key = (str(record.get("device_id") or ""), str(record.get("alert_type") or ""))
                self._type_ts.setdefault(key, record["received_at"])
                restored += 1
        return restored

    def update_status(self, device_id: str, status: str, *, ts: float | None = None) -> None:
        now = ts if ts is not None else time.time()
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                state = DeviceState(device_id=device_id, device_type="unknown")
                self._devices[device_id] = state
            state.status = status
            state.status_ts = now
            if status == "online":
                state.last_seen = max(state.last_seen, now)

    def mark_offline(self, now: float | None = None) -> list[str]:
        """把 last_seen 超时的设备标记为离线，返回被标记的设备 ID。"""
        current = now if now is not None else time.time()
        changed: list[str] = []
        with self._lock:
            for state in self._devices.values():
                if state.status == "offline":
                    continue
                ttl = self._ttl_by_device.get(state.device_id, self._default_ttl)
                if state.last_seen and (current - state.last_seen) > ttl:
                    state.status = "offline"
                    changed.append(state.device_id)
        return changed

    # ---------------- 读取 ----------------

    def get(self, device_id: str) -> DeviceState | None:
        with self._lock:
            return self._devices.get(device_id)

    def devices(self) -> list[DeviceState]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda s: s.device_id)

    def is_online(self, device_id: str, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        with self._lock:
            state = self._devices.get(device_id)
            if state is None:
                return False
            if state.status == "offline":
                return False
            ttl = self._ttl_by_device.get(device_id, self._default_ttl)
            return bool(state.last_seen) and (current - state.last_seen) <= ttl

    def ttl_for(self, device_id: str) -> float:
        """该设备的离线判定阈值（秒）。= 上报间隔 × offline_after_periods。"""
        with self._lock:
            return self._ttl_by_device.get(device_id, self._default_ttl)

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._alerts)[-limit:][::-1]

    def alerts_since(self, since: float) -> list[dict[str, Any]]:
        with self._lock:
            return [a for a in self._alerts if a.get("received_at", 0) >= since]

    def history(self, device_id: str, metric: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = [dict(r) for r in self._history.get(device_id, ())]
        if metric is None:
            return records
        return [r for r in records if metric in (r.get("data") or {})]

    def trend(self, device_id: str, metric: str) -> Trend | None:
        """对某个指标做最小二乘斜率拟合。"""
        numeric: list[tuple[float, float]] = []
        for record in self.history(device_id, metric):
            value = record["data"].get(metric)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric.append((float(record["ts"]), float(value)))

        if len(numeric) < 2:
            return None

        values = [v for _, v in numeric]
        mean_v = sum(values) / len(values)
        slope = _least_squares_slope(numeric)
        if slope > 1e-4:
            direction = "上升"
        elif slope < -1e-4:
            direction = "下降"
        else:
            direction = "平稳"

        return Trend(
            metric=metric,
            samples=len(values),
            mean=mean_v,
            minimum=min(values),
            maximum=max(values),
            slope_per_s=slope,
            direction=direction,
        )

    def metrics(self, device_id: str) -> list[str]:
        """列出该设备出现过的数值型指标名。"""
        names: set[str] = set()
        for record in self.history(device_id):
            for key, value in (record.get("data") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    names.add(key)
        return sorted(names)

    def snapshot(self) -> dict[str, DeviceState]:
        with self._lock:
            return dict(self._devices)

    # ---------------- 内部 ----------------

    def _prune_alert_ids(self, now: float) -> None:
        # 先按时间去重窗口清理
        if len(self._alert_ids) >= ALERT_ID_MAX:
            expired = [k for k, ts in self._alert_ids.items() if now - ts > ALERT_DEDUPE_TTL_S]
            for key in expired:
                self._alert_ids.pop(key, None)
        # 仍然超限（例如短时间内收到大量不同告警）：按插入顺序淘汰最旧的。
        # 只按时间清是不够的——512 条都在 300s 窗口内时一条也删不掉，会无界增长。
        while len(self._alert_ids) > ALERT_ID_MAX:
            self._alert_ids.pop(next(iter(self._alert_ids)), None)


def _least_squares_slope(points: Iterable[tuple[float, float]]) -> float:
    pts = list(points)
    n = len(pts)
    if n < 2:
        return 0.0
    mean_x = sum(x for x, _ in pts) / n
    mean_y = sum(y for _, y in pts) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in pts)
    den = sum((x - mean_x) ** 2 for x, _ in pts)
    if den == 0:
        return 0.0
    return num / den
