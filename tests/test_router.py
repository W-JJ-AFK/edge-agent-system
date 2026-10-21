"""意图路由测试：这是修复「LLM 短路规则引擎」的核心保障。"""

from __future__ import annotations

import pytest

from agent.registry import DeviceRegistry
from agent.router import Intent, IntentRouter


@pytest.fixture
def router(registry: DeviceRegistry) -> IntentRouter:
    return IntentRouter(registry)


@pytest.mark.parametrize(
    "text,device_id,action,value",
    [
        ("电机降速到 800", "motor_01", "set_speed", 800.0),
        ("把电机转速调到1200", "motor_01", "set_speed", 1200.0),
        ("关灯", "light_01", "off", None),
        ("开灯", "light_01", "on", None),
        ("把振动阈值调到0.6", "vibration_01", "set_threshold", 0.6),
        ("亮度调到50", "light_01", "set_brightness", 50.0),
        ("上报间隔改成5秒", "temp_sensor_01", "set_interval", 5000.0),
        ("上报间隔改成800毫秒", "temp_sensor_01", "set_interval", 800.0),
        ("电机停机", "motor_01", "stop", None),
    ],
)
def test_control_intents(router: IntentRouter, text, device_id, action, value):
    route = router.route(text)
    assert route.intent is Intent.CONTROL
    assert route.device_id == device_id
    assert route.action == action
    if value is None:
        assert route.value is None
    else:
        assert route.value == pytest.approx(value)


@pytest.mark.parametrize(
    "text,query",
    [
        ("有哪些设备", "list_devices"),
        ("查看所有设备", "list_devices"),
        ("扫描设备", "list_devices"),
        ("查看告警", "alerts"),
        ("现在有报警吗", "alerts"),
    ],
)
def test_query_intents(router: IntentRouter, text, query):
    route = router.route(text)
    assert route.intent is Intent.QUERY
    assert route.query == query


def test_query_telemetry_and_trend(router: IntentRouter):
    route = router.route("振动传感器的数据")
    assert route.intent is Intent.QUERY
    assert route.query == "telemetry"
    assert route.device_id == "vibration_01"

    trend_route = router.route("电机的趋势怎么样")
    assert trend_route.intent is Intent.QUERY
    assert trend_route.query == "trend"
    assert trend_route.device_id == "motor_01"


@pytest.mark.parametrize(
    "text,device_id",
    [
        ("振动传感器现在什么情况？", "vibration_01"),
        ("电机现在什么状况", "motor_01"),
        ("温度传感器咋样了", "temp_sensor_01"),
        ("振动传感器状态如何", "vibration_01"),
    ],
)
def test_colloquial_status_is_telemetry(router: IntentRouter, text, device_id):
    """「现在什么情况」是最自然的问法，必须落到 QUERY 而不是兜底 DIAGNOSE。

    这类说法没有"数据"两个字，早期会被漏掉，导致本该 0 成本的模板查询
    退化成一次 LLM 调用（或规则诊断报告），既慢又答非所问。
    """
    route = router.route(text)
    assert route.intent is Intent.QUERY, f"{text!r} 应该是查询"
    assert route.query == "telemetry"
    assert route.device_id == device_id


def test_diagnose_takes_priority_over_alert_query(router: IntentRouter):
    """「为什么…告警」必须走诊断，而不是被当成查询告警列表。"""
    route = router.route("为什么振动传感器告警了")
    assert route.intent is Intent.DIAGNOSE
    assert route.device_id == "vibration_01"


def test_diagnose_free_form_fallback(router: IntentRouter):
    route = router.route("温度传感器现在还好吗")
    assert route.intent is Intent.DIAGNOSE


@pytest.mark.parametrize("text", ["", "   ", "你好", "help", "能做什么"])
def test_help_intents(router: IntentRouter, text):
    assert router.route(text).intent is Intent.HELP


def test_control_beats_diagnose_keywords(router: IntentRouter):
    """带"为什么"但同时是明确指令时，控制优先——否则用户改不了参数。"""
    route = router.route("为什么这么吵，电机降速到600")
    assert route.intent is Intent.CONTROL
    assert route.action == "set_speed"
    assert route.value == 600.0


def test_unknown_device_control_falls_back_by_capability(router: IntentRouter):
    """没说设备名时，按动作找归属设备。"""
    route = router.route("调速到 900")
    assert route.intent is Intent.CONTROL
    assert route.device_id == "motor_01"
