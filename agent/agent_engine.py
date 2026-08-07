"""
AI Agent 引擎 - 设备运维 Agent
"""

import json
import time
import re
import paho.mqtt.client as mqtt
from agent.tools import DeviceTools


class MQTTListener:
    """MQTT 监听器 - 在后台收集设备数据"""

    def __init__(self, tools: DeviceTools, broker="localhost", port=1883):
        self.tools = tools
        self.broker = broker
        self.port = port
        self.client = mqtt.Client(client_id=f"agent_listener_{int(time.time())}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.running = False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("[Agent] MQTT 监听器已连接")
            client.subscribe("devices/+/telemetry")
            client.subscribe("devices/alerts")
        else:
            print(f"[Agent] 监听器连接失败: {rc}")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if msg.topic == "devices/alerts":
                device_id = payload.get("device_id")
                if device_id:
                    self.tools.update_alert_cache(device_id, payload)
            elif "/telemetry" in msg.topic:
                device_id = payload.get("device_id")
                device_type = payload.get("device_type")
                if device_id:
                    self.tools.update_device_cache(device_id, device_type, payload)
        except Exception:
            pass

    def start(self):
        self.running = True
        self.client.connect(self.broker, self.port, 60)
        self.client.loop_start()

    def stop(self):
        self.running = False
        self.client.loop_stop()
        self.client.disconnect()


class RuleEngine:
    """规则引擎 - 基于关键词匹配"""

    def __init__(self, tools: DeviceTools):
        self.tools = tools

    def process(self, user_input: str) -> str:
        user_input_lower = user_input.lower()

        if any(kw in user_input_lower for kw in ["扫描", "设备列表", "在线", "scan", "list"]):
            return self.tools.scan_devices()

        if any(kw in user_input_lower for kw in ["告警", "报警", "alert", "故障", "异常"]):
            alerts = self.tools.get_all_alerts()
            if "没有" not in alerts:
                for at in ["temp_high", "vibration_high", "current_overload", "co2_high"]:
                    if at in alerts:
                        kb = self.tools.query_knowledge(at)
                        return alerts + "\n\n📚 知识库建议:\n" + kb
            return alerts

        for did in ["temp_sensor_01", "vibration_01", "motor_01", "light_01", "env_01"]:
            if did in user_input_lower:
                t = self.tools.get_telemetry(did)
                tr = self.tools.analyze_trend(did)
                return t + "\n" + tr

        if any(kw in user_input_lower for kw in ["知识库", "原因", "建议", "怎么办", "处理"]):
            for at in ["temp_high", "vibration_high", "current_overload", "co2_high"]:
                if at.replace("_", "") in user_input_lower.replace(" ", ""):
                    return self.tools.query_knowledge(at)
            return "请指定告警类型: temp_high, vibration_high, current_overload, co2_high"

        if "降速" in user_input_lower or "减速" in user_input_lower:
            numbers = re.findall(r'\d+', user_input)
            speed = int(numbers[0]) if numbers else 800
            return self.tools.send_command("motor_01", "set_speed", speed)

        if "关灯" in user_input_lower or "关闭灯光" in user_input_lower:
            return self.tools.send_command("light_01", "off")

        if "开灯" in user_input_lower or "打开灯光" in user_input_lower:
            return self.tools.send_command("light_01", "on")

        return self._help()

    def _help(self):
        return """
🤖 设备运维 Agent (规则引擎模式)

可用命令:
  • "扫描设备" - 查看所有在线设备
  • "查看告警" - 查看当前告警状态
  • "temp_sensor_01 状态" - 查询具体设备
  • "温度过高怎么办" - 查询知识库
  • "电机降速到 800" - 下发控制指令
  • "关灯" / "开灯" - 控制智能灯

💡 提示: 安装 LangChain 可获得 AI 驱动智能诊断:
   pip install langchain langchain-openai
"""


class DeviceAgent:
    """设备运维 Agent 主类"""

    def __init__(self, broker="localhost", port=1883):
        self.tools = DeviceTools(broker, port)
        self.listener = MQTTListener(self.tools, broker, port)
        self.rule_engine = RuleEngine(self.tools)

    def start(self):
        print("🤖 设备运维 Agent 启动中...")
        self.listener.start()
        time.sleep(2)
        print("✅ Agent 就绪\n")

    def stop(self):
        self.listener.stop()
        print("Agent 已停止")

    def chat(self, user_input: str) -> str:
        return self.rule_engine.process(user_input)


def interactive_shell(agent: DeviceAgent):
    print("=" * 60)
    print("🤖 嵌入式设备运维 AI Agent")
    print("=" * 60)
    print("输入 'help' 查看帮助, 'quit' 退出\n")

    while True:
        try:
            user_input = input("💬 You: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["quit", "exit", "q"]:
                print("👋 再见!")
                break
            if user_input.lower() == "help":
                print(agent.rule_engine._help())
                continue
            response = agent.chat(user_input)
            print(f"\n🤖 Agent:\n{response}\n")
        except KeyboardInterrupt:
            print("\n👋 再见!")
            break
        except Exception as e:
            print(f"\n❌ 错误: {e}\n")
