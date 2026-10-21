"""Streamlit 运维面板。

旧实现的三个问题在这里都修掉了：
1. 用 sleep(3)+sleep(2) 盲等快照 -> 改为常驻订阅线程 + 队列，告警不再丢失
2. 每次刷新重建订阅 -> 连接只建一次（st.cache_resource），页面 rerun 不重建
3. 只能看不能控 -> 增加指令面板，显示「待确认 / 已执行 / 超时」

启动：streamlit run agent/dashboard/app.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import protocol  # noqa: E402
from agent.bus import create_client  # noqa: E402
from agent.cache import DeviceCache  # noqa: E402
from agent.command_service import CommandService  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.registry import DeviceRegistry  # noqa: E402

st.set_page_config(page_title="IoT 设备运维中心", page_icon="🛰️", layout="wide")

ICONS = {
    "temperature_sensor": "🌡️",
    "vibration_sensor": "📳",
    "motor_controller": "⚙️",
    "smart_light": "💡",
    "env_monitor": "🌿",
}


class DashboardBus:
    """常驻订阅：把 MQTT 消息持续写入缓存，并承接指令回执。"""

    def __init__(self, config: AgentConfig, cache: DeviceCache) -> None:
        self.config = config
        self.cache = cache
        self.command_service: CommandService | None = None
        self.connected = threading.Event()
        self.message_count = 0
        self._client = create_client(f"dashboard-{int(time.time())}")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def start(self) -> bool:
        """非阻塞启动：broker 没起也能先把面板打开，连上后自动刷新订阅。

        与 ``agent/bus.py`` 一致地改用 ``connect_async`` + ``loop_start``。
        旧写法在 broker 未启动时 ``connect()`` 抛异常 → ``loop_start()`` 没执行
        → paho 的自动重连根本没启用，面板会一直显示"断开"直到手动重启 Streamlit。
        """
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)
        try:
            self._client.connect_async(self.config.mqtt.host, self.config.mqtt.port, 30)
        except Exception:
            return False
        self._client.loop_start()
        return self.connected.wait(3.0)

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> None:
        if not self.connected.is_set():
            raise ConnectionError("MQTT 未连接")
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        code = int(getattr(reason_code, "value", reason_code) or 0)
        if code != 0:
            return
        self.connected.set()
        for topic_filter in (
            protocol.TELEMETRY_FILTER,
            protocol.ALERT_FILTER,
            protocol.STATUS_FILTER,
            protocol.ACK_FILTER,
        ):
            client.subscribe(topic_filter, qos=1)

    def _on_disconnect(self, client, userdata, *args) -> None:
        # paho 1.x 传 (rc)，2.x 传 (flags, reason_code, properties)；
        # 面板只关心"断了"，用 *args 兼容两代签名。
        self.connected.clear()

    def _on_message(self, client, userdata, msg) -> None:
        payload = protocol.decode(msg.payload)
        if payload is None:
            return
        parsed = protocol.parse_topic(msg.topic)
        if parsed is None:
            return

        self.message_count += 1
        now = time.time()

        if parsed.kind == "telemetry":
            data = payload.get("data")
            if isinstance(data, dict):
                self.cache.update_telemetry(
                    parsed.device_id,
                    str(payload.get("device_type") or "unknown"),
                    data,
                    ts=now,
                    seq=payload.get("seq"),
                )
        elif parsed.kind == "alert":
            payload.setdefault("device_id", parsed.device_id)
            self.cache.update_alert(payload, ts=now)
        elif parsed.kind == "status":
            state = str(payload.get("state") or "").lower()
            if state in {"online", "offline"}:
                self.cache.update_status(parsed.device_id, state, ts=now)
        elif parsed.kind == "ack" and self.command_service is not None:
            self.command_service.handle_ack(payload)


@st.cache_resource
def get_runtime() -> dict:
    config = AgentConfig.load()
    registry = DeviceRegistry(config.devices)
    cache = DeviceCache(ttl_by_device=registry.ttl_map(config.offline_after_periods))
    bus = DashboardBus(config, cache)
    online = bus.start()
    commands = CommandService(bus, registry, timeout_s=config.command_timeout_s)
    bus.command_service = commands
    return {
        "config": config,
        "registry": registry,
        "cache": cache,
        "bus": bus,
        "commands": commands,
        "mqtt_online": online,
    }


runtime = get_runtime()
config: AgentConfig = runtime["config"]
registry: DeviceRegistry = runtime["registry"]
cache: DeviceCache = runtime["cache"]
bus: DashboardBus = runtime["bus"]
commands: CommandService = runtime["commands"]


def render_metrics() -> None:
    now = time.time()
    specs = registry.all()
    online = [s for s in specs if cache.is_online(s.device_id, now)]
    alerts = cache.recent_alerts(50)
    critical = sum(1 for a in alerts if a.get("severity") == "critical")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("在线设备", f"{len(online)}/{len(specs)}")
    col2.metric("严重告警", critical)
    col3.metric("告警总数", len(alerts))
    col4.metric("MQTT", "已连接" if bus.connected.is_set() else "断开")


def render_devices() -> None:
    st.subheader("📡 设备实时数据")
    specs = registry.all()
    if not specs:
        st.info("配置中没有设备")
        return

    columns = st.columns(min(3, len(specs)))
    now = time.time()
    for index, spec in enumerate(specs):
        state = cache.get(spec.device_id)
        column = columns[index % len(columns)]
        with column:
            online = cache.is_online(spec.device_id, now)
            icon = ICONS.get(spec.device_type, "📦")
            st.markdown(f"### {icon} {spec.device_id}")
            st.caption(
                f"{spec.device_type} · {'🟢 在线' if online else '⚪ 离线'}"
                f" · 来源 {spec.source}"
            )
            if state is None or not state.last_data:
                st.write("暂无数据")
                continue
            for key, value in state.last_data.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    st.metric(key, f"{value:g}")
                else:
                    st.text(f"{key}: {value}")


def render_alerts() -> None:
    st.subheader("🚨 告警记录")
    alerts = cache.recent_alerts(50)
    if not alerts:
        st.info("当前无告警")
        return
    st.dataframe(
        [
            {
                "时间": time.strftime("%H:%M:%S", time.localtime(a.get("received_at", 0))),
                "级别": a.get("severity"),
                "设备": a.get("device_id"),
                "类型": a.get("alert_type"),
                "值": a.get("value"),
                "阈值": a.get("threshold"),
            }
            for a in alerts
        ],
        use_container_width=True,
        hide_index=True,
    )


def render_commands() -> None:
    st.subheader("🎮 指令下发")
    specs = registry.all()
    if not specs:
        return

    device_ids = [s.device_id for s in specs]
    col1, col2, col3 = st.columns([2, 2, 2])
    device_id = col1.selectbox("设备", device_ids, key="cmd_device")
    spec = registry.get(device_id)
    actions = sorted(spec.capabilities) if spec else []
    action = col2.selectbox("动作", actions or ["无可用动作"], key="cmd_action")
    needs_value = bool(spec and spec.capabilities.get(action))
    value = col3.number_input("参数", value=800.0, step=1.0, key="cmd_value") if needs_value else None

    if st.button("下发指令", type="primary", disabled=not (spec and actions)):
        # 不阻塞：Streamlit 的脚本线程每 2s 重绘一次，在里面等 5s 回执会和
        # run_every="2s" 的 fragment 互相拖累（页面卡住、回执也读不到）。
        # 改成"先下发、下一轮渲染再取回执"。
        result = commands.send(device_id, action, value, wait=False)
        st.session_state["pending_cmd"] = result.command_id
        st.info(result.as_text())

    pending_id = st.session_state.get("pending_cmd")
    if pending_id:
        ack = commands.poll(pending_id)
        if ack is None:
            st.info(f"⏳ 等待设备回执…（command_id={pending_id}）")
        else:
            if ack.get("result") == "ok":
                st.success(f"✅ {ack.get('detail') or '执行成功'}")
            else:
                st.error(f"❌ {ack.get('detail') or ack.get('result')}")
            st.session_state["pending_cmd"] = None

    if spec is not None:
        st.caption(
            "动作说明："
            + ("、".join(f"{k}{'(带参数)' if v else ''}" for k, v in spec.capabilities.items()) or "无")
        )


st.title("🛰️ IoT 设备运维中心")
if not bus.connected.is_set():
    st.warning("MQTT 未连接：请先启动 broker（docker compose up -d mosquitto）")
st.caption(f"常驻订阅已接收 {bus.message_count} 条消息 · 数据来源为真实设备与模拟器的统一报文")
st.divider()

refresh = getattr(st, "fragment", None)


def main_body() -> None:
    render_metrics()
    st.divider()
    left, right = st.columns([3, 2])
    with left:
        render_devices()
    with right:
        render_commands()
    st.divider()
    render_alerts()


if refresh is not None:
    refresh(run_every="2s")(main_body)()
else:
    if st.button("🔄 刷新"):
        st.rerun()
    main_body()
