"""提示词与上下文构建。

上下文按意图裁剪是控制 token 成本的关键：
旧实现无论问什么都把 5 台设备全量数据塞进 prompt，用 1.5b 小模型时既慢又容易跑偏。
"""

from __future__ import annotations

import time
from typing import Any

from .. import knowledge
from ..cache import DeviceCache
from ..registry import DeviceRegistry

SYSTEM_PROMPT = """你是工业物联网设备运维专家，负责根据真实遥测数据做故障诊断。

回答要求：
1. **设备状态总览**：一句话概括涉及设备当前状态
2. **异常分析**：只针对数据中确实存在的异常，说明可能原因
3. **建议操作**：给出 1~3 条具体、可执行的步骤
4. **风险评估**：给出 低 / 中 / 高，并说明依据

约束：
- 只能使用提供的数据与工具返回结果，不要编造数值、设备或阈值
- 数据不足时明确说明"数据不足"，不要猜测
- 用中文回答，专业但易懂
"""


def build_context(
    cache: DeviceCache,
    registry: DeviceRegistry,
    device_id: str | None = None,
    alert_window_s: int = 300,
) -> str:
    """按意图裁剪上下文：指定设备时只喂该设备，否则给全量摘要。"""
    now = time.time()
    sections: list[str] = []

    if device_id:
        spec = registry.get(device_id)
        state = cache.get(device_id)
        if spec is None or state is None:
            return "（该设备暂无数据）"
        sections.append(_device_block(device_id, state, cache, now))
        related = [
            a for a in cache.alerts_since(now - alert_window_s)
            if a.get("device_id") == device_id
        ]
    else:
        sections.append(_overview_block(registry, cache, now))
        related = cache.alerts_since(now - alert_window_s)

    sections.append(_alerts_block(related))

    kb_types = {str(a.get("alert_type")) for a in related if a.get("alert_type")}
    kb_text = [
        entry.as_text() for entry in (knowledge.get(t) for t in kb_types) if entry is not None
    ]
    if kb_text:
        sections.append("=== 知识库参考 ===\n" + "\n".join(kb_text))

    return "\n\n".join(sections)


def _overview_block(registry: DeviceRegistry, cache: DeviceCache, now: float) -> str:
    lines = ["=== 设备总览 ==="]
    for spec in registry.all():
        state = cache.get(spec.device_id)
        online = cache.is_online(spec.device_id, now)
        if state is None:
            lines.append(f"- {spec.device_id}（{spec.device_type}）：无数据")
            continue
        age = f"{now - state.last_seen:.1f}s 前" if state.last_seen else "从未上报"
        data = ", ".join(f"{k}={v}" for k, v in state.last_data.items())
        lines.append(
            f"- {spec.device_id}（{spec.device_type}）：{'在线' if online else '离线'}，{age}，{data}"
        )
    return "\n".join(lines)


def _device_block(device_id: str, state: Any, cache: DeviceCache, now: float) -> str:
    lines = [f"=== 设备 {device_id}（{state.device_type}）==="]
    age = f"{now - state.last_seen:.1f}s 前" if state.last_seen else "从未上报"
    lines.append(f"最新上报：{age}")
    for key, value in state.last_data.items():
        lines.append(f"  {key} = {value}")

    for metric in cache.metrics(device_id)[:4]:
        trend = cache.trend(device_id, metric)
        if trend is not None:
            lines.append("  " + trend.as_text())
    return "\n".join(lines)


def _alerts_block(alerts: list[dict[str, Any]]) -> str:
    if not alerts:
        return "=== 近期告警 ===\n无"
    lines = ["=== 近期告警 ==="]
    for alert in alerts[-10:]:
        lines.append(
            f"- {alert.get('device_id')} {alert.get('alert_type')} "
            f"= {alert.get('value')}（阈值 {alert.get('threshold')}, {alert.get('severity')}）"
        )
    return "\n".join(lines)


def build_messages(question: str, context: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{context}\n\n=== 用户问题 ===\n{question}",
        },
    ]
