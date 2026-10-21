"""意图路由：把自然语言分成 CONTROL / QUERY / DIAGNOSE / HELP 四路。

为什么必须先判 CONTROL：
旧实现里 ``chat()`` 优先调用 LLM，而 LLM 只要可用就会返回一段文字，
导致「电机降速到800」只会得到"建议降速"，规则引擎里的真正执行分支永远不可达。
把控制意图放在最高优先级，控制权才真正回到确定性代码手里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .registry import DeviceRegistry


class Intent(str, Enum):
    CONTROL = "control"
    QUERY = "query"
    DIAGNOSE = "diagnose"
    HELP = "help"


@dataclass(frozen=True)
class Route:
    intent: Intent
    device_id: str | None = None
    action: str | None = None
    value: float | None = None
    query: str | None = None
    reason: str = ""


_NUMBER = r"(\d+(?:\.\d+)?)"

_RULES: tuple[tuple[str, re.Pattern[str], str | None, str], ...] = (
    # (动作, 正则, 第二捕获组用途, 说明)
    ("set_speed", re.compile(r"(?:降速|减速|提速|升速|转速|调速|speed)[^0-9]{0,6}" + _NUMBER, re.I), "number", "调整转速"),
    ("set_brightness", re.compile(r"(?:亮度|brightness)[^0-9]{0,6}" + _NUMBER, re.I), "number", "调整亮度"),
    ("set_threshold", re.compile(r"(?:阈值|threshold)[^0-9]{0,6}" + _NUMBER, re.I), "number", "调整告警阈值"),
    ("set_interval", re.compile(r"(?:间隔|周期|interval)[^0-9]{0,6}" + _NUMBER + r"\s*(毫秒|ms|秒|s|分钟|min)?", re.I), "interval", "调整上报周期"),
    ("off", re.compile(r"(?:关灯|关掉.{0,4}灯|关闭.{0,4}灯|把灯关)", re.I), None, "关灯"),
    ("on", re.compile(r"(?:开灯|打开.{0,4}灯|点亮|把灯开)", re.I), None, "开灯"),
    ("stop", re.compile(r"(?:停机|停止运行|停转|紧急停止)", re.I), None, "停机"),
    ("start", re.compile(r"(?:启动电机|开机|启动运行)", re.I), None, "启动"),
    ("reboot", re.compile(r"(?:重启|reboot)", re.I), None, "重启节点"),
)

_LIST_PATTERNS = re.compile(r"(设备列表|有哪些设备|所有设备|在线设备|扫描设备|设备清单|list devices)", re.I)
_ALERT_PATTERNS = re.compile(r"(告警|报警|alert|故障|异常)", re.I)
_TREND_PATTERNS = re.compile(r"(趋势|走势|变化情况|trend)", re.I)
_TELEMETRY_PATTERNS = re.compile(
    r"(数据|读数|状态|情况|状况|怎么样|咋样|如何|多少|值)", re.I
)

_DIAGNOSE_STRONG = re.compile(
    r"(为什么|为啥|原因|怎么回事|什么原因|诊断|分析一下|帮我分析|怎么处理|如何处理|根因|why)", re.I
)
_GREETING = re.compile(r"^(你好|您好|hi|hello|help|帮助|能做什么|怎么用|\?|？)?$", re.I)


class IntentRouter:
    def __init__(self, registry: DeviceRegistry) -> None:
        self._registry = registry

    def route(self, text: str) -> Route:
        raw = (text or "").strip()
        if not raw or _GREETING.match(raw):
            return Route(Intent.HELP, reason="空输入或问候")

        # 1) 控制意图：最高优先级，保证指令一定被执行而不是被"建议"
        control = self._match_control(raw)
        if control is not None:
            return control

        # 2) 强诊断信号（先于查询，否则"为什么告警"会被当成查询告警列表）
        if _DIAGNOSE_STRONG.search(raw):
            spec = self._registry.find(raw)
            return Route(
                Intent.DIAGNOSE,
                device_id=spec.device_id if spec else None,
                reason="命中诊断关键词",
            )

        # 3) 结构化查询：模板渲染，不消耗 token
        query = self._match_query(raw)
        if query is not None:
            return query

        # 4) 其余自由问题交给 LLM
        spec = self._registry.find(raw)
        return Route(
            Intent.DIAGNOSE,
            device_id=spec.device_id if spec else None,
            reason="未命中规则，兜底交给 LLM",
        )

    # ---------------- 内部 ----------------

    def _match_control(self, text: str) -> Route | None:
        for action, pattern, value_kind, _reason in _RULES:
            match = pattern.search(text)
            if not match:
                continue

            spec = self._registry.find(text)

            # 用户点名了设备、但这台设备不支持该动作 —— 直接拒绝，绝不改派。
            #
            # 旧逻辑会回退到 first_supporting(action)：说"电机重启"，motor_01 不支持
            # reboot，于是指令被改派给 ACTION_OWNER 里的温度节点，还回一句
            # ✅ temp_sensor_01 -> reboot。用户以为重启了电机，实际重启了另一台设备。
            # 对控制类操作，"改派"比"拒绝"危险得多。
            if spec is not None and action not in spec.capabilities:
                supported = "、".join(sorted(spec.capabilities)) or "无"
                return Route(
                    Intent.CONTROL,
                    device_id=spec.device_id,
                    action=None,
                    value=None,
                    reason=(
                        f"{spec.device_id}（{spec.device_type}）不支持 {action}，"
                        f"可用动作：{supported}"
                    ),
                )

            if spec is None:
                # 完全没点名设备时才按"动作归属设备类型"兜底填空
                spec = self._registry.first_supporting(action)
            if spec is None:
                continue

            value: float | None = None
            if value_kind == "number":
                value = float(match.group(1))
            elif value_kind == "interval":
                value = self._parse_interval(float(match.group(1)), match.group(2))

            return Route(
                Intent.CONTROL,
                device_id=spec.device_id,
                action=action,
                value=value,
                reason=f"命中控制规则 {action}",
            )
        return None

    def _match_query(self, text: str) -> Route | None:
        spec = self._registry.find(text)

        if _LIST_PATTERNS.search(text):
            return Route(Intent.QUERY, query="list_devices", reason="查询设备列表")
        if _ALERT_PATTERNS.search(text):
            return Route(
                Intent.QUERY,
                device_id=spec.device_id if spec else None,
                query="alerts",
                reason="查询告警",
            )
        if spec is not None and _TREND_PATTERNS.search(text):
            return Route(
                Intent.QUERY,
                device_id=spec.device_id,
                query="trend",
                reason="查询趋势",
            )
        if spec is not None and _TELEMETRY_PATTERNS.search(text):
            return Route(
                Intent.QUERY,
                device_id=spec.device_id,
                query="telemetry",
                reason="查询遥测",
            )
        return None

    @staticmethod
    def _parse_interval(value: float, unit: str | None) -> float:
        """统一换算成毫秒。无单位时按大小猜测：小于 120 视为秒。"""
        if unit is None:
            return value * 1000.0 if value < 120 else value
        unit = unit.lower()
        if unit in {"毫秒", "ms"}:
            return value
        if unit in {"秒", "s"}:
            return value * 1000.0
        if unit in {"分钟", "min"}:
            return value * 60000.0
        return value


HELP_TEXT = """🤖 设备运维 Agent

控制类（走确定性规则，执行后返回设备回执）：
  • 电机降速到 800
  • 关灯 / 开灯
  • 把振动阈值调到 0.6
  • 上报间隔改成 5 秒

查询类（模板化输出，不消耗 token）：
  • 有哪些设备
  • 查看告警
  • 振动传感器的数据
  • 电机的趋势

诊断类（本地大模型 + 工具调用）：
  • 为什么振动传感器告警了？
  • 帮我分析一下温度传感器的数据
"""
