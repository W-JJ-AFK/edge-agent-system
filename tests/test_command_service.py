"""指令服务测试：护栏、幂等、回执闭环、超时与失败路径。"""

from __future__ import annotations

import pytest

from agent.command_service import CommandService
from tests.conftest import FakePublisher


def test_success_closed_loop(commands: CommandService, publisher: FakePublisher):
    result = commands.send("motor_01", "set_speed", 800)

    assert result.status == "ok"
    assert result.ok
    assert result.latency_ms is not None
    assert result.latency_ms < 500

    assert len(publisher.published) == 1
    topic, payload, qos, retain = publisher.published[0]
    assert topic == "devices/motor_01/cmd"
    assert qos == 1
    assert retain is False
    assert '"set_speed"' in payload
    assert commands.pending_count == 0


def test_reject_unknown_device(commands: CommandService, publisher: FakePublisher):
    result = commands.send("not_exists", "off")
    assert result.status == "rejected"
    assert "未知设备" in result.detail
    assert publisher.published == []


def test_reject_unsupported_action(commands: CommandService):
    result = commands.send("motor_01", "fly")
    assert result.status == "rejected"
    assert "不支持动作" in result.detail


def test_reject_negative_speed(commands: CommandService):
    result = commands.send("motor_01", "set_speed", -100)
    assert result.status == "rejected"
    assert "超出范围" in result.detail


def test_reject_bool_value(commands: CommandService):
    result = commands.send("motor_01", "set_speed", True)
    assert result.status == "rejected"
    assert "布尔" in result.detail


def test_reject_nan(commands: CommandService):
    result = commands.send("motor_01", "set_speed", float("nan"))
    assert result.status == "rejected"
    assert "NaN" in result.detail or "非法" in result.detail


def test_reject_missing_value(commands: CommandService):
    result = commands.send("motor_01", "set_speed", None)
    assert result.status == "rejected"
    assert "缺少参数" in result.detail


def test_timeout_when_no_ack(registry, publisher: FakePublisher):
    publisher.auto_ack = False
    service = CommandService(publisher, registry, timeout_s=0.1)
    publisher.command_service = service

    result = service.send("motor_01", "stop")
    assert result.status == "timeout"
    assert "未收到" in result.detail
    assert service.pending_count == 0


def test_failure_result_maps_to_error(registry, publisher: FakePublisher):
    publisher.result = "error"
    publisher.detail = "设备忙"
    service = CommandService(publisher, registry, timeout_s=0.5)
    publisher.command_service = service

    result = service.send("light_01", "on")
    assert result.status == "error"
    assert result.detail == "设备忙"


def test_publish_exception_returns_error(commands: CommandService, publisher: FakePublisher):
    publisher.raise_on_publish = ConnectionError("MQTT 未连接")
    result = commands.send("motor_01", "stop")
    assert result.status == "error"
    assert "指令发送失败" in result.detail
    assert commands.pending_count == 0


def test_handle_ack_for_unknown_command(commands: CommandService):
    assert commands.handle_ack({"command_id": "nope", "result": "ok"}) is False
    assert commands.handle_ack({}) is False


def test_fire_and_forget(commands: CommandService):
    result = commands.send("light_01", "on", wait=False)
    assert result.status == "pending"
    # 迟到的回执不应抛异常
    assert commands.handle_ack({"command_id": result.command_id, "result": "ok"}) is True
