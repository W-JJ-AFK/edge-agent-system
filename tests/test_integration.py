"""端到端联调测试（纯逻辑层，不需要 broker 与硬件）。

把真实模拟器设备接到假总线上，验证：
    用户自然语言 -> 意图路由 -> 指令报文 -> 设备执行 -> 回执 -> 用户可见结果
这条链路一旦断了，"控制闭环"就只是 PPT。
"""

from __future__ import annotations

import json
import threading

import pytest

from agent import protocol
from agent.cache import DeviceCache
from agent.command_service import CommandService
from agent.config import AgentConfig
from agent.engine import AgentEngine
from agent.registry import DeviceRegistry
from agent.simulator.devices import MotorController, SmartLight, VibrationSensor


class LoopbackPublisher:
    """把指令直接投递给真实模拟器设备，再把设备回执送回 CommandService。"""

    def __init__(self) -> None:
        self.devices: dict[str, object] = {}
        self.command_service: CommandService | None = None
        self.published: list[tuple[str, str]] = []

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> None:
        self.published.append((topic, payload))
        parsed = protocol.parse_topic(topic)
        if parsed is None or parsed.kind != "cmd":
            return

        device = self.devices.get(parsed.device_id)
        if device is None:
            return

        command = json.loads(payload)
        ok, detail = device.apply_command(command)  # type: ignore[attr-defined]

        def _ack() -> None:
            if self.command_service is not None:
                self.command_service.handle_ack(
                    {
                        "command_id": command["command_id"],
                        "result": "ok" if ok else "error",
                        "detail": detail,
                    }
                )

        timer = threading.Timer(0.01, _ack)
        timer.daemon = True
        timer.start()


@pytest.fixture
def loopback():
    config = AgentConfig()
    config.llm.enabled = False
    registry = DeviceRegistry(config.devices)
    cache = DeviceCache(ttl_by_device=registry.ttl_map(3))

    publisher = LoopbackPublisher()
    commands = CommandService(publisher, registry, timeout_s=1.0)
    publisher.command_service = commands

    motor = MotorController()
    light = SmartLight()
    vibration = VibrationSensor()
    publisher.devices = {
        "motor_01": motor,
        "light_01": light,
        "vibration_01": vibration,
    }

    engine = AgentEngine(config, cache, registry, commands, llm=None, store=None)
    return engine, publisher, motor, light, vibration


def test_speed_command_reaches_real_device(loopback):
    engine, _publisher, motor, _light, _vib = loopback

    answer = engine.chat("电机降速到 800")

    assert motor.target_speed == 800.0
    assert "✅" in answer
    assert "目标转速 800 RPM" in answer


def test_light_command_reaches_real_device(loopback):
    engine, _p, _motor, light, _vib = loopback

    engine.chat("关灯")
    assert light.status == "off"

    engine.chat("开灯")
    assert light.status == "on"


def test_threshold_command_reaches_real_device(loopback):
    engine, publisher, _motor, _light, _vib = loopback

    engine.chat("把振动阈值调到0.6")
    sent = [json.loads(p) for t, p in publisher.published if t.endswith("/cmd")]
    assert sent and sent[-1]["action"] == "set_threshold"
    assert sent[-1]["value"] == 0.6


def test_out_of_range_rejected_before_device(loopback):
    engine, publisher, motor, _light, _vib = loopback

    answer = engine.chat("电机降速到 99999")

    assert motor.target_speed == 1500.0  # 设备状态未被破坏
    assert publisher.published == []      # 报文根本没发出去
    assert "🚫" in answer


def test_telemetry_alert_diagnosis_chain(loopback):
    engine, _publisher, _motor, _light, vibration = loopback

    # 1) 设备产生遥测 -> 引擎入库
    data = vibration.generate_telemetry()
    engine.handle_message(
        "devices/vibration_01/telemetry",
        {
            "device_id": "vibration_01",
            "device_type": "vibration_sensor",
            "seq": 1,
            "data": data,
        },
    )
    assert "📈" in engine.chat("振动传感器的数据")

    # 2) 设备告警 -> 引擎入库
    engine.handle_message(
        "devices/vibration_01/alert",
        {
            "device_id": "vibration_01",
            "alert_id": "loopback-1",
            "alert_type": "vibration_high",
            "value": 0.62,
            "threshold": 0.55,
            "severity": "critical",
        },
    )

    # 3) 诊断引用真实数据与知识库
    answer = engine.chat("为什么振动传感器告警了")
    assert "vibration_high" in answer
    assert "0.62" in answer
    assert "风险等级：高" in answer
