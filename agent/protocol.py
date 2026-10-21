"""MQTT 报文契约。

与固件端 ``main/net/protocol.c`` 严格对应，任何改动必须两端同步。

主题矩阵：

    devices/{id}/telemetry   QoS0        遥测
    devices/{id}/status      QoS1 retain 在线状态（含 LWT 遗嘱）
    devices/{id}/alert       QoS1        告警
    devices/{id}/cmd         QoS1        指令（下行）
    devices/{id}/cmd/ack     QoS1        执行回执

字段矩阵（**这张表是两端唯一的契约来源**，不做「各写各的 docstring」）：

    | 字段       | 时基 / 取值                    | 缺省含义                        |
    |------------|--------------------------------|---------------------------------|
    | timestamp  | Unix epoch 秒                  | 字段缺失 = 设备未同步时间       |
    | uptime_s   | 开机秒数（单调）               | 必定存在，仅供排查时序，不可当日志时间 |
    | valid      | bool                           | 字段缺失 = 该帧数据有效         |
    | expires_at | Unix epoch 秒（仅指令）        | 字段缺失 = 不做时效校验         |
    | seq        | 设备内自增（仅振动遥测）       | 字段缺失 = 不参与去重           |
    | state      | NORMAL/WARNING/ALARM/UNKNOWN   | UNKNOWN = 传感器不可用          |

粒度与作用域：

    * ``status`` 是**设备级**的：每台设备一条 MQTT 连接、一份遗嘱。
      固件为此给每个 device_id 单独建连接（见 ``mqtt_mgr.c`` 的文件头注释）。
    * ``set_interval`` 对硬件是**节点级**的（一块板上的两个 device_id 共用一个周期），
      对模拟器是单设备。Agent 只按设备下发，不假设作用域。
    * ``alert.severity`` 固件恒为 ``critical``（WARNING 状态不发告警）。

为什么 timestamp 需要 ``resolve_timestamp``：
设备没有 RTC 电池，SNTP 同步前 ``time(NULL)`` 从 1970 开始。早期固件直接把
"开机秒数"写进 ``timestamp``，Agent 又按 epoch 落库，于是真机告警在数据库里
全变成 1970 年——模拟器演示完全正常，接上硬件才炸。现在固件在未同步时
**不写** timestamp，Agent 侧再兜一层可信度校验，两边都不依赖对方正确。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

TOPIC_PREFIX = "devices"
TELEMETRY_SUFFIX = "telemetry"
STATUS_SUFFIX = "status"
ALERT_SUFFIX = "alert"
CMD_SUFFIX = "cmd"
ACK_SUFFIX = "cmd/ack"

TELEMETRY_FILTER = f"{TOPIC_PREFIX}/+/{TELEMETRY_SUFFIX}"
ALERT_FILTER = f"{TOPIC_PREFIX}/+/{ALERT_SUFFIX}"
STATUS_FILTER = f"{TOPIC_PREFIX}/+/{STATUS_SUFFIX}"
ACK_FILTER = f"{TOPIC_PREFIX}/+/{ACK_SUFFIX}"

#: 早于此值的时间戳不可能是真实 epoch（2020-09-13）
EPOCH_MIN_PLAUSIBLE = 1_600_000_000
#: 与本地时钟偏差超过这个范围的 timestamp 也不可信（容忍 7 天，覆盖长期宕机）
EPOCH_MAX_SKEW_S = 7 * 24 * 3600

STATE_UNKNOWN = "UNKNOWN"


def telemetry_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{TELEMETRY_SUFFIX}"


def status_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{STATUS_SUFFIX}"


def alert_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{ALERT_SUFFIX}"


def cmd_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{CMD_SUFFIX}"


def ack_topic(device_id: str) -> str:
    return f"{TOPIC_PREFIX}/{device_id}/{ACK_SUFFIX}"


@dataclass(frozen=True)
class ParsedTopic:
    """拆分后的主题信息。"""

    device_id: str
    kind: str  # telemetry | status | alert | cmd | ack


def parse_topic(topic: str) -> ParsedTopic | None:
    """把 ``devices/{id}/{kind}`` 拆开，无法识别时返回 None。"""
    if not topic:
        return None
    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != TOPIC_PREFIX:
        return None
    if len(parts) == 3:
        kind = parts[2]
    elif len(parts) == 4 and parts[2] == "cmd" and parts[3] == "ack":
        kind = "ack"
    else:
        return None
    if kind not in {TELEMETRY_SUFFIX, STATUS_SUFFIX, ALERT_SUFFIX, CMD_SUFFIX, "ack"}:
        return None
    return ParsedTopic(device_id=parts[1], kind=kind)


def decode(payload: bytes | str) -> dict[str, Any] | None:
    """把 MQTT 载荷解码成 dict，非法 JSON 或非对象返回 None。"""
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def resolve_timestamp(payload: dict[str, Any], now: float | None = None) -> float:
    """把报文里的 ``timestamp`` 转成可信的时间戳，不可信时回退到本地接收时间。

    判据很保守：必须是 [EPOCH_MIN_PLAUSIBLE, now + 7 天] 区间内的数字。
    这样同时挡住三种情况——设备发 uptime（很小）、设备时钟跑飞（1970 或未来）、
    字段是字符串或布尔。落库与喂给 LLM 的时间都用这个函数的返回值。
    """
    current = time.time() if now is None else now
    raw = payload.get("timestamp")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return current
    value = float(raw)
    if value < EPOCH_MIN_PLAUSIBLE:
        return current
    if abs(value - current) > EPOCH_MAX_SKEW_S:
        return current
    return value


def is_valid_frame(payload: dict[str, Any]) -> bool:
    """``data.valid`` 显式为 False 表示传感器失效（不是"数据为 0"）。"""
    data = payload.get("data")
    return not (isinstance(data, dict) and data.get("valid") is False)


def is_sensor_unknown(payload: dict[str, Any]) -> bool:
    """传感器不可用：valid=false，或状态被显式标成 UNKNOWN。"""
    if not is_valid_frame(payload):
        return True
    data = payload.get("data")
    return isinstance(data, dict) and str(data.get("state", "")).upper() == STATE_UNKNOWN


def new_command_id() -> str:
    """指令 ID：时间前缀便于排查，随机后缀保证唯一。"""
    return f"cmd-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def build_command(
    action: str,
    value: float | None = None,
    *,
    command_id: str | None = None,
    issued_by: str = "agent",
    expires_in_s: float = 30.0,
) -> tuple[str, str]:
    """构造指令报文。

    ``expires_at`` 是**两端都实现**的字段：设备收指令时若已过期则回
    ``result=error, detail=command expired`` 并拒绝执行（见固件
    ``mqtt_mgr.c`` 的 handle_command_message 与 ``simulator/base.py``）。

    :return: (command_id, json 字符串)
    """
    cid = command_id or new_command_id()
    payload: dict[str, Any] = {
        "command_id": cid,
        "action": action,
        "issued_by": issued_by,
        "expires_at": int(time.time() + expires_in_s),
    }
    if value is not None:
        payload["value"] = value
    return cid, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def is_expired(payload: dict[str, Any] | str | bytes, now: float | None = None) -> bool:
    """判断指令是否已过期。没有 ``expires_at`` 时视为不过期。

    同时接受 dict 与 JSON 字符串：``build_command`` 返回的是字符串，
    设备侧收到的是 dict，两处都要能校验。
    """
    if isinstance(payload, (str, bytes)):
        payload = decode(payload) or {}
    ts = payload.get("expires_at")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    return (now if now is not None else time.time()) > float(ts)
