"""五类虚拟设备的具体实现。"""

from __future__ import annotations

import math
import random
from typing import Any

from .base import IoTDevice


class TempSensor(IoTDevice):
    fault_probability = 0.02
    fault_length = 5.0
    alert_type = "temp_high"
    alert_threshold = 38.0
    alert_severity = "critical"

    def __init__(self, device_id: str = "temp_sensor_01", **kwargs: Any) -> None:
        super().__init__(device_id, "temperature_sensor", **kwargs)
        self.telemetry_interval = 2.0
        self._base_temp = random.uniform(22, 28)

    def generate_telemetry(self) -> dict[str, Any]:
        with self.lock:
            if self._fault_active:
                temperature = 40 + random.gauss(0, 1)
            else:
                temperature = self._base_temp + random.gauss(0, 0.5)
                self._base_temp = max(
                    18.0, min(35.0, self._base_temp + random.gauss(0, 0.01))
                )
        humidity = max(30.0, min(90.0, 60 + random.gauss(0, 5)))
        return {"temperature": round(temperature, 1), "humidity": round(humidity, 1)}

    def current_alert_value(self) -> float:
        return round(40 + random.gauss(0, 1), 1)


class VibrationSensor(IoTDevice):
    fault_probability = 0.03
    fault_length = 3.0
    alert_type = "vibration_high"
    alert_threshold = 0.55
    alert_severity = "critical"

    def __init__(self, device_id: str = "vibration_01", **kwargs: Any) -> None:
        super().__init__(device_id, "vibration_sensor", **kwargs)
        self.telemetry_interval = 2.0
        self._rms = 0.06
        self.threshold = float(self.alert_threshold)

    def apply_command(self, command: dict[str, Any]) -> tuple[bool, str]:
        handled, detail = super().apply_command(command)
        if handled:
            return handled, detail

        if command.get("action") == "set_threshold":
            value = command.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, "阈值必须是数字"
            threshold = float(value)
            if not math.isfinite(threshold) or not 0.05 <= threshold <= 4.0:
                return False, "阈值需在 0.05~4.0 g"
            with self.lock:
                self.threshold = threshold
                # 与固件一致：退出阈值取进入阈值的 0.6 倍
                self.alert_threshold = threshold
            return True, f"threshold={threshold:.2f}g (release {threshold * 0.6:.2f}g)"

        return False, f"unsupported action: {command.get('action')}"

    def generate_telemetry(self) -> dict[str, Any]:
        with self.lock:
            if self._fault_active:
                rms = max(0.0, 0.62 + random.gauss(0, 0.05))
            else:
                rms = max(0.0, 0.06 + random.gauss(0, 0.015))
            self._rms = rms
        return {
            "rms": round(rms, 3),
            "peak": round(rms * random.uniform(2.0, 4.0), 3),
            "frequency": round(max(0.0, 50 + random.gauss(0, 2)), 1),
            "state": "ALARM" if self._fault_active else "NORMAL",
        }

    def current_alert_value(self) -> float:
        """告警值必须与故障状态一致。

        踩过的坑：`_publish_alert()` 在 `generate_telemetry()` 之前调用，
        此时 `_rms` 还是上一轮的正常值，于是告警里出现"振动超限 0.056（阈值 0.55）"
        这种自相矛盾的报文——数据看着像 bug，其实只是时序问题。
        """
        with self.lock:
            if self._fault_active:
                return round(0.62 + random.gauss(0, 0.05), 3)
            return round(self._rms, 3)


