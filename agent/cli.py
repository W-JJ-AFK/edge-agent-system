"""命令行入口：装配组件、启动总线、交互或单次问答。

    python -m agent.cli                 # 交互模式
    python -m agent.cli --once "有哪些设备"
    python -m agent.cli --no-llm        # 强制规则引擎模式（演示降级）
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .bus import MqttBus
from .cache import DeviceCache
from .command_service import CommandService
from .config import AgentConfig
from .engine import AgentEngine
from .llm.client import OllamaClient
from .registry import DeviceRegistry
from .router import HELP_TEXT
from .storage.db import Store

log = logging.getLogger(__name__)


def build_engine(config: AgentConfig) -> tuple[AgentEngine, MqttBus, Store]:
    registry = DeviceRegistry(config.devices)
    ttl_map = registry.ttl_map(config.offline_after_periods)
    cache = DeviceCache(
        ttl_by_device=ttl_map,
        default_ttl_s=max(ttl_map.values()) if ttl_map else 6.0,
    )
    store = Store(config.storage.path)
    # 启动时清一次历史：没有保留策略时 21 万点/天，一年就是 GB 级，
    # 而演示环境没人会记得手工清库。
    try:
        removed = store.prune(
            telemetry_days=config.telemetry_retention_days,
            alert_days=config.alert_retention_days,
        )
        if any(removed.values()):
            log.info("pruned old data: %s", removed)
    except Exception:  # noqa: BLE001 - 清理失败不应阻塞启动
        log.debug("prune failed", exc_info=True)

    bus = MqttBus(config.mqtt)
    commands = CommandService(bus, registry, timeout_s=config.command_timeout_s)
    llm = OllamaClient(config.llm)
    engine = AgentEngine(config, cache, registry, commands, llm=llm, store=store)
    bus.on_message = engine.handle_message
    return engine, bus, store


def _print_mode(engine: AgentEngine, config: AgentConfig) -> None:
    llm_state = "可用" if engine.llm_available else "不可用（规则引擎兜底）"
    print("=" * 62)
    print("🤖 设备运维 Agent v2")
    print(f"   MQTT     : {config.mqtt.host}:{config.mqtt.port}")
    print(f"   设备数   : {len(config.devices)}"
          f"（硬件 {len(config.hardware_devices())}）")
    print(f"   LLM      : {config.llm.model} · {llm_state}")
    print("=" * 62)


def _wait_for_data(engine: AgentEngine, text: str, timeout_s: float) -> tuple[str | None, bool]:
    """等"这个问题真正需要的数据"到达。

    `--once` 模式下如果不等待，订阅刚建立就问问题，缓存必然是空的，
    用户会看到"无数据"——这不是 bug，但体验上等于不可用。

    分两种情况，因为"需要的数据"不一样：
    * 问某台设备：必须等**这一台**。motor_01 每 1s 上报会立刻满足"任意设备有数据"，
      而用户问的是 2s 才上报一次的振动传感器，只等"任意"依然拿到空数据。
    * 问设备列表：等**配置里的全部设备**都有数据。

    第二个判断以前写成 `all(state.last_data for state in cache.devices())`，
    而 `cache.devices()` 只包含"已经上报过消息"的设备——一台从没上报的设备
    根本不在列表里，于是 1 台设备上报就满足了，其余 4 台照样空白。
    必须以配置清单为期望集，而不是以"已出现的子集"取全集。

    :return: (目标设备 ID 或 None, 是否已满足)
    """
    target = engine.router.route(text).device_id
    expected = [spec.device_id for spec in engine.registry.all()]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if target:
            state = engine.cache.get(target)
            if state is not None and state.last_data:
                return target, True
        elif expected and all(
            (state := engine.cache.get(did)) is not None and state.last_data
            for did in expected
        ):
            return None, True
        time.sleep(0.05)
    return target, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端云协同设备运维 Agent")
    parser.add_argument("--config", help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--once", help="只回答一个问题后退出")
    parser.add_argument("--no-llm", action="store_true", help="禁用 LLM，仅用规则引擎")
    parser.add_argument(
        "--wait",
        type=float,
        default=3.0,
        help="--once 模式下等待遥测的秒数（默认 3.0，设 0 则不等待）",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = AgentConfig.load(args.config)
    if args.no_llm:
        config.llm.enabled = False

    engine, bus, store = build_engine(config)

    if not bus.start(wait=True, timeout=5.0):
        print("⚠️  MQTT 连接失败：请先启动 broker（docker compose up -d mosquitto）")
        print("   已转为后台重连，broker 起来后会自动接上；离线问答仍然可用。")

    _print_mode(engine, config)

    try:
        if args.once:
            if args.wait > 0:
                target, ready = _wait_for_data(engine, args.once, args.wait)
                # 只在"问单台设备但没数据"时提示；列表查询缺几台是正常的
                # （比如 5s 上报一次的环境监测器），不值得打扰用户。
                if target and not ready:
                    print(f"⚠️  {args.wait:.1f}s 内没等到 {target} 的遥测，回答可能不完整")
            print(engine.chat(args.once))
            return 0

        print("输入 'help' 查看示例，'quit' 退出\n")
        while True:
            try:
                text = input("💬 You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 再见")
                break
            if not text:
                continue
            if text.lower() in {"quit", "exit", "q"}:
                print("👋 再见")
                break
            if text.lower() in {"help", "帮助"}:
                print(HELP_TEXT)
                continue
            print(f"\n🤖 Agent:\n{engine.chat(text)}\n")
            engine.tick()
            time.sleep(0.05)
    finally:
        bus.stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
