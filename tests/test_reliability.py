"""可靠性回归测试：每条对应代码审查报告里的一条 P0/P1 缺陷。

这些用例的价值不在"覆盖代码"，而在**锁住语义**：修复被后续改动悄悄回退时，
测试会立刻变红。每条都在 docstring 里写清"修复前是什么行为"。
"""

from __future__ import annotations

import json
import time

import pytest

from agent import protocol
from agent.cache import DeviceCache
from agent.cli import _wait_for_data
from agent.command_service import CommandService
from agent.config import AgentConfig, LlmConfig
from agent.engine import AgentEngine
from agent.llm.client import OllamaClient
from agent.registry import DeviceRegistry
from agent.router import Intent
from agent.simulator.devices import VibrationSensor
from tests.conftest import FakePublisher


@pytest.fixture
def engine(config: AgentConfig, cache: DeviceCache, registry: DeviceRegistry,
           commands: CommandService) -> AgentEngine:
    return AgentEngine(config, cache, registry, commands, llm=None, store=None)


# ================= P0-3 时基 =================


def test_uptime_timestamp_is_distrusted():
    """设备发开机秒数（如 3600）时，不能当成 1970 年的 epoch 落库。

    修复前：db.insert_alert 直接 int(payload["timestamp"])，真机告警全变成 1970。
    """
    now = time.time()
    assert protocol.resolve_timestamp({"timestamp": 3600}, now) == now
    assert protocol.resolve_timestamp({"timestamp": 0}, now) == now
    assert protocol.resolve_timestamp({}, now) == now
    assert protocol.resolve_timestamp({"timestamp": "abc"}, now) == now
    assert protocol.resolve_timestamp({"timestamp": True}, now) == now


def test_epoch_timestamp_is_kept():
    now = time.time()
    assert protocol.resolve_timestamp({"timestamp": int(now)}, now) == pytest.approx(
        int(now)
    )


def test_far_future_timestamp_is_distrusted():
    """设备时钟跑飞（差 30 天）同样不可信。"""
    now = time.time()
    skewed = int(now + 30 * 86400)
    assert protocol.resolve_timestamp({"timestamp": skewed}, now) == now


# ================= P0-5 传感器失效语义 =================


def test_valid_false_is_detected():
    assert protocol.is_valid_frame({"data": {"temperature": 25.0}}) is True
    assert protocol.is_valid_frame({"data": {"valid": False}}) is False
    assert protocol.is_sensor_unknown({"data": {"valid": False}}) is True
    assert protocol.is_sensor_unknown({"data": {"state": "UNKNOWN"}}) is True
    assert protocol.is_sensor_unknown({"data": {"state": "NORMAL"}}) is False


def test_invalid_frame_is_rendered_as_sensor_failure(engine: AgentEngine):
    """IMU/DHT 失效时上报 valid=false，渲染必须说"传感器失效"而不是显示 0。

    修复前：温度读失败发 0.0，IMU 没起来发 rms=0/state=NORMAL，
    仪表盘上看起来是"全车间最健康的设备"。
    """
    engine.handle_message(
        "devices/temp_sensor_01/telemetry",
        {"device_type": "temperature_sensor", "data": {"valid": False}},
    )
    text = engine.chat("温度传感器现在什么情况")
    assert "传感器失效" in text

    listing = engine.chat("有哪些设备")
    assert "传感器失效" in listing


# ================= P1-6 控制指令不许静默改派 =================


def test_named_device_without_capability_is_rejected(registry: DeviceRegistry):
    from agent.router import IntentRouter

    route = IntentRouter(registry).route("电机重启")
    assert route.intent is Intent.CONTROL
    assert route.device_id == "motor_01"
    assert route.action is None
    assert "不支持" in route.reason


def test_unsupported_action_publishes_nothing(engine: AgentEngine, publisher: FakePublisher):
    """修复前：「电机重启」会被改派给 temp_sensor_01 并真的发出去。"""
    reply = engine.chat("电机重启")
    assert "不支持" in reply
    assert publisher.commands() == []


def test_unnamed_device_still_uses_capability_fallback(engine: AgentEngine,
                                                       publisher: FakePublisher):
    """没点名设备时仍然允许按动作归属兜底，否则「重启」这种说法会失效。"""
    route = engine.router.route("重启")
    assert route.intent is Intent.CONTROL
    assert route.action == "reboot"
    assert route.device_id == "temp_sensor_01"


# ================= P1-5 expires_at =================


