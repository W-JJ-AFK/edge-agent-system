"""MQTT 总线封装。

只做三件事：兼容 paho 1.x/2.x 建客户端、订阅四类主题、把消息交给上层回调。
业务逻辑一律不写在这里，方便用假实现替换（测试时无需真实 broker）。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

import paho.mqtt.client as mqtt

from . import protocol
from .config import MqttConfig

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, dict[str, Any]], None]


def create_client(client_id: str) -> mqtt.Client:
    """paho-mqtt 2.x 要求显式传入回调 API 版本，1.x 没有该参数。"""
    version = getattr(mqtt, "CallbackAPIVersion", None)
    if version is not None:
        return mqtt.Client(callback_api_version=version.VERSION2, client_id=client_id)
    return mqtt.Client(client_id=client_id)


class MqttBus:
    def __init__(self, config: MqttConfig, on_message: MessageHandler | None = None) -> None:
        self._config = config
        self.on_message = on_message
        self._client = create_client(config.client_id)
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._connected = threading.Event()
        self._stopping = False

    # ---------------- 生命周期 ----------------

    def start(self, wait: bool = True, timeout: float = 5.0) -> bool:
        """启动网络循环；broker 暂时不可用也不返回失败，交给 paho 自动重连。

        早期实现是 ``connect()`` 成功之后才 ``loop_start()``：一旦 broker 没起
        或者比 Agent 晚起（docker compose 里很常见），``connect()`` 抛异常，
        网络循环根本没启动，于是 paho 的自动重连也无从谈起——
        进程一直"活着但永远连不上"，只能重启 Agent。
        改成 ``connect_async()`` + ``loop_start()`` 之后，连接动作在循环线程里
        反复重试，返回值只表示"当前是否已经连上"。
        """
        self._client.reconnect_delay_set(min_delay=1, max_delay=10)
        try:
            self._client.connect_async(self._config.host, self._config.port,
                                      self._config.keepalive)
        except Exception as exc:  # 非法 host/port 之类的参数错误仍然要报出来
            log.error("MQTT connect_async failed: %s", exc)
            return False
        self._client.loop_start()
        if wait:
            connected = self._connected.wait(timeout)
            if not connected:
                log.warning(
                    "MQTT not connected within %.1fs (%s:%s), retrying in background",
                    timeout, self._config.host, self._config.port,
                )
            return connected
        return True

    def stop(self) -> None:
        self._stopping = True
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ---------------- 发布 ----------------

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> None:
        if not self.connected:
            raise ConnectionError("MQTT 未连接，指令无法下发")
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT 发布失败，rc={info.rc}")

    # ---------------- 回调 ----------------

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any,
                    reason_code: Any, properties: Any = None) -> None:
        code = int(getattr(reason_code, "value", reason_code) or 0)
        if code != 0:
            log.error("MQTT connect refused, code=%s", code)
            return
        self._connected.set()
        for topic_filter in (
            protocol.TELEMETRY_FILTER,
            protocol.ALERT_FILTER,
            protocol.STATUS_FILTER,
            protocol.ACK_FILTER,
        ):
            client.subscribe(topic_filter, qos=1)
        log.info("MQTT connected to %s:%s", self._config.host, self._config.port)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, *args: Any) -> None:
        """paho 1.x 与 2.x 的回调签名不同，这里统一兼容。

        * paho 1.x：``on_disconnect(client, userdata, rc)`` → args == (rc,)
        * paho 2.x：``on_disconnect(client, userdata, flags, reason_code, properties)``

        早期实现把第三个形参命名为 ``flags``，于是在 1.x 上 ``rc`` 落进了 flags、
        ``reason_code`` 恒为默认值 0，日志永远是 ``code=0``，排查断线原因时毫无信息。
        """
        self._connected.clear()
        code: Any = args[1] if len(args) >= 2 else (args[0] if args else 0)
        code = getattr(code, "value", code)
        if self._stopping:
            # 主动退出：不该用 WARNING 吓人，否则每次正常结束都像出了故障
            log.debug("MQTT disconnected (shutdown)")
            return
        log.warning("MQTT disconnected (code=%s), auto-reconnect enabled", code)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        payload = protocol.decode(msg.payload)
        if payload is None:
            log.debug("ignored non-JSON payload on %s", msg.topic)
            return
        handler = self.on_message
        if handler is None:
            return
        try:
            handler(msg.topic, payload)
        except Exception:
            log.exception("message handler failed for topic %s", msg.topic)
