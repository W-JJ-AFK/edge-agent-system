"""虚拟设备层：无硬件时提供与固件完全一致的 MQTT 报文。"""

from .base import IoTDevice
from .devices import EnvMonitor, MotorController, SmartLight, TempSensor, VibrationSensor

__all__ = [
    "IoTDevice",
    "TempSensor",
    "VibrationSensor",
    "MotorController",
    "SmartLight",
    "EnvMonitor",
]
