"""装配层测试。

这里只验证「组件能被正确拼起来」这件事本身 —— 之前 cli.build_engine 里
DeviceCache 的参数名写错（default_ttl vs default_ttl_s），单元测试全绿但
命令行一启动就崩。装配层必须有自己的测试，否则就是靠运气。

不依赖 broker、不依赖 Ollama：MqttBus 的构造函数不建连，Store 用临时库。
"""

from __future__ import annotations

from pathlib import Path

from agent.cli import build_engine
from agent.config import AgentConfig


def _config(tmp_path: Path) -> AgentConfig:
    cfg = AgentConfig.load()
    cfg.llm.enabled = False
    cfg.storage.path = str(tmp_path / "agent.db")
    return cfg


def test_build_engine_wires_all_components(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    engine, bus, store = build_engine(cfg)

    assert engine is not None
    assert bus is not None
    # 消息回调必须挂上，否则订阅到的数据没人处理
    assert bus.on_message == engine.handle_message
    assert Path(cfg.storage.path).exists(), "Store 应已建库"
    store.close()


def test_build_engine_respects_offline_ttl(tmp_path: Path) -> None:
    """离线判定阈值来自 offline_after_periods × 上报间隔，不是写死的。"""
    cfg = _config(tmp_path)
    engine, _bus, store = build_engine(cfg)

    # vibration_01 间隔 2s、offline_after_periods=3 → TTL 6s
    assert engine.cache.ttl_for("vibration_01") == 6.0
    store.close()


def test_engine_answers_without_broker(tmp_path: Path) -> None:
    """没有 broker 时也要能回答静态问题，而不是抛异常。"""
    cfg = _config(tmp_path)
    engine, _bus, store = build_engine(cfg)

    reply = engine.chat("有哪些设备")
    assert "vibration_01" in reply
    store.close()


def test_llm_disabled_falls_back_to_rules(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    engine, _bus, store = build_engine(cfg)

    assert engine.llm_available is False
    reply = engine.chat("为什么振动传感器告警了")
    # 规则引擎的四段式诊断报告必须仍然产出
    assert "振动" in reply or "vibration" in reply
    store.close()
