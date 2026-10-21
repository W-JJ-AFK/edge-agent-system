"""配置加载与设备能力表。

设备能力（CAPABILITIES）是「护栏」的数据来源：LLM 或规则引擎想下发指令时，
先在这里查设备类型支持哪些 action、参数范围是多少。把校验数据放在配置层，
而不是散落在业务代码的 if 里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: 每种设备类型支持的控制动作；值为 None 表示无参数，(min, max) 表示参数范围
CAPABILITIES: dict[str, dict[str, tuple[float, float] | None]] = {
    "temperature_sensor": {
        "set_interval": (500.0, 60000.0),
        "reboot": None,
    },
    "vibration_sensor": {
        "set_threshold": (0.05, 4.0),  # 单位 g
        "set_interval": (500.0, 60000.0),
        "reboot": None,
    },
    "motor_controller": {
        "set_speed": (0.0, 3000.0),  # 单位 RPM
        "stop": None,
        "start": None,
    },
    "smart_light": {
        "on": None,
        "off": None,
        "toggle": None,
        "set_brightness": (0.0, 100.0),
    },
    "env_monitor": {"set_interval": (500.0, 60000.0)},
}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

_DEFAULT_DEVICES: dict[str, tuple[str, float]] = {
    # device_id: (device_type, interval_s)
    "temp_sensor_01": ("temperature_sensor", 2.0),
    "vibration_01": ("vibration_sensor", 2.0),
    "motor_01": ("motor_controller", 1.0),
    "light_01": ("smart_light", 3.0),
    "env_01": ("env_monitor", 5.0),
}


@dataclass(frozen=True)
class DeviceSpec:
    device_id: str
    device_type: str
    interval_s: float = 2.0
    source: str = "simulator"  # simulator | hardware

    @property
    def capabilities(self) -> dict[str, tuple[float, float] | None]:
        return CAPABILITIES.get(self.device_type, {})


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    client_id: str = "devops-agent"
    keepalive: int = 30
    username: str | None = None
    password: str | None = None


@dataclass
class LlmConfig:
    enabled: bool = True
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:3b"
    timeout_s: float = 45.0
    temperature: float = 0.1
    max_tool_rounds: int = 3


@dataclass
class StorageConfig:
    path: str = "data/agent.db"


@dataclass
class AgentConfig:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    devices: dict[str, DeviceSpec] = field(default_factory=dict)
    offline_after_periods: int = 3
    command_timeout_s: float = 10.0
    telemetry_retention_days: float = 7.0
    alert_retention_days: float = 90.0

    def __post_init__(self) -> None:
        """没有外部配置时也保证有一套可用的默认设备清单。"""
        if not self.devices:
            self.devices = {
                device_id: DeviceSpec(
                    device_id=device_id, device_type=dtype, interval_s=interval
                )
                for device_id, (dtype, interval) in _DEFAULT_DEVICES.items()
            }

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AgentConfig":
        """从 YAML 加载，缺失项走默认值，环境变量优先级最高。"""
        config = cls()
        config.devices = {
            device_id: DeviceSpec(device_id=device_id, device_type=dtype, interval_s=interval)
            for device_id, (dtype, interval) in _DEFAULT_DEVICES.items()
        }

        candidate = Path(path) if path else DEFAULT_CONFIG_PATH
        if candidate.is_file():
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            config._apply(raw)

        config._apply_env()
        return config

    def _apply(self, raw: dict[str, Any]) -> None:
        mqtt = raw.get("mqtt") or {}
        for key in ("host", "port", "client_id", "keepalive", "username", "password"):
            if key in mqtt and mqtt[key] is not None:
                setattr(self.mqtt, key, mqtt[key])

        llm = raw.get("llm") or {}
        for key in ("enabled", "base_url", "model", "timeout_s", "temperature", "max_tool_rounds"):
            if key in llm and llm[key] is not None:
                setattr(self.llm, key, llm[key])

        storage = raw.get("storage") or {}
        if storage.get("path"):
            self.storage.path = str(storage["path"])

        agent = raw.get("agent") or {}
        if agent.get("offline_after_periods") is not None:
            self.offline_after_periods = int(agent["offline_after_periods"])
        if agent.get("command_timeout_s") is not None:
            self.command_timeout_s = float(agent["command_timeout_s"])
        if agent.get("telemetry_retention_days") is not None:
            self.telemetry_retention_days = float(agent["telemetry_retention_days"])
        if agent.get("alert_retention_days") is not None:
            self.alert_retention_days = float(agent["alert_retention_days"])

        devices = raw.get("devices") or {}
        if devices:
            parsed: dict[str, DeviceSpec] = {}
            for device_id, spec in devices.items():
                parsed[str(device_id)] = DeviceSpec(
                    device_id=str(device_id),
                    device_type=str(spec.get("type", "unknown")),
                    interval_s=float(spec.get("interval_s", 2.0)),
                    source=str(spec.get("source", "simulator")),
                )
            self.devices = parsed

    def _apply_env(self) -> None:
        env = os.environ
        if env.get("AGENT_MQTT_HOST"):
            self.mqtt.host = env["AGENT_MQTT_HOST"]
        if env.get("AGENT_MQTT_PORT"):
            self.mqtt.port = int(env["AGENT_MQTT_PORT"])
        if env.get("AGENT_LLM_BASE_URL"):
            self.llm.base_url = env["AGENT_LLM_BASE_URL"]
        if env.get("AGENT_LLM_MODEL"):
            self.llm.model = env["AGENT_LLM_MODEL"]
        if env.get("AGENT_LLM_DISABLED") in {"1", "true", "yes"}:
            self.llm.enabled = False
        if env.get("AGENT_DB_PATH"):
            self.storage.path = env["AGENT_DB_PATH"]

    def hardware_devices(self) -> list[str]:
        return [d.device_id for d in self.devices.values() if d.source == "hardware"]

    def simulator_devices(self) -> list[DeviceSpec]:
        return [d for d in self.devices.values() if d.source != "hardware"]
