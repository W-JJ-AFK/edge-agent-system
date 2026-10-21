"""引擎集成测试：消息分发、三条意图通道、LLM 降级。"""

from __future__ import annotations

import time

import pytest

from agent.cache import DeviceCache
from agent.command_service import CommandService
from agent.config import AgentConfig
from agent.engine import AgentEngine
from agent.registry import DeviceRegistry
from agent.storage.db import Store
from tests.conftest import FakePublisher


class _UnavailableLlm:
    available = False


@pytest.fixture
def engine(config: AgentConfig, cache: DeviceCache, registry: DeviceRegistry,
           commands: CommandService) -> AgentEngine:
    return AgentEngine(config, cache, registry, commands, llm=None, store=None)


def _feed_telemetry(engine: AgentEngine, device_id="vibration_01", data=None):
    engine.handle_message(
        f"devices/{device_id}/telemetry",
        {
            "device_id": device_id,
            "device_type": "vibration_sensor",
            "timestamp": int(time.time()),
            "seq": 1,
            "data": data or {"rms": 0.06, "temperature": 41.0},
        },
    )


def test_telemetry_updates_cache_and_db(config, cache, registry, commands, tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    engine = AgentEngine(config, cache, registry, commands, llm=None, store=store)
    try:
        _feed_telemetry(engine)
        state = cache.get("vibration_01")
        assert state is not None
        assert state.last_data["rms"] == 0.06
        assert store.stats()["telemetry_points"] == 2
    finally:
        store.close()


def test_alert_ingest_and_query(engine: AgentEngine, cache: DeviceCache):
    engine.handle_message(
        "devices/vibration_01/alert",
        {
            "device_id": "vibration_01",
            "alert_id": "vib-1",
            "alert_type": "vibration_high",
            "value": 0.62,
            "threshold": 0.55,
            "severity": "critical",
            "timestamp": int(time.time()),
        },
    )
    assert len(cache.recent_alerts()) == 1

    answer = engine.chat("查看告警")
    assert "vibration_high" in answer
    assert "vibration_01" in answer


def test_list_devices_query(engine: AgentEngine):
    _feed_telemetry(engine)
    answer = engine.chat("有哪些设备")
    assert "vibration_01" in answer
    assert "motor_01" in answer


def test_control_command_executes(engine: AgentEngine, publisher: FakePublisher):
    answer = engine.chat("电机降速到 800")
    assert "✅" in answer
    assert "set_speed" in answer

    sent = publisher.commands()
    assert len(sent) == 1
    assert sent[0]["action"] == "set_speed"
    assert sent[0]["value"] == 800


def test_control_rejected_by_guardrail(engine: AgentEngine, publisher: FakePublisher):
    answer = engine.chat("电机降速到 99999")
    assert "🚫" in answer
    assert publisher.commands() == []


def test_diagnose_falls_back_to_rules(engine: AgentEngine):
    _feed_telemetry(engine)
    engine.handle_message(
        "devices/vibration_01/alert",
        {
            "device_id": "vibration_01",
            "alert_id": "vib-2",
            "alert_type": "vibration_high",
            "value": 0.62,
            "threshold": 0.55,
            "severity": "critical",
        },
    )
    answer = engine.chat("为什么振动传感器告警了")
    assert "诊断报告" in answer
    assert "轴承磨损" in answer  # 知识库内容被引用
    assert "风险等级：高" in answer


def test_degradation_notice_when_llm_configured_but_down(cache, registry, commands):
    config = AgentConfig()
    config.llm.enabled = True
    engine = AgentEngine(config, cache, registry, commands, llm=_UnavailableLlm(), store=None)
    answer = engine.chat("为什么温度告警了")
    assert "已自动切换到规则引擎" in answer


def test_ack_routing_closes_the_loop(engine: AgentEngine, commands: CommandService):
    # 直接投递一个 ack，不应抛异常
    engine.handle_message("devices/motor_01/cmd/ack", {"command_id": "unknown", "result": "ok"})
    assert commands.pending_count == 0


def test_help_intent(engine: AgentEngine):
    answer = engine.chat("你好")
    assert "设备运维 Agent" in answer
