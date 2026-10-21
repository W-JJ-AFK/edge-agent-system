"""报文契约测试。"""

from __future__ import annotations

import json
import time

import pytest

from agent import protocol


def test_topic_builders():
    assert protocol.telemetry_topic("motor_01") == "devices/motor_01/telemetry"
    assert protocol.status_topic("motor_01") == "devices/motor_01/status"
    assert protocol.alert_topic("motor_01") == "devices/motor_01/alert"
    assert protocol.cmd_topic("motor_01") == "devices/motor_01/cmd"
    assert protocol.ack_topic("motor_01") == "devices/motor_01/cmd/ack"


@pytest.mark.parametrize(
    "topic,device_id,kind",
    [
        ("devices/temp_sensor_01/telemetry", "temp_sensor_01", "telemetry"),
        ("devices/vibration_01/alert", "vibration_01", "alert"),
        ("devices/motor_01/status", "motor_01", "status"),
        ("devices/motor_01/cmd", "motor_01", "cmd"),
        ("devices/motor_01/cmd/ack", "motor_01", "ack"),
    ],
)
def test_parse_topic(topic, device_id, kind):
    parsed = protocol.parse_topic(topic)
    assert parsed is not None
    assert parsed.device_id == device_id
    assert parsed.kind == kind


@pytest.mark.parametrize(
    "topic",
    ["", "devices", "devices/x", "sensors/x/telemetry", "devices/x/unknown", "devices/x/cmd/other"],
)
def test_parse_topic_rejects(topic):
    assert protocol.parse_topic(topic) is None


def test_decode_accepts_bytes_and_str():
    raw = b'{"device_id":"a"}'
    assert protocol.decode(raw) == {"device_id": "a"}
    assert protocol.decode('{"device_id":"a"}') == {"device_id": "a"}


@pytest.mark.parametrize("bad", [b"\xff\xfe", "not json", "[1,2,3]", b"", "123"])
def test_decode_rejects(bad):
    assert protocol.decode(bad) is None


def test_build_command_contains_required_fields():
    command_id, payload = protocol.build_command("set_speed", 800)
    data = json.loads(payload)
    assert command_id.startswith("cmd-")
    assert data["command_id"] == command_id
    assert data["action"] == "set_speed"
    assert data["value"] == 800
    assert "expires_at" in data


def test_build_command_omits_value_when_none():
    _, payload = protocol.build_command("off")
    assert "value" not in json.loads(payload)


def test_command_expiry():
    _, payload = protocol.build_command("off", expires_in_s=10)
    data = json.loads(payload)
    assert not protocol.is_expired(data)
    assert protocol.is_expired(data, now=time.time() + 60)