def test_expired_command_is_rejected_without_publishing(
    commands: CommandService, publisher: FakePublisher
):
    result = commands.send("motor_01", "set_speed", 800, expires_in_s=-1)
    assert result.status == "rejected"
    assert "时效" in result.detail
    assert publisher.commands() == []


def test_is_expired_accepts_json_string():
    """build_command 返回字符串，设备侧收到 dict，两处都要能校验。"""
    _, payload = protocol.build_command("off", expires_in_s=-5)
    assert protocol.is_expired(payload) is True
    assert protocol.is_expired(json.loads(payload)) is True
    _, fresh = protocol.build_command("off", expires_in_s=30)
    assert protocol.is_expired(fresh) is False


def test_simulator_rejects_expired_command():
    """模拟器不校验过期，会让"设备端校验"这条契约只在真机上成立。"""

    class _StubClient:
        def __init__(self) -> None:
            self.published: list[tuple[str, str]] = []

        def publish(self, topic, payload, qos=0, retain=False):
            self.published.append((topic, payload))

    class _Msg:
        def __init__(self, payload: str) -> None:
            self.payload = payload.encode("utf-8")

    device = VibrationSensor(device_id="vib_probe")
    stub = _StubClient()
    _, payload = protocol.build_command("set_threshold", 0.9, expires_in_s=-1)
    device._on_message(stub, None, _Msg(payload))

    acks = [json.loads(p) for t, p in stub.published if t.endswith("/cmd/ack")]
    assert acks and acks[0]["result"] == "error"
    assert "expired" in acks[0]["detail"]


# ================= P1-4 pending 不泄漏 =================


def test_fire_and_forget_pending_is_pruned(publisher: FakePublisher,
                                           registry: DeviceRegistry):
    """修复前：wait=False 的条目永远不会被 pop，pending_count 只增不减。

    TTL 用 0.2s 而不是 0.05s：首次运行（冷启动、首次 import、磁盘缓存未热）
    时 `send()` 与 `pending_count` 之间的间隔可能超过 50ms，
    会把"条目刚建就被清理"误判成泄漏。断言要留出足够的余量。
    """
    service = CommandService(publisher, registry, timeout_s=0.2, pending_ttl_s=0.2)
    service.send("light_01", "on", wait=False)
    assert service.pending_count == 1
    time.sleep(0.4)
    assert service.pending_count == 0


def test_poll_returns_ack_for_fire_and_forget(commands: CommandService):
    """不阻塞的调用方（Streamlit）靠 poll 取回执。"""
    result = commands.send("light_01", "on", wait=False)
    deadline = time.time() + 1.0
    ack = None
    while time.time() < deadline and ack is None:
        ack = commands.poll(result.command_id)
        time.sleep(0.01)
    assert ack is not None
    assert ack["result"] == "ok"


# ================= P1-8 --wait 的期望集 =================


def test_wait_for_data_requires_all_configured_devices(engine: AgentEngine,
                                                       cache: DeviceCache):
    """修复前用 `cache.devices()` 当全集，而它只包含"已上报过"的设备：
    1 台设备上报就满足，其余 4 台照样空白。
    """
    cache.update_telemetry("motor_01", "motor_controller", {"speed": 800.0})
    _, ready = _wait_for_data(engine, "有哪些设备", 0.2)
    assert ready is False, "只有 1/5 台设备有数据时不该认为准备好了"

    for spec in engine.registry.all():
        cache.update_telemetry(spec.device_id, spec.device_type, {"x": 1.0})
    _, ready = _wait_for_data(engine, "有哪些设备", 0.2)
    assert ready is True


def test_wait_for_data_waits_for_named_device_only(engine: AgentEngine,
                                                   cache: DeviceCache):
    cache.update_telemetry("motor_01", "motor_controller", {"speed": 800.0})
    target, ready = _wait_for_data(engine, "振动传感器现在什么情况", 0.2)
    assert target == "vibration_01"
    assert ready is False

    cache.update_telemetry("vibration_01", "vibration_sensor", {"rms": 0.06})
    target, ready = _wait_for_data(engine, "振动传感器现在什么情况", 0.2)
    assert (target, ready) == ("vibration_01", True)


# ================= P1-9 Ollama 探测 =================


def test_llm_probe_failure_is_retried(monkeypatch):
    """Agent 先起、Ollama 后起（docker compose 并行拉起很常见）时，
    永久缓存 False 会让所有诊断永远走规则引擎，而日志里看不出任何异常。
    """
    import agent.llm.client as client_module

    monkeypatch.setattr(client_module, "PROBE_RETRY_S", 0.05)

    client = OllamaClient(LlmConfig(enabled=True, base_url="http://127.0.0.1:1"))
    state = {"ok": False}
    monkeypatch.setattr(client, "_probe", lambda: state["ok"])

    assert client.available is False
    state["ok"] = True
    assert client.available is False, "重试间隔未到前应沿用缓存"
    time.sleep(0.08)
    assert client.available is True, "间隔到了必须重新探测"


