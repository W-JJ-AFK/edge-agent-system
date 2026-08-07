"""
AI Agent 引擎 - 设备运维 Agent
"""

import json
import time
import re
import paho.mqtt.client as mqtt
from agent.tools import DeviceTools
from agent.llm_engine import LLMEngine


class MQTTListener:
    def __init__(self, tools, broker="localhost", port=1883):
        self.tools = tools
        self.client = mqtt.Client(client_id=f"listener_{int(time.time())}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("[Agent] MQTT 监听器已连接")
            client.subscribe("devices/+/telemetry")
            client.subscribe("devices/alerts")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if msg.topic == "devices/alerts":
                did = payload.get("device_id")
                if did:
                    self.tools.update_alert_cache(did, payload)
            elif "/telemetry" in msg.topic:
                did = payload.get("device_id")
                dtype = payload.get("device_type")
                if did:
                    self.tools.update_device_cache(did, dtype, payload)
        except Exception:
            pass

    def start(self):
        self.client.connect("localhost", 1883, 60)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()


class RuleEngine:
    def __init__(self, tools):
        self.tools = tools

    def process(self, user_input: str) -> str:
        u = user_input.lower()
        if any(kw in u for kw in ["扫描", "设备列表", "在线", "scan"]):
            return self.tools.scan_devices()
        if any(kw in u for kw in ["告警", "报警", "alert", "故障"]):
            alerts = self.tools.get_all_alerts()
            if "没有" not in alerts:
                for at in ["temp_high", "vibration_high", "current_overload", "co2_high"]:
                    if at in alerts:
                        return alerts + "\n\n📚 知识库:\n" + self.tools.query_knowledge(at)
            return alerts
        for did in ["temp_sensor_01", "vibration_01", "motor_01", "light_01", "env_01"]:
            if did in u:
                return self.tools.get_telemetry(did) + "\n" + self.tools.analyze_trend(did)
        if any(kw in u for kw in ["知识库", "原因", "建议"]):
            for at in ["temp_high", "vibration_high", "current_overload", "co2_high"]:
                if at.replace("_", "") in u.replace(" ", ""):
                    return self.tools.query_knowledge(at)
            return "告警类型: temp_high, vibration_high, current_overload, co2_high"
        if "降速" in u or "减速" in u:
            nums = re.findall(r'\d+', user_input)
            return self.tools.send_command("motor_01", "set_speed", int(nums[0]) if nums else 800)
        if "关灯" in u: return self.tools.send_command("light_01", "off")
        if "开灯" in u: return self.tools.send_command("light_01", "on")
        return self._help()

    def _help(self):
        return """
🤖 设备运维 Agent (LLM模式)

自然语言交互示例:
  • "检查所有设备状态"
  • "为什么温度传感器告警了？"
  • "帮我分析一下振动传感器的数据"
  • "电机降速到 800"
  • "关灯" / "开灯"
  • "扫描设备" / "查看告警"
"""


class DeviceAgent:
    def __init__(self, use_llm=True):
        self.tools = DeviceTools()
        self.listener = MQTTListener(self.tools)
        self.rule_engine = RuleEngine(self.tools)
        self.llm_engine = LLMEngine(self.tools) if use_llm else None
        self.use_llm = use_llm and self.llm_engine and self.llm_engine.available

    def start(self):
        mode = "LLM 智能诊断" if self.use_llm else "规则引擎"
        print(f"🤖 Agent 启动中... (模式: {mode})")
        self.listener.start()
        time.sleep(2)
        print("✅ Agent 就绪\n")

    def stop(self):
        self.listener.stop()

    def chat(self, user_input: str) -> str:
        if self.use_llm:
            result = self.llm_engine.diagnose(user_input)
            if result:
                return result
        return self.rule_engine.process(user_input)


def interactive_shell(agent: DeviceAgent):
    mode = "LLM 智能诊断" if agent.use_llm else "规则引擎"
    print("=" * 60)
    print(f"🤖 嵌入式设备运维 AI Agent ({mode})")
    print("=" * 60)
    print("输入 'help' 查看帮助, 'quit' 退出\n")

    while True:
        try:
            user_input = input("💬 You: ").strip()
            if not user_input: continue
            if user_input.lower() in ["quit", "exit", "q"]:
                print("👋 再见!"); break
            if user_input.lower() == "help":
                print(agent.rule_engine._help()); continue
            print("🤖 思考中...")
            response = agent.chat(user_input)
            print(f"\n🤖 Agent:\n{response}\n")
        except KeyboardInterrupt:
            print("\n👋 再见!"); break
