"""指令下发服务：护栏校验 + 幂等 ID + 时效校验 + 等待回执。

这是「控制权不交给 LLM」的落点：无论指令来自规则引擎还是模型建议，
最终都必须经过 DeviceRegistry.validate() 才允许发到设备。

两个容易漏掉的收尾工作（都是"只增不减"型缺陷）：

* ``_pending`` 必须在 TTL 之后清理。``wait=False`` 的指令没人来 pop，
  如果只增不删，长期运行的仪表盘每下发一条就泄漏一个 Event + 一个 dict。
* ``expires_at`` 必须真的被校验。写进报文却没人读，比不写更危险：
  以后有人按这个契约做离线指令队列，设备会执行几十秒前的过期指令。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from . import protocol
from .registry import DeviceRegistry


class Publisher(Protocol):
    """只依赖 publish 这一个方法，便于测试时注入假实现。"""

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> None:
        ...


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    device_id: str
    action: str
    value: float | None
    status: str  # ok | error | timeout | rejected | pending
    detail: str
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_text(self) -> str:
        icon = {"ok": "✅", "pending": "⏳", "rejected": "🚫", "timeout": "⏱️"}.get(
            self.status, "❌"
        )
        body = f"{icon} {self.device_id} -> {self.action}"
        if self.value is not None:
            body += f"({self.value:g})"
        body += f"：{self.detail or self.status}"
        if self.latency_ms is not None:
            body += f"（往返 {self.latency_ms:.0f} ms）"
        return body


@dataclass
class _Pending:
    """一条等待回执的指令。created 用于 TTL 清理。"""

    event: threading.Event
    box: dict[str, Any]
    created: float


class CommandService:
    def __init__(
        self,
        publisher: Publisher,
        registry: DeviceRegistry,
        timeout_s: float = 10.0,
        pending_ttl_s: float | None = None,
    ) -> None:
        self._publisher = publisher
        self._registry = registry
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        # 未等待的回执也不会立刻丢：仪表盘可能在 1 秒后才去查结果，
        # 所以保留一段时间再清理，而不是"不等待就不登记"。
        self._pending_ttl_s = (
            pending_ttl_s if pending_ttl_s is not None else max(timeout_s * 3.0, 30.0)
        )

    # ---------------- 回执 ----------------

    def handle_ack(self, payload: dict[str, Any]) -> bool:
        """由 MQTT 分发层调用。返回 False 表示这是过期/未知指令的回执。"""
        command_id = str(payload.get("command_id") or "")
        if not command_id:
            return False
        with self._lock:
            self._prune_locked(time.monotonic())
            waiter = self._pending.get(command_id)
            if waiter is None:
                return False
            waiter.box.update(payload)
        waiter.event.set()
        return True

    # ---------------- 下发 ----------------

    def send(
        self,
        device_id: str,
        action: str,
        value: float | None = None,
        *,
        wait: bool = True,
        issued_by: str = "agent",
        timeout_s: float | None = None,
        expires_in_s: float = 30.0,
    ) -> CommandResult:
        spec = self._registry.get(device_id)
        error = self._registry.validate(spec, action, value)
        if error is not None:
            return CommandResult(
                command_id="",
                device_id=device_id,
                action=action,
                value=value,
                status="rejected",
                detail=error,
            )

        command_id, payload = protocol.build_command(
            action, value, issued_by=issued_by, expires_in_s=expires_in_s
        )
        # 设备端也会校验 expires_at（固件 mqtt_mgr.c / simulator/base.py），
        # 这里先拦一次，避免明知已经过期还发一条注定被拒的指令。
        if protocol.is_expired(payload):
            return CommandResult(
                command_id=command_id,
                device_id=device_id,
                action=action,
                value=value,
                status="rejected",
                detail="指令时效已过（expires_in_s 必须为正）",
            )

        event = threading.Event()
        with self._lock:
            self._prune_locked(time.monotonic())
            self._pending[command_id] = _Pending(event=event, box={}, created=time.monotonic())

        started = time.monotonic()
        try:
            self._publisher.publish(protocol.cmd_topic(device_id), payload, qos=1)
        except Exception as exc:  # 发布失败要让调用方看到原因，而不是静默成功
            with self._lock:
                self._pending.pop(command_id, None)
            return CommandResult(
                command_id=command_id,
                device_id=device_id,
                action=action,
                value=value,
                status="error",
                detail=f"指令发送失败: {exc}",
            )

        if not wait:
            return CommandResult(
                command_id=command_id,
                device_id=device_id,
                action=action,
                value=value,
                status="pending",
                detail="已下发，等待回执",
            )

        timeout = self._timeout_s if timeout_s is None else timeout_s
        completed = event.wait(timeout)
        latency_ms = (time.monotonic() - started) * 1000.0

        with self._lock:
            entry = self._pending.pop(command_id, None)
        ack = entry.box if entry is not None else {}

        if not completed:
            return CommandResult(
                command_id=command_id,
                device_id=device_id,
                action=action,
                value=value,
                status="timeout",
                detail=f"{timeout:g}s 内未收到设备回执",
                latency_ms=latency_ms,
            )

        raw_result = str(ack.get("result", "ok"))
        detail = str(ack.get("detail") or ("执行成功" if raw_result == "ok" else ""))
        return CommandResult(
            command_id=command_id,
            device_id=device_id,
            action=action,
            value=value,
            status="ok" if raw_result == "ok" else "error",
            detail=detail,
            latency_ms=latency_ms,
        )

    # ---------------- 观测 ----------------

    def poll(self, command_id: str) -> dict[str, Any] | None:
        """取回 ``wait=False`` 指令的回执；还没到则返回 None。

        给"不能阻塞的调用方"用的（Streamlit 的脚本线程就是典型：页面每 2 秒重绘，
        在里面 ``wait`` 5 秒会和刷新互相拖累）。回执到达后条目会保留到 TTL，
        所以轮询几次都能读到。
        """
        with self._lock:
            entry = self._pending.get(command_id)
            if entry is None or not entry.event.is_set():
                return None
            return dict(entry.box)

    @property
    def pending_count(self) -> int:
        with self._lock:
            self._prune_locked(time.monotonic())
            return len(self._pending)

    def _prune_locked(self, now: float) -> None:
        """调用方必须持锁。"""
        if not self._pending:
            return
        expired = [
            cid
            for cid, entry in self._pending.items()
            if now - entry.created > self._pending_ttl_s
        ]
        for cid in expired:
            self._pending.pop(cid, None)
