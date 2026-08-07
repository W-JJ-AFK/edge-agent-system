"""
IoT device base class.

All virtual devices inherit from this class and share a common MQTT,
telemetry, alert, command, and lifecycle interface.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Mapping

import paho.mqtt.client as mqtt


class IoTDevice:
    """Base class for an MQTT-connected virtual IoT device."""

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

        self.topic_telemetry = f"devices/{device_id}/telemetry"
        self.topic_command = f"devices/{device_id}/command"
        self.topic_alert = "devices/alerts"

        self.telemetry_interval = 2.0
        self.lock = threading.RLock()
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()

        self.client = self._create_mqtt_client()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.logger = logging.getLogger(device_id)
        self.fault_active = False
        self.fault_timer = 0.0

    @property
    def is_running(self) -> bool:
        return not self._stop_event.is_set()

    @is_running.setter
    def is_running(self, value: bool) -> None:
        if value:
            self._stop_event.clear()
        else:
            self._stop_event.set()

    def _create_mqtt_client(self) -> mqtt.Client:
        """Create a client compatible with paho-mqtt 1.x and 2.x."""
        callback_version = getattr(mqtt, "CallbackAPIVersion", None)
        if callback_version is not None:
            return mqtt.Client(
                callback_api_version=callback_version.VERSION2,
                client_id=self.device_id,
            )
        return mqtt.Client(client_id=self.device_id)

    @staticmethod
    def _reason_code_value(reason_code: Any) -> int:
        try:
            return int(reason_code)
        except (TypeError, ValueError):
            return int(getattr(reason_code, "value", -1))

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        """Handle a successful or failed broker connection."""
        code = self._reason_code_value(reason_code)
        if code == 0:
            self._connected_event.set()
            result, _ = client.subscribe(self.topic_command, qos=1)
            if result == mqtt.MQTT_ERR_SUCCESS:
                self.logger.info("Device %s connected to MQTT broker", self.device_id)
            else:
                self.logger.error("Command subscription failed, result code: %s", result)
        else:
            self._connected_event.clear()
            self.logger.error("Connection failed, reason code: %s", reason_code)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any = None,
        reason_code: Any = 0,
        properties: Any = None,
    ) -> None:
        """Track broker disconnections for both callback API versions."""
        self._connected_event.clear()
        if self._reason_code_value(reason_code) != 0 and self.is_running:
            self.logger.warning("Unexpected MQTT disconnection: %s", reason_code)

    def _on_message(
        self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage
    ) -> None:
        """Decode and dispatch a JSON command."""
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("command payload must be a JSON object")
            self.logger.info("Received command: %s", payload)
            self.on_command(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            self.logger.error("Invalid command on %s: %s", msg.topic, exc)
        except Exception:
            self.logger.exception("Command handling failed")

    def connect(self) -> bool:
        """Connect to the configured MQTT broker and start its network loop."""
        try:
            result = self.client.connect(self.mqtt_broker, self.mqtt_port, 60)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self.logger.error("Connection request failed, result code: %s", result)
                return False
            self.client.loop_start()
            return True
        except Exception as exc:
            self.logger.error("Connection failed: %s", exc)
            return False

    def disconnect(self) -> None:
        """Stop the device and disconnect it from MQTT."""
        self.is_running = False
        try:
            self.client.disconnect()
        except Exception as exc:
            self.logger.warning("MQTT disconnect failed: %s", exc)
        finally:
            self.client.loop_stop()
            self._connected_event.clear()
        self.logger.info("Device %s disconnected", self.device_id)

    def _publish(self, topic: str, payload: Mapping[str, Any], qos: int = 0) -> bool:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        info = self.client.publish(topic, encoded, qos=qos)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            self.logger.warning("Publish to %s failed, result code: %s", topic, info.rc)
            return False
        return True

    def publish_telemetry(self, data: Mapping[str, Any]) -> bool:
        """Publish one telemetry sample."""
        payload = {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "timestamp": int(time.time()),
            "data": dict(data),
        }
        return self._publish(self.topic_telemetry, payload)

    def publish_alert(
        self, alert_type: str, value: Any, severity: str = "warning"
    ) -> bool:
        """Publish a device alert to the shared alert topic."""
        alert = {
            "device_id": self.device_id,
            "device_type": self.device_type,
            "alert_type": alert_type,
            "value": value,
            "severity": severity,
            "timestamp": int(time.time()),
        }
        published = self._publish(self.topic_alert, alert, qos=1)
        self.logger.warning("Alert: %s = %s", alert_type, value)
        return published

    def on_command(self, command: Mapping[str, Any]) -> None:
        """Handle commands common to all devices."""
        if command.get("action") != "set_interval":
            return

        value = command.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.logger.warning("Ignored invalid telemetry interval: %r", value)
            return

        interval = float(value)
        if not math.isfinite(interval) or interval <= 0:
            self.logger.warning("Ignored invalid telemetry interval: %r", value)
            return

        with self.lock:
            self.telemetry_interval = interval
        self.logger.info("Telemetry interval changed to %.3fs", interval)

    def generate_telemetry(self) -> Mapping[str, Any]:
        """Generate one telemetry sample. Subclasses must implement this."""
        raise NotImplementedError

    def inject_fault(self) -> None:
        """Optionally update a simulated fault state."""

    def run(self) -> None:
        """Run the device loop until :meth:`disconnect` is called."""
        self.is_running = True
        self.logger.info("Device %s started", self.device_id)

        while self.is_running:
            try:
                data = self.generate_telemetry()
                self.publish_telemetry(data)
                self.inject_fault()

                with self.lock:
                    interval = self.telemetry_interval
                self._stop_event.wait(interval)
            except Exception:
                self.logger.exception("Device loop failed")
                self._stop_event.wait(1.0)