def test_llm_available_result_is_cached(monkeypatch):
    client = OllamaClient(LlmConfig(enabled=True))
    calls = {"n": 0}

    def _probe() -> bool:
        calls["n"] += 1
        return True

    monkeypatch.setattr(client, "_probe", _probe)
    assert client.available is True
    assert client.available is True
    assert calls["n"] == 1


# ================= P2-10 落库异常隔离 =================


class _BrokenStore:
    def insert_telemetry(self, *args, **kwargs):
        raise RuntimeError("disk I/O error")

    def insert_alert(self, *args, **kwargs):
        raise RuntimeError("disk I/O error")


def test_store_failure_keeps_mqtt_ingest_alive(config: AgentConfig,
                                               cache: DeviceCache,
                                               registry: DeviceRegistry,
                                               commands: CommandService):
    """修复前 insert_telemetry 没有异常保护，一次 SQLite 错误会打断整条订阅回调。"""
    engine = AgentEngine(config, cache, registry, commands, llm=None, store=_BrokenStore())
    engine.handle_message(
        "devices/vibration_01/telemetry",
        {"device_type": "vibration_sensor", "data": {"rms": 0.06}},
    )
    # 缓存照常更新，异常被计数
    assert cache.get("vibration_01").last_data["rms"] == 0.06
    assert engine.persist_errors == 1

    engine.handle_message(
        "devices/vibration_01/telemetry",
        {"device_type": "vibration_sensor", "data": {"rms": 0.07}},
    )
    assert engine.persist_errors == 2
    assert cache.get("vibration_01").last_data["rms"] == 0.07


# ================= 第二轮审查新增 =================


def test_device_returns_online_after_a_short_gap(cache: DeviceCache):
    """掉线若干秒后恢复上报，必须重新变成在线。

    修复前：mark_offline() 把 status 置成 "offline" 之后，update_telemetry()
    只更新 last_seen 不动 status，而 is_online() 一看到 status == "offline"
    就直接返回 False —— 于是设备"数据在刷新、状态永远离线"，
    只有等它重连时重新发布 retained status 才恢复（稳态下不会发生）。
    """
    now = time.time()
    cache.update_telemetry(
        "vibration_01", "vibration_sensor", {"rms": 0.06}, ts=now
    )
    assert cache.is_online("vibration_01", now) is True

    # 超过 TTL：被判定离线
    later = now + 100.0
    assert cache.mark_offline(later) == ["vibration_01"]
    assert cache.is_online("vibration_01", later) is False

    # 又收到遥测：必须恢复在线
    cache.update_telemetry(
        "vibration_01", "vibration_sensor", {"rms": 0.07}, ts=later
    )
    assert cache.is_online("vibration_01", later) is True
    assert cache.get("vibration_01").status == "online"


def test_alert_received_at_comes_from_engine(cache: DeviceCache):
    """库里的 received_at 必须与缓存里一致，否则面板排序和"最近告警"对不上。"""
    from agent.storage.db import Store

    store = Store(":memory:")
    try:
        engine = AgentEngine(
            AgentConfig(), cache, DeviceRegistry(AgentConfig().devices),
            CommandService(FakePublisher(), DeviceRegistry(AgentConfig().devices)),
            llm=None, store=store,
        )
        stamp = time.time()
        engine.handle_message(
            "devices/vibration_01/alert",
            {
                "device_id": "vibration_01",
                "alert_id": "vibration_01-1",
                "alert_type": "vibration_high",
                "value": 0.72,
                "threshold": 0.55,
                "severity": "critical",
                "timestamp": 3600,  # 设备没同步时间：必须回退到接收时间
            },
        )
        row = store._conn.execute(  # noqa: SLF001 - 测试直接查库最直观
            "SELECT received_at, ts FROM alerts WHERE alert_id='vibration_01-1'"
        ).fetchone()
        assert row is not None
        cached = cache.recent_alerts(1)[0]
        assert row["received_at"] == pytest.approx(cached["received_at"])
        assert row["received_at"] > stamp - 1
        # ts 是"可信时间"，设备只给了开机秒数，所以应当是接收时间而不是 3600
        assert row["ts"] > 1_600_000_000
    finally:
        store.close()
