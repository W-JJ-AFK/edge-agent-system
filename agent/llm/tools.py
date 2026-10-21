"""LLM 工具定义与执行器。

**权限分离**：这里只暴露只读工具。控制设备的 send_command 故意不作为工具提供给模型——
模型可以「建议」降速，但真正下发必须经过 CommandService 的护栏校验与调用方确认。
这是 LLM 应用最基本的边界：模型不直接持有写权限。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .. import knowledge
from ..cache import DeviceCache
from ..registry import DeviceRegistry

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": "列出所有已注册设备及其在线状态、最近上报时间。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_telemetry",
            "description": "获取指定设备最新一帧遥测数据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备 ID，例如 vibration_01"}
                },
                "required": ["device_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_trend",
            "description": "对指定设备某个指标做趋势统计（均值、极值、斜率）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "metric": {"type": "string", "description": "指标名，例如 rms/temperature"},
                },
                "required": ["device_id", "metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_knowledge",
            "description": "查询故障知识库，获得某类告警的可能原因与建议操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert_type": {
                        "type": "string",
                        "description": "告警类型：temp_high / vibration_high / current_overload / co2_high",
                    }
                },
                "required": ["alert_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_alerts",
            "description": "获取最近一段时间内的告警记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "since_seconds": {"type": "integer", "description": "回溯秒数，默认 300"}
                },
                "required": [],
            },
        },
    },
]


class ToolExecutor:
    """把工具名映射到本地实现，并对异常做兜底，避免把栈信息喂给模型。"""

    def __init__(self, registry: DeviceRegistry, cache: DeviceCache) -> None:
        self._registry = registry
        self._cache = cache
        self._handlers: dict[str, Callable[..., Any]] = {
            "list_devices": self.list_devices,
            "get_telemetry": self.get_telemetry,
            "analyze_trend": self.analyze_trend,
            "query_knowledge": self.query_knowledge,
            "get_alerts": self.get_alerts,
        }

    def call(self, name: str, arguments: dict[str, Any] | str | None) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"error": f"未知工具 {name}"}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        args = arguments or {}
        try:
            return {"ok": True, "data": handler(**args)}
        except TypeError as exc:
            return {"error": f"参数不匹配: {exc}"}
        except Exception as exc:  # 工具内部错误也要结构化返回
            return {"error": f"工具执行失败: {exc}"}

    # ---------------- 工具实现 ----------------

    def list_devices(self) -> list[dict[str, Any]]:
        now = time.time()
        result = []
        for spec in self._registry.all():
            state = self._cache.get(spec.device_id)
            result.append(
                {
                    "device_id": spec.device_id,
                    "device_type": spec.device_type,
                    "source": spec.source,
                    "online": self._cache.is_online(spec.device_id, now),
                    "last_seen_ago_s": round(now - state.last_seen, 1) if state and state.last_seen else None,
                    "last_data": state.last_data if state else {},
                }
            )
        return result

    def get_telemetry(self, device_id: str) -> dict[str, Any]:
        state = self._cache.get(device_id)
        if state is None:
            return {"error": f"设备 {device_id} 无数据"}
        return {
            "device_id": device_id,
            "device_type": state.device_type,
            "last_seen_ago_s": round(time.time() - state.last_seen, 1) if state.last_seen else None,
            "data": state.last_data,
            "last_alert": state.last_alert,
        }

    def analyze_trend(self, device_id: str, metric: str) -> dict[str, Any]:
        trend = self._cache.trend(device_id, metric)
        if trend is None:
            return {
                "error": f"设备 {device_id} 的 {metric} 历史数据不足",
                "available_metrics": self._cache.metrics(device_id),
            }
        return {
            "metric": trend.metric,
            "samples": trend.samples,
            "mean": round(trend.mean, 4),
            "min": round(trend.minimum, 4),
            "max": round(trend.maximum, 4),
            "slope_per_s": round(trend.slope_per_s, 5),
            "direction": trend.direction,
        }

    def query_knowledge(self, alert_type: str) -> dict[str, Any]:
        entry = knowledge.get(alert_type)
        if entry is None:
            return {"error": f"未知告警类型 {alert_type}", "known": knowledge.alert_types()}
        return {
            "alert_type": entry.alert_type,
            "severity": entry.severity,
            "causes": list(entry.causes),
            "actions": list(entry.actions),
        }

    def get_alerts(self, since_seconds: int = 300) -> list[dict[str, Any]]:
        """返回告警记录，时间统一用**本地接收时间**。

        不能直接回传报文里的 ``timestamp``：设备可能没同步时间，那个值可能是
        开机秒数。喂给模型的应该是可靠的时间，否则它会说"这条告警发生在 1970 年"。
        """
        since = time.time() - max(1, int(since_seconds))
        return [
            {
                "device_id": a.get("device_id"),
                "alert_type": a.get("alert_type"),
                "value": a.get("value"),
                "threshold": a.get("threshold"),
                "severity": a.get("severity"),
                "ts": int(a.get("received_at") or 0),
                "ts_iso": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(a.get("received_at") or 0)
                ),
            }
            for a in self._cache.alerts_since(since)
        ]
