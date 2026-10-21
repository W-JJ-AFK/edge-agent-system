"""虚拟设备基类。

与固件端 main/net/protocol.c 使用同一套主题与字段，因此上层 Agent 完全不需要
区分「真实硬件」和「模拟器」——这正是把设备抽象成协议契约而不是实现的价值。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any

from .. import protocol
from ..bus import create_client

log = logging.getLogger(__name__)

ALERT_REPUBLISH_S = 30.0


class IoTDevice:
    #: 子类可覆盖的故障参数
    fault_probability: float = 0.0
    fault_length: float = 0.0
    alert_type: str = "device_fault"
    alert_value: Any = True
    alert_threshold: float | None = None
    alert_severity: str = "warning"

    def __init__(
        self,
        device_id: str,
        device_type: str,
        mqtt_broker: str = "localhost",
        mqtt_port: int = 1883,
    ) -> None:
        self.device_id = device_id
        self.device_type = device_type
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port

        self.topic_telemetry = protocol.telemetry_topic(device_id)
        self.topic_status = protocol.status_topic(device_id)
        self.topic_alert = protocol.alert_topic(device_id)
        self.topic_command = protocol.cmd_topic(device_id)
        self.topic_ack = protocol.ack_topic(device_id)

        self.telemetry_interval = 2.0
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._started_at = time.time()
        self._seq = 0
        self._alert_seq = 0

        self._fault_active = False
        self._fault_remaining = 0.0
        self._alert_episode_id: str | None = None
        self._last_alert_pub = 0.0

        self.client = create_client(device_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.will_set(
            self.topic_status,
            json.dumps({"device_id": device_id, "state": "offline", "timestamp": int(time.time())}),
            qos=1,
            retain=True,
        )

    # ---------------- 生命周期 ----------------

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    def connect(self) -> bool:
        try:
            self.client.connect(self.mqtt_broker, self.mqtt_port, 60)
            self.client.loop_start()
            return True
        except Exception as exc:
            log.error("%s connect failed: %s", self.device_id, exc)
            return False

    def stop(self) -> None:
        self._stop.set()

    def disconnect(self) -> None:
        self.stop()
        try:
            self.client.publish(
                self.topic_status,
                json.dumps(
                    {"device_id": self.device_id, "state": "offline", "timestamp": int(time.time())}
                ),
                qos=1,
                retain=True,
            )
            self.client.disconnect()
        except Exception as exc:
            log.debug("%s disconnect: %s", self.device_id, exc)
        finally:
            self.client.loop_stop()

    def run(self) -> None:
        log.info(
            "%s started (type=%s, interval=%.1fs)",
            self.device_id,
            self.device_type,
            self.telemetry_interval,
        )
        while self.running:
            try:
                self._tick_fault()
                if self._fault_active and time.time() - self._last_alert_pub >= ALERT_REPUBLISH_S:
                    self._publish_alert()

                self.publish_telemetry(self.generate_telemetry())
                time.sleep(self.telemetry_interval)
            except Exception:
                log.exception("%s loop error", self.device_id)
                time.sleep(1.0)

    # ---------------- 发布 ----------------

    def publish_telemetry(self, data: dict[str, Any]) -> None:
        payload = {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "timestamp": int(time.time()),
            # 与固件同样的字段：uptime_s 是开机秒数，永远存在；
            # timestamp 是 epoch（模拟器不需要 SNTP，所以直接给真值）。
            "uptime_s": int(time.time() - self._started_at),
            "seq": self._seq,
            "data": data,
        }
        self._seq += 1
        self.client.publish(
            self.topic_telemetry,
            json.dumps(payload, ensure_ascii=False),
            qos=0,
        )

    def _tick_fault(self) -> None:
        if self.fault_probability <= 0 or self.fault_length <= 0:
            return
        with self.lock:
            if not self._fault_active:
                if random.random() < self.fault_probability:
                    self._fault_active = True
                    self._fault_remaining = self.fault_length
                    self._alert_episode_id = None
                    self._publish_alert()
                return

            self._fault_remaining -= self.telemetry_interval
            if self._fault_remaining <= 0:
                self._fault_active = False
                self._alert_episode_id = None

    def _publish_alert(self) -> None:
        """同一故障周期内复用 alert_id，Agent 侧按 ID + 时间窗双重去重。"""
        if self._alert_episode_id is None:
            self._alert_seq += 1
            self._alert_episode_id = (
                f"{self.device_id}-{self.alert_type}-{int(time.time())}-{self._alert_seq}"
            )

        alert = {
            "device_id": self.device_id,
            "alert_id": self._alert_episode_id,
            "alert_type": self.alert_type,
            "value": self.current_alert_value(),
            "threshold": self.alert_threshold,
            "severity": self.alert_severity,
            "timestamp": int(time.time()),
        }
        self._last_alert_pub = time.time()
        self.client.publish(self.topic_alert, json.dumps(alert, ensure_ascii=False), qos=1)
        log.warning("%s ALERT %s = %s", self.device_id, self.alert_type, alert["value"])

    def current_alert_value(self) -> Any:
        return self.alert_value

    # ---------------- 指令 ----------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        code = int(getattr(reason_code, "value", reason_code) or 0)
        if code != 0:
            log.error("%s mqtt connect refused: %s", self.device_id, code)
            return
        client.subscribe(self.topic_command, qos=1)
        client.publish(
            self.topic_status,
            json.dumps(
                {"device_id": self.device_id, "state": "online", "timestamp": int(time.time())}
            ),
            qos=1,
            retain=True,
        )
        log.info("%s connected", self.device_id)

    def _on_message(self, client, userdata, msg) -> None:
        payload = protocol.decode(msg.payload)
        if payload is None:
            return

        command_id = str(payload.get("command_id") or "")
        # 与固件一致：过期指令拒绝执行。模拟器不校验会让"设备端校验"这条契约
        # 只在真机上成立，演示时永远发现不了。
        if protocol.is_expired(payload):
            ok, detail = False, "command expired"
        else:
            ok, detail = self.apply_command(payload)

        if command_id:
            ack = {
                "command_id": command_id,
                "result": "ok" if ok else "error",
                "detail": detail,
                "timestamp": int(time.time()),
            }
            client.publish(self.topic_ack, json.dumps(ack, ensure_ascii=False), qos=1)
        if not ok:
            log.warning("%s command rejected: %s", self.device_id, detail)

    def apply_command(self, command: dict[str, Any]) -> tuple[bool, str]:
        """返回 (是否成功, 说明)。子类覆写。"""
        action = command.get("action")
        if action == "set_interval":
            value = command.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, "参数非法"
            ms = float(value)
            # 与固件 app_main.c 的 set_interval 校验保持一致：500~60000 ms。
            # 只判正数会让模拟器"接受"固件会拒绝的参数，演示时看不出差异。
            if ms < 500 or ms > 60000:
                return False, "interval must be 500-60000 ms"
            with self.lock:
                self.telemetry_interval = ms / 1000.0
            return True, f"telemetry period={ms:.0f} ms"

        if action == "reboot":
            # 真机会 esp_restart()；模拟器只重置内部状态，并在回执里说清楚，
            # 避免让人误以为真的重启了。
            with self.lock:
                self._fault_active = False
                self._fault_remaining = 0.0
                self._alert_episode_id = None
                self._seq = 0
            return True, "rebooting in 500 ms（模拟器仅重置内部状态）"

        return False, f"unsupported action: {action}"

    # ---------------- 数据生成 ----------------

    def generate_telemetry(self) -> dict[str, Any]:
        raise NotImplementedError
