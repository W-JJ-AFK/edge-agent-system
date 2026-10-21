"""共享测试夹具。

所有测试都跑在「无 broker、无 Ollama」的环境里，靠注入假实现验证逻辑。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.cache import DeviceCache  # noqa: E402
from agent.command_service import CommandService  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.registry import DeviceRegistry  # noqa: E402


class FakePublisher:
    """记录发布内容；可选地自动回执，用于测试指令闭环。"""

    def __init__(self, auto_ack: bool = True, result: str = "ok", detail: str = "执行成功") -> None:
        self.auto_ack = auto_ack
        self.result = result
        self.detail = detail
        self.published: list[tuple[str, str, int, bool]] = []
        self.command_service: CommandService | None = None
        self.raise_on_publish: Exception | None = None

    def publish(self, topic: str, payload: str, qos: int = 1, retain: bool = False) -> None:
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.published.append((topic, payload, qos, retain))
        if not self.auto_ack or not topic.endswith("/cmd"):
            return
        try:
            command_id = json.loads(payload)["command_id"]
        except (json.JSONDecodeError, KeyError):
            return

        def _ack() -> None:
            if self.command_service is not None:
                self.command_service.handle_ack(
                    {"command_id": command_id, "result": self.result, "detail": self.detail}
                )

        timer = threading.Timer(0.01, _ack)
        timer.daemon = True
        timer.start()

    def commands(self) -> list[dict]:
        return [json.loads(p) for t, p, _, _ in self.published if t.endswith("/cmd")]


@pytest.fixture
def config() -> AgentConfig:
    cfg = AgentConfig()
    cfg.llm.enabled = False  # 测试中不访问网络
    return cfg


@pytest.fixture
def registry(config: AgentConfig) -> DeviceRegistry:
    return DeviceRegistry(config.devices)


@pytest.fixture
def cache(registry: DeviceRegistry) -> DeviceCache:
    return DeviceCache(ttl_by_device=registry.ttl_map(3))


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def commands(publisher: FakePublisher, registry: DeviceRegistry) -> CommandService:
    service = CommandService(publisher, registry, timeout_s=0.5)
    publisher.command_service = service
    return service