class MotorController(IoTDevice):
    fault_probability = 0.015
    fault_length = 4.0
    alert_type = "current_overload"
    alert_threshold = 5.0
    alert_severity = "critical"

    def __init__(self, device_id: str = "motor_01", **kwargs: Any) -> None:
        super().__init__(device_id, "motor_controller", **kwargs)
        self.telemetry_interval = 1.0
        self.target_speed = 1500.0
        self.current_speed = 1500.0
        self.default_speed = 1500.0

    def generate_telemetry(self) -> dict[str, Any]:
        with self.lock:
            self.current_speed += (self.target_speed - self.current_speed) * 0.3
            self.current_speed = max(0.0, self.current_speed + random.gauss(0, 10))
            speed = self.current_speed
            fault = self._fault_active

        current = 5.4 + random.gauss(0, 0.3) if fault else 2.0 + random.gauss(0, 0.2)
        return {
            "speed": round(speed),
            "current": round(max(0.0, current), 2),
            "temperature": round(40 + random.gauss(0, 2), 1),
        }

    def current_alert_value(self) -> float:
        return round(5.4 + random.gauss(0, 0.3), 2)

    def apply_command(self, command: dict[str, Any]) -> tuple[bool, str]:
        handled, detail = super().apply_command(command)
        if handled:
            return handled, detail

        action = command.get("action")
        value = command.get("value")

        if action == "set_speed":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, "转速必须是数字"
            speed = float(value)
            if not math.isfinite(speed) or speed < 0 or speed > 3000:
                return False, "转速需在 0~3000 RPM"
            with self.lock:
                self.target_speed = speed
            return True, f"目标转速 {speed:.0f} RPM"

        if action == "stop":
            with self.lock:
                self.target_speed = 0.0
            return True, "已停机"

        if action == "start":
            with self.lock:
                self.target_speed = self.default_speed
            return True, f"已启动，目标转速 {self.default_speed:.0f} RPM"

        return False, f"unsupported action: {action}"


class SmartLight(IoTDevice):
    def __init__(self, device_id: str = "light_01", **kwargs: Any) -> None:
        super().__init__(device_id, "smart_light", **kwargs)
        self.telemetry_interval = 3.0
        self.brightness = 80
        self.status = "on"

    def generate_telemetry(self) -> dict[str, Any]:
        with self.lock:
            status = self.status
            brightness = self.brightness
        power = 0.5 if status == "off" else 12.5 * (brightness / 100.0)
        return {
            "status": status,
            "brightness": brightness,
            "power": round(power, 1),
        }

    def apply_command(self, command: dict[str, Any]) -> tuple[bool, str]:
        handled, detail = super().apply_command(command)
        if handled:
            return handled, detail

        action = command.get("action")
        value = command.get("value")

        if action == "on":
            with self.lock:
                self.status = "on"
            return True, "已开灯"
        if action == "off":
            with self.lock:
                self.status = "off"
            return True, "已关灯"
        if action == "toggle":
            with self.lock:
                self.status = "off" if self.status == "on" else "on"
            return True, f"状态切换为 {self.status}"
        if action == "set_brightness":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, "亮度必须是数字"
            level = float(value)
            if not math.isfinite(level) or level < 0 or level > 100:
                return False, "亮度需在 0~100"
            with self.lock:
                self.brightness = int(round(level))
            return True, f"亮度 {self.brightness}%"

        return False, f"unsupported action: {action}"


class EnvMonitor(IoTDevice):
    fault_probability = 0.01
    fault_length = 8.0
    alert_type = "co2_high"
    alert_threshold = 1500.0
    alert_severity = "warning"

    def __init__(self, device_id: str = "env_01", **kwargs: Any) -> None:
        super().__init__(device_id, "env_monitor", **kwargs)
        self.telemetry_interval = 5.0

    def generate_telemetry(self) -> dict[str, Any]:
        with self.lock:
            fault = self._fault_active
        co2 = 2000 + random.gauss(0, 100) if fault else 800 + random.gauss(0, 50)
        return {
            "pm25": round(max(0.0, 30 + random.gauss(0, 5)), 1),
            "co2": round(max(0.0, co2)),
            "temperature": round(24 + random.gauss(0, 0.3), 1),
            "humidity": round(max(0.0, min(100.0, 55 + random.gauss(0, 3))), 1),
        }

    def current_alert_value(self) -> float:
        return round(2000 + random.gauss(0, 100))
