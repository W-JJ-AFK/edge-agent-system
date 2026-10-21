"""Agent 引擎：MQTT 消息分发 + 意图执行 + 降级。

控制流（关键设计）：
    chat(text)
      → IntentRouter
          ├─ CONTROL  → CommandService（护栏 + 幂等 + 等回执）
          ├─ QUERY    → 模板渲染（零 token）
          ├─ DIAGNOSE → LLM(工具调用)；不可用则规则引擎兜底
          └─ HELP     → 帮助文本

LLM 只出现在 DIAGNOSE 分支里，因此不可能"抢走"控制指令。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from . import knowledge, protocol
from .cache import DeviceCache
from .command_service import CommandResult, CommandService
from .config import AgentConfig
from .llm.prompts import build_context
from .llm.tools import ToolExecutor
from .registry import DeviceRegistry
from .router import HELP_TEXT, Intent, IntentRouter, Route

if TYPE_CHECKING:
    from .llm.client import OllamaClient
    from .storage.db import Store

log = logging.getLogger(__name__)


def _stamp_of(alert: dict[str, Any]) -> str:
    """告警的展示时间：优先接收时间，其次设备时间，都没有就显示 "--:--:--"。

    直接用 ``alert.get("received_at", 0)`` 会在缺字段时渲染成 1970 年的
    ``08:00:00``（东八区），看着像一条 56 年前的历史告警。
    """
    for key in ("received_at", "timestamp"):
        value = alert.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return time.strftime("%H:%M:%S", time.localtime(float(value)))
    return "--:--:--"


class AgentEngine:
    def __init__(
        self,
        config: AgentConfig,
        cache: DeviceCache,
        registry: DeviceRegistry,
        commands: CommandService,
        *,
        llm: "OllamaClient | None" = None,
        store: "Store | None" = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._registry = registry
        self._commands = commands
        self._llm = llm
        self._store = store
        self._persist_errors = 0
        self.router = IntentRouter(registry)
        self.tools = ToolExecutor(registry, cache)

        # 重启后把历史告警回填进缓存，否则"查看告警"在进程刚起来时永远是空的
        if store is not None:
            try:
                self._cache.restore_alerts(store.recent_alerts(50))
            except Exception:  # noqa: BLE001 - 回填失败不应阻塞启动
                log.debug("restore persisted alerts failed", exc_info=True)

    @property
    def cache(self) -> DeviceCache:
        """只读缓存视图（仪表盘、测试用）。"""
        return self._cache

    @property
    def registry(self) -> DeviceRegistry:
        """设备清单（CLI 等待逻辑、仪表盘都要用它判"应该有几台设备"）。"""
        return self._registry

    @property
    def llm_available(self) -> bool:
        """当前是否真的能走 LLM 通道（用于启动提示与降级判断）。"""
        if self._llm is None or not self._config.llm.enabled:
            return False
        try:
            return self._llm.available
        except Exception:
            return False

    @property
    def persist_errors(self) -> int:
        """落库失败次数（可观测性：仪表盘可据此提示磁盘/权限问题）。"""
        return self._persist_errors

    # ================= MQTT 入口 =================

    def handle_message(self, topic: str, payload: dict[str, Any]) -> None:
        parsed = protocol.parse_topic(topic)
        if parsed is None:
            return

        now = time.time()

        if parsed.kind == "telemetry":
            data = payload.get("data")
            if not isinstance(data, dict):
                return
            seq = payload.get("seq") if isinstance(payload.get("seq"), int) else None
            self._cache.update_telemetry(
                parsed.device_id,
                str(payload.get("device_type") or "unknown"),
                data,
                ts=now,
                seq=seq,
            )
            # 落库失败不能打断整条订阅回调：一条 INSERT 出错不该让后续遥测全丢
            self._persist("telemetry", "insert_telemetry", parsed.device_id, now, data, seq)

        elif parsed.kind == "alert":
            record = dict(payload)
            record.setdefault("device_id", parsed.device_id)
            # 设备时间不可信时由存储层回退到接收时间（见 protocol.resolve_timestamp）
            record["received_at"] = now
            if self._cache.update_alert(record, ts=now):
                self._persist("alert", "insert_alert", record)

        elif parsed.kind == "status":
            state = str(payload.get("state") or "").lower()
            if state in {"online", "offline"}:
                self._cache.update_status(parsed.device_id, state, ts=now)

        elif parsed.kind == "ack":
            self._commands.handle_ack(payload)

    def tick(self) -> list[str]:
        """周期性维护：把超时设备标记为离线。由 CLI 循环调用。"""
        return self._cache.mark_offline()

    # ================= 对话入口 =================

    def chat(self, text: str) -> str:
        route = self.router.route(text)

        if route.intent is Intent.CONTROL:
            return self._run_control(route)
        if route.intent is Intent.QUERY:
            return self._run_query(route)
        if route.intent is Intent.DIAGNOSE:
            return self._run_diagnose(text, route)
        return HELP_TEXT

    # ================= 分支实现 =================

    def _run_control(self, route: Route) -> str:
        # 设备认出来了但不支持该动作：如实拒绝，并说清它能做什么。
        # 绝不能"改派"给另一台设备——那会让用户以为指令生效了。
        if route.device_id and not route.action:
            return f"🚫 {route.reason or '该设备不支持这个动作'}"
        if not route.device_id or not route.action:
            return "🤔 没听出要控制哪台设备，可以说「电机降速到 800」这样带设备名的指令。"

        result = self._commands.send(route.device_id, route.action, route.value)
        self._record_command(result)
        return result.as_text()

    def _run_query(self, route: Route) -> str:
        if route.query == "list_devices":
            return self._render_devices()
        if route.query == "alerts":
            return self._render_alerts(route.device_id)
        if route.query == "trend" and route.device_id:
            return self._render_trend(route.device_id)
        if route.query == "telemetry" and route.device_id:
            return self._render_telemetry(route.device_id)
        return self._render_devices()

    def _run_diagnose(self, text: str, route: Route) -> str:
        context = build_context(self._cache, self._registry, route.device_id)

        if self._llm is not None:
            try:
                if self._llm.available:
                    answer = self._llm.diagnose(text, context, self.tools)
                    if answer:
                        return answer
            except Exception as exc:  # LLM 层任何异常都不应影响可用性
                log.warning("LLM diagnose failed, fallback to rules: %s", exc)

        return self._rule_diagnosis(route)

    # ================= 渲染 =================

    def _render_devices(self) -> str:
        now = time.time()
        specs = self._registry.all()
        online = [s for s in specs if self._cache.is_online(s.device_id, now)]

        lines = [f"📡 设备状态（在线 {len(online)}/{len(specs)}）", ""]
        for spec in specs:
            state = self._cache.get(spec.device_id)
            if state is None or not state.last_seen:
                lines.append(f"· {spec.device_id}［{spec.device_type}］ 未上报")
                continue
            mark = "在线" if self._cache.is_online(spec.device_id, now) else "离线"
            age = f"{now - state.last_seen:.0f}s 前"
            segments = [mark, age]
            # retain 的 status 可能先于首个遥测到达，此时 data 为空，
            # 不要留下 "在线 · 0s 前 · " 这种悬空分隔符
            data = self._data_summary(state.last_data)
            if data:
                segments.append(data)
            lines.append(f"· {spec.device_id}［{spec.device_type}］ " + " · ".join(segments))
        return "\n".join(lines)

    def _render_alerts(self, device_id: str | None = None) -> str:
        alerts = self._cache.recent_alerts(20)
        if device_id:
            alerts = [a for a in alerts if a.get("device_id") == device_id]
        if not alerts:
            return "✅ 当前没有活跃告警。"

        lines = [f"🚨 最近 {len(alerts)} 条告警（新→旧）", ""]
        for alert in alerts[:10]:
            lines.append(
                f"[{_stamp_of(alert)}] {alert.get('device_id')} · {alert.get('alert_type')} "
                f"= {alert.get('value')}（阈值 {alert.get('threshold')}，{alert.get('severity')}）"
            )
        return "\n".join(lines)

    def _render_telemetry(self, device_id: str) -> str:
        state = self._cache.get(device_id)
        if state is None or not state.last_seen:
            return f"❓ {device_id} 暂无数据（设备可能未启动或未上报）。"

        now = time.time()
        lines = [
            f"📈 {device_id}［{state.device_type}］",
            f"状态：{'在线' if self._cache.is_online(device_id, now) else '离线'}"
            f"，最近上报 {now - state.last_seen:.0f}s 前",
            "",
        ]
        if state.last_data.get("valid") is False:
            # 设备在线但没有有效读数：这跟"设备离线"是两件事，必须分开说，
            # 否则传感器坏了会被当成"数据稳定"。
            lines.append("⚠️ 传感器失效：设备在线，但最近一帧标记 valid=false，没有可用读数。")
            lines.append("   常见原因：传感器脱落/接触不良、供电不足、I2C 或单总线异常。")
            return "\n".join(lines)

        for key, value in state.last_data.items():
            if key == "valid":
                continue
            lines.append(f"· {key} = {value}")

        trends = [t for t in (self._cache.trend(device_id, m) for m in self._cache.metrics(device_id)) if t]
        if trends:
            lines.append("")
            lines.append("趋势：")
            lines.extend(f"· {t.as_text()}" for t in trends[:4])
        return "\n".join(lines)

    def _render_trend(self, device_id: str) -> str:
        metrics = self._cache.metrics(device_id)
        trends = [t for t in (self._cache.trend(device_id, m) for m in metrics) if t]
        if not trends:
            return f"❓ {device_id} 历史数据不足，暂时算不出趋势。"

        lines = [f"📊 {device_id} 趋势分析", ""]
        lines.extend(f"· {t.as_text()}" for t in trends)
        return "\n".join(lines)

    @staticmethod
    def _data_summary(data: dict[str, Any], limit: int = 3) -> str:
        """把一帧遥测压成一行摘要；失效帧显式说明，而不是把 valid=False 当指标。"""
        if not data:
            return ""
        if data.get("valid") is False:
            return "⚠️ 传感器失效"
        pairs = [f"{k}={v}" for k, v in data.items() if k != "valid"]
        return "，".join(pairs[:limit])

    # ================= 规则引擎诊断（降级路径） =================

    def _rule_diagnosis(self, route: Route) -> str:
        now = time.time()
        device_id = route.device_id

        alerts = [a for a in self._cache.alerts_since(now - 300)
                  if device_id is None or a.get("device_id") == device_id]

        lines = ["🔍 诊断报告（规则引擎模式）", ""]

        # 一、状态总览
        lines.append("一、设备状态总览")
        if device_id:
            state = self._cache.get(device_id)
            if state is None:
                lines.append(f"· {device_id} 无数据")
            else:
                data = self._data_summary(state.last_data, limit=8) or "无有效读数"
                lines.append(
                    f"· {device_id}："
                    f"{'在线' if self._cache.is_online(device_id, now) else '离线'}，{data}"
                )
        else:
            for spec in self._registry.all():
                state = self._cache.get(spec.device_id)
                if state is None:
                    lines.append(f"· {spec.device_id}：无数据")
                    continue
                lines.append(
                    f"· {spec.device_id}："
                    f"{'在线' if self._cache.is_online(spec.device_id, now) else '离线'}"
                    f"，{len(state.last_data)} 项指标"
                )

        # 二、异常分析
        lines.append("")
        lines.append("二、异常分析")
        if alerts:
            for alert in alerts[-5:]:
                entry = knowledge.get(str(alert.get("alert_type")))
                lines.append(
                    f"· {alert.get('device_id')} 触发 {alert.get('alert_type')}："
                    f"{alert.get('value')}（阈值 {alert.get('threshold')}）"
                )
                if entry is not None:
                    lines.append(f"  可能原因：{'、'.join(entry.causes)}")
        else:
            lines.append("· 近 5 分钟没有活跃告警，读数处于正常区间。")

        # 三、建议操作
        lines.append("")
        lines.append("三、建议操作")
        suggestions: list[str] = []
        for alert in alerts[-3:]:
            entry = knowledge.get(str(alert.get("alert_type")))
            if entry is not None:
                suggestions.extend(entry.actions[:2])
        if suggestions:
            for index, action in enumerate(dict.fromkeys(suggestions), start=1):
                lines.append(f"{index}. {action}")
        else:
            lines.append("1. 维持当前运行参数，继续观察趋势变化")

        # 四、风险评估
        lines.append("")
        severities = {str(a.get("severity")) for a in alerts}
        if "critical" in severities:
            risk = "高"
        elif "warning" in severities:
            risk = "中"
        else:
            risk = "低"
        lines.append(f"四、风险评估\n风险等级：{risk}")

        if self._llm is not None and self._config.llm.enabled:
            lines.append("\n（本地模型暂不可用，已自动切换到规则引擎）")

        return "\n".join(lines)

    # ================= 内部 =================

    def _persist(self, what: str, method: str, *args: Any) -> None:
        """统一包裹存储调用：失败只计数 + 告警，不打断消息分发。

        用方法名而不是传方法对象：`self._store.insert_telemetry` 这种写法在
        `_store is None` 时会在**取属性**阶段就抛 AttributeError，
        等于把保护写在了异常发生之后。
        """
        if self._store is None:
            return
        try:
            getattr(self._store, method)(*args)
        except Exception as exc:  # noqa: BLE001 - 存储层异常不应影响可用性
            self._persist_errors += 1
            log.warning("persist %s failed (#%d): %s", what, self._persist_errors, exc)

    def _record_command(self, result: CommandResult) -> None:
        if self._store is None or not result.command_id:
            return
        try:
            self._store.record_command(
                result.command_id,
                result.device_id,
                result.action,
                result.value,
                result.status,
                result.detail,
                result.latency_ms,
            )
        except Exception as exc:
            log.warning("failed to persist command: %s", exc)
