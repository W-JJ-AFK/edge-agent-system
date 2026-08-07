"""Concrete implementations for five virtual IoT device types."""

from __future__ import annotations

import math
import random
from typing import Any, Mapping

from .base_device import IoTDevice


class TimedFaultDevice(IoTDevice):
    """Internal helper for devices that simulate timed random faults."""

    fault_probability = 0.0
    fault_length = 0.0
    fault_alert_type = "device_fault"
    fault_alert_value: Any = True
    fault_severity = "warning"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fault_duration = 0.0

    def inject_fault(self) -> None:
        with self.lock:
            if not self.fault_active:
                if random.random() < self.fault_probability:
                    self.fault_active = True
                    self.fault_duration = self.fault_length
                    self.publish_alert(
                        self.fault_alert_type,
                        self.fault_alert_value,
                        self.fault_severity,
                    )
                return

            self.fault_duration -= self.telemetry_interval
            if self.fault_duration <= 0:
                self.fault_active = False
                self.fault_duration = 0.0


class TempSensor(TimedFaultDevice):
    """Temperature and humidity sensor."""

    fault_probability = 0.02
    fault_length = 5.0
    fault_alert_type = "temp_high"
    fault_alert_value = 42.0
    fault_severity = "critical"

    def __init__(self, device_id: str = "temp_sensor_01", **kwargs: Any) -> None:
        super().__init__(device_id, "temperature_sensor", **kwargs)
        self.base_temp = random.uniform(22, 28)

    def generate_telemetry(self) -> Mapping[str, Any]:
        with self.lock:
            fault_active = self.fault_active
            if fault_active:
                temperature = 40 + random.gauss(0, 1)
            else:
                temperature = self.base_temp + random.gauss(0, 0.5)
                self.base_temp = max(18.0, min(35.0, self.base_temp + random.gauss(0, 0.01)))

        humidity = max(30.0, min(90.0, 60 + random.gauss(0, 5)))
        return {
            "temperature": round(temperature, 1),
            "humidity": round(humidity, 1),
        }


class VibrationSensor(TimedFaultDevice):
    """Vibration RMS, peak, and frequency sensor."""

    fault_probability = 0.03
    fault_length = 3.0
    fault_alert_type = "vibration_high"
    fault_alert_value = 0.55
    fault_severity = "critical"

    def __init__(self, device_id: str = "vibration_sensor_01", **kwargs: Any) -> None:
        super().__init__(device_id, "vibration_sensor", **kwargs)
        self.telemetry_interval = 0.5

    def generate_telemetry(self) -> Mapping[str, Any]:
        with self.lock:
            fault_active = self.fault_active

        if fault_active:
            rms = max(0.0, 0.5 + random.gauss(0, 0.1))
            peak = rms * 3
        else:
            rms = max(0.0, 0.05 + random.gauss(0.05, 0.02))
            peak = rms * random.uniform(2, 4)

        return {
            "rms": round(rms, 3),
            "peak": round(peak, 3),
            "frequency": round(max(0.0, 50 + random.gauss(0, 2)), 1),
        }


class MotorController(TimedFaultDevice):
    """Motor controller supporting speed and start/stop commands."""

    fault_probability = 0.015
    fault_length = 4.0
    fault_alert_type = "current_overload"
    fault_alert_value = 5.2
    fault_severity = "critical"

    def __init__(self, device_id: str = "motor_01", **kwargs: Any) -> None:
        super().__init__(device_id, "motor_controller", **kwargs)
        self.telemetry_interval = 1.0
        self.target_speed = 1500.0
        self.current_speed = 1500.0
        self.default_speed = 1500.0

    def generate_telemetry(self) -> Mapping[str, Any]:
        with self.lock:
            self.current_speed += (self.target_speed - self.current_speed) * 0.3
            self.current_speed = max(0.0, self.current_speed + random.gauss(0, 10))
            fault_active = self.fault_active

        current = 5.0 + random.gauss(0, 0.5) if fault_active else 2.0 + random.gauss(0, 0.2)
        return {
            "speed": round(self.current_speed),
            "current": round(max(0.0, current), 2),
            "temperature": round(40 + random.gauss(0, 2), 1),
        }

    def on_command(self, command: Mapping[str, Any]) -> None:
        super().on_command(command)
        action = command.get("action")

        if action == "set_speed":
            value = command.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                self.logger.warning("Ignored invalid target speed: %r", value)
                return
            speed = float(value)
            if not math.isfinite(speed) or speed < 0:
                self.logger.warning("Ignored invalid target speed: %r", value)
                return
            with self.lock:
                self.target_speed = speed
            self.logger.info("Target speed changed to %.0f RPM", speed)
        elif action == "stop":
            with self.lock:
                self.target_speed = 0.0
        elif action == "start":
            with self.lock:
                self.target_speed = self.default_speed


class SmartLight(IoTDevice):
    """Dimmable smart light."""

    def __init__(self, device_id: str = "smart_light_01", **kwargs: Any) -> None:
        super().__init__(device_id, "smart_light", **kwargs)
        self.telemetry_interval = 3.0
        self.brightness = 80
        self.status = "on"

    def generate_telemetry(self) -> Mapping[str, Any]:
        with self.lock:
            status = self.status
            brightness = self.brightness

        power = 0.5 if status == "off" else 12.5 * (brightness / 100)
        return {
            "status": status,
            "brightness": brightness,
            "power": round(power, 1),
        }

    def on_command(self, command: Mapping[str, Any]) -> None:
        super().on_command(command)
        action = command.get("action")

        with self.lock:
            if action == "on":
                self.status = "on"
            elif action == "off":
                self.status = "off"
            elif action == "toggle":
                self.status = "off" if self.status == "on" else "on"
            elif action == "set_brightness":
                value = command.get("value")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    self.logger.warning("Ignored invalid brightness: %r", value)
                    return
                if not math.isfinite(float(value)):
                    self.logger.warning("Ignored invalid brightness: %r", value)
                    return
                self.brightness = round(max(0.0, min(100.0, float(value))))


class EnvMonitor(TimedFaultDevice):
    """Environmental monitor for PM2.5, CO2, temperature, and humidity."""

    fault_probability = 0.01
    fault_length = 8.0
    fault_alert_type = "co2_high"
    fault_alert_value = 2050
    fault_severity = "warning"

    def __init__(self, device_id: str = "env_monitor_01", **kwargs: Any) -> None:
        super().__init__(device_id, "env_monitor", **kwargs)
        self.telemetry_interval = 5.0

    def generate_telemetry(self) -> Mapping[str, Any]:
        with self.lock:
            fault_active = self.fault_active

        co2 = 2000 + random.gauss(0, 100) if fault_active else 800 + random.gauss(0, 50)
        return {
            "pm25": round(max(0.0, 30 + random.gauss(0, 5)), 1),
            "co2": round(max(0.0, co2)),
            "temperature": round(24 + random.gauss(0, 0.3), 1),
            "humidity": round(max(0.0, min(100.0, 55 + random.gauss(0, 3))), 1),
        }

