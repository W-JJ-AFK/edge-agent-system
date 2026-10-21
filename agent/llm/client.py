"""Ollama 客户端：原生 /api/chat + 工具调用循环。

不引入 LangChain 的理由：
Function Calling 本质就是「请求 → 返回 tool_calls → 本地执行 → 结果回灌」的循环，
自己写约 80 行，换来的是可控的提示词、可控的超时、可控的降级行为，
以及一条能讲清楚的调用链（面试时比"我用了 LangChain Agent"更有信息量）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import LlmConfig
from .prompts import build_messages
from .tools import TOOL_SCHEMAS, ToolExecutor

log = logging.getLogger(__name__)

#: 探测失败后的重试间隔。只缓存 True 会漏掉"Ollama 比 Agent 晚启动"这种常见顺序；
#: 永久缓存 False 更糟——日志里什么都看不到，用户以为模型一直在工作。
PROBE_RETRY_S = 30.0


class OllamaClient:
    def __init__(self, config: LlmConfig) -> None:
        self._cfg = config
        self._available: bool | None = None
        self._probed_at = 0.0

    # ---------------- 可用性 ----------------

    @property
    def available(self) -> bool:
        """惰性探测：可用结果永久缓存，不可用结果 30s 后重试。"""
        if not self._cfg.enabled:
            return False
        if self._available is True:
            return True

        now = time.monotonic()
        if self._available is False and (now - self._probed_at) < PROBE_RETRY_S:
            return False

        self._available = self._probe()
        self._probed_at = now
        if self._available:
            log.info("ollama reachable at %s (model=%s)", self._cfg.base_url, self._cfg.model)
        else:
            log.info(
                "ollama not reachable at %s, will retry in %.0fs (rules engine in use)",
                self._cfg.base_url,
                PROBE_RETRY_S,
            )
        return self._available

    def refresh(self) -> bool:
        """强制重新探测（配置变更或人工重连后调用）。"""
        self._available = None
        return self.available

    def _probe(self) -> bool:
        try:
            import requests
        except ImportError:
            return False
        try:
            response = requests.get(f"{self._cfg.base_url}/api/tags", timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def _mark_down(self) -> None:
        """调用失败时把可用性降级，让下一轮重新探测，而不是一直"以为它在"。"""
        self._available = False
        self._probed_at = time.monotonic()

    # ---------------- 基础调用 ----------------

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        try:
            import requests
        except ImportError:
            return None

        payload: dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self._cfg.temperature},
        }
        if tools:
            payload["tools"] = tools

        try:
            response = requests.post(
                f"{self._cfg.base_url}/api/chat",
                json=payload,
                timeout=self._cfg.timeout_s,
            )
            if response.status_code != 200:
                log.warning("ollama returned %s", response.status_code)
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.warning("ollama chat failed: %s", exc)
            self._mark_down()
            return None

    # ---------------- 诊断主流程 ----------------

    def diagnose(self, question: str, context: str, executor: ToolExecutor) -> str | None:
        """带工具调用的诊断；任何一步失败都返回 None，由上层降级到规则引擎。"""
        if not self.available:
            return None

        messages = build_messages(question, context)

        for _ in range(max(1, self._cfg.max_tool_rounds)):
            response = self.chat(messages, TOOL_SCHEMAS)
            if response is None:
                return None

            message = response.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                content = (message.get("content") or "").strip()
                return content or None

            # 回灌 assistant 的工具调用意图，再逐条追加执行结果
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                result = executor.call(name, function.get("arguments"))
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

        return None
