"""模拟器与能力声明的一致性测试。

这类测试的存在理由：`CAPABILITIES` 说"振动传感器支持 set_threshold"，
固件 `app_main.c` 也实现了，但模拟器没实现 —— 于是演示时 Agent 下发成功、
设备回 `unsupported action`，看起来像 Agent 的 bug。

声明、固件、模拟器三者只要有一处掉队，就必须被测试抓住。
"""

from __future__ import annotations

import pytest

from agent.config import CAPABILITIES
from agent.simulator.devices import (
    EnvMonitor,
    MotorController,
    SmartLight,
    TempSensor,
    VibrationSensor,
)

# 设备类型 → 模拟器实现类（与 agent/simulator/runner.py 的 FACTORY 对应）
IMPL = {
    "temperature_sensor": TempSensor,
    "vibration_sensor": VibrationSensor,
    "motor_controller": MotorController,
    "smart_light": SmartLight,
    "env_monitor": EnvMonitor,
}


def _make(device_type: str):
    return IMPL[device_type](device_id=f"bench_{device_type}")


def _sample_value(bounds):
    """给带范围的动作挑一个合法值。"""
    if bounds is None:
        return None
    low, high = bounds
    return (low + high) / 2


@pytest.mark.parametrize("device_type", sorted(CAPABILITIES))
def test_simulator_implements_every_declared_action(device_type):
    device = _make(device_type)
    for action, bounds in CAPABILITIES[device_type].items():
        value = _sample_value(bounds)
        ok, detail = device.apply_command({"action": action, "value": value})
        assert ok, f"{device_type} 未实现 {action}（回执：{detail}）"
        assert "unsupported" not in detail.lower()


@pytest.mark.parametrize("device_type", sorted(CAPABILITIES))
def test_simulator_rejects_out_of_range(device_type):
    """越界参数必须被设备侧拒绝，而不是"接受然后忽略"。"""
    device = _make(device_type)
    for action, bounds in CAPABILITIES[device_type].items():
        if bounds is None:
            continue
        low, high = bounds
        ok, _ = device.apply_command({"action": action, "value": high * 10 + 1})
        assert not ok, f"{device_type}.{action} 应拒绝越界值 {high * 10 + 1}"


def test_simulator_rejects_unknown_action():
    device = _make("vibration_sensor")
    ok, detail = device.apply_command({"action": "self_destruct"})
    assert not ok
    assert "unsupported" in detail


def test_set_threshold_changes_alert_threshold():
    """调阈值必须真的影响后续告警报文的 threshold 字段，否则只是"回执好看"。"""
    device = _make("vibration_sensor")
    ok, detail = device.apply_command({"action": "set_threshold", "value": 0.8})
    assert ok
    assert "0.80g" in detail
    assert device.alert_threshold == 0.8
