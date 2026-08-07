"""
LLM 诊断引擎 - 直接调用 Ollama，不依赖 LangChain Agent
"""

import json
import requests
from agent.tools import DeviceTools


class LLMEngine:
    """基于 Ollama 原生 API 的 LLM 诊断引擎"""

    def __init__(self, tools: DeviceTools, model="qwen2.5:1.5b"):
        self.tools = tools
        self.model = model
        self.base_url = "http://localhost:11434"
        self.available = self._check_ollama()
        if self.available:
            print(f"[Agent] LLM 引擎已加载 (模型: {self.model})")

    def _check_ollama(self):
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def _call_ollama(self, messages: list) -> str:
        """调用 Ollama chat API"""
        try:
            r = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.1}
                },
                timeout=60
            )
            if r.status_code == 200:
                return r.json()["message"]["content"]
            return None
        except Exception as e:
            print(f"[Agent] Ollama 调用失败: {e}")
            return None

    def diagnose(self, user_input: str) -> str:
        """智能诊断：收集数据 → 喂给 LLM → 返回诊断结果"""
        if not self.available:
            return None

        # 第一步：收集所有设备信息
        devices_info = self.tools.scan_devices()
        alerts_info = self.tools.get_all_alerts()

        # 第二步：如果有告警，获取详细数据
        detail_info = ""
        for device_id in ["temp_sensor_01", "vibration_01", "motor_01", "light_01", "env_01"]:
            if device_id in self.tools.device_cache:
                telemetry = self.tools.get_telemetry(device_id)
                trend = self.tools.analyze_trend(device_id)
                detail_info += f"\n--- {device_id} ---\n{telemetry}\n{trend}\n"

        # 第三步：获取知识库
        kb_info = ""
        if "没有" not in alerts_info:
            for alert_type in ["temp_high", "vibration_high", "current_overload", "co2_high"]:
                if alert_type in alerts_info:
                    kb_info += self.tools.query_knowledge(alert_type) + "\n"

        # 第四步：构建 Prompt
        system_prompt = """你是工业物联网设备运维专家。根据以下设备数据，诊断问题并给出建议。

回复格式：
1. **设备状态总览**：简要概括所有设备状态
2. **异常分析**：如果存在异常，分析可能原因
3. **建议操作**：给出具体可执行的修复步骤
4. **风险评估**：评估当前风险等级（低/中/高）

用中文回复，专业但易懂。不要编造数据，只基于提供的信息做判断。"""

        user_prompt = f"""用户问题：{user_input}

=== 设备信息 ===
{devices_info}

=== 告警信息 ===
{alerts_info}

=== 详细数据 ===
{detail_info}

=== 知识库参考 ===
{kb_info}

请根据以上信息进行诊断。"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        return self._call_ollama(messages)


class SimpleLLM:
    """简化版 LLM 调用，用于聊天模式"""

    def __init__(self, model="qwen2.5:1.5b"):
        self.model = model
        self.base_url = "http://localhost:11434"

    def chat(self, user_message: str, system_prompt: str = None) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        try:
            r = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": 0.7}
                },
                timeout=60
            )
            if r.status_code == 200:
                return r.json()["message"]["content"]
            return "LLM 调用失败"
        except Exception as e:
            return f"错误: {e}"
