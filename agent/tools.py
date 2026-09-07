"""
Agent 工具箱 - 设备运维工具函数
"""

import json
import time
import paho.mqtt.client as mqtt
from collections import defaultdict
from datetime import datetime

class DeviceTools:
    """设备管理工具集，供 LangChain Agent 调用"""
    
    def __init__(self, broker="localhost", port=1883):
        self.broker = broker
        self.port = port
        self.device_cache = {}
        self.telemetry_history = defaultdict(list)
        self.max_history = 100
        
        self.knowledge_base = {
            "temp_high": {
                "causes": ["散热风扇故障", "环境温度过高", "传感器漂移", "设备过载"],
                "actions": ["检查散热系统", "降低设备负载", "校准传感器", "启用备用冷却"],
                "severity": "critical"
            },
            "vibration_high": {
                "causes": ["轴承磨损", "转子不平衡", "安装松动", "共振"],
                "actions": ["停机检查轴承", "做动平衡校正", "紧固安装螺栓", "改变运行频率"],
                "severity": "critical"
            },
            "current_overload": {
                "causes": ["机械卡滞", "绕组短路", "电源波动", "负载突变"],
                "actions": ["立即降速", "检查机械传动", "测量绕组电阻", "检查电源质量"],
                "severity": "critical"
            },
            "co2_high": {
                "causes": ["通风不足", "人员密集", "燃烧源泄漏", "新风系统故障"],
                "actions": ["开启新风系统", "疏散人员", "检查通风管道", "启用空气净化"],
                "severity": "warning"
            }
        }
    
    def scan_devices(self) -> str:
        if not self.device_cache:
            return "未发现在线设备。请确认设备模拟器正在运行。"
        
        result = "当前在线设备：\n"
        result += "-" * 50 + "\n"
        for device_id, info in self.device_cache.items():
            last_seen = datetime.fromtimestamp(info["last_seen"]).strftime("%H:%M:%S")
            result += f"  ID: {device_id}\n"
            result += f"  类型: {info['device_type']}\n"
            result += f"  最后活跃: {last_seen}\n"
            result += "-" * 50 + "\n"
        
        return result
    
    def get_telemetry(self, device_id: str) -> str:
        if device_id not in self.device_cache:
            return f"设备 {device_id} 未找到。可用设备: {list(self.device_cache.keys())}"
        
        info = self.device_cache[device_id]
        if not info.get("last_data"):
            return f"设备 {device_id} 尚无遥测数据。"
        
        data = info["last_data"]
        timestamp = datetime.fromtimestamp(data["timestamp"]).strftime("%H:%M:%S")
        
        result = f"设备 {device_id} 最新数据 ({timestamp}):\n"
        for key, value in data["data"].items():
            result += f"  {key}: {value}\n"
        
        return result
    
    def analyze_trend(self, device_id: str, metric: str = None) -> str:
        if device_id not in self.telemetry_history:
            return f"设备 {device_id} 没有历史数据。"
        
        history = self.telemetry_history[device_id]
        if len(history) < 3:
            return f"设备 {device_id} 数据不足（仅 {len(history)} 条），需要更多数据。"
        
        recent = history[-20:]
        
        result = f"设备 {device_id} 趋势分析（最近 {len(recent)} 条数据）：\n"
        result += "-" * 50 + "\n"
        
        all_metrics = set()
        for entry in recent:
            all_metrics.update(entry["data"].keys())
        
        for m in all_metrics:
            if metric and m != metric:
                continue
            
            values = []
            for entry in recent:
                if m in entry["data"]:
                    val = entry["data"][m]
                    if isinstance(val, (int, float)):
                        values.append(val)
            
            if len(values) >= 2:
                avg = sum(values) / len(values)
                min_val = min(values)
                max_val = max(values)
                trend = "上升" if values[-1] > values[0] else "下降"
                change = abs(values[-1] - values[0])
                
                result += f"  {m}:\n"
                result += f"    均值: {avg:.2f}\n"
                result += f"    范围: {min_val:.2f} ~ {max_val:.2f}\n"
                result += f"    趋势: {trend} (变化 {change:.2f})\n"
        
        return result
    
    def send_command(self, device_id: str, action: str, value: float = None) -> str:
        if device_id not in self.device_cache:
            return f"设备 {device_id} 不在线，无法发送指令。"
        
        command = {"action": action}
        if value is not None:
            command["value"] = value
        
        topic = f"devices/{device_id}/command"
        
        try:
            client = mqtt.Client(client_id=f"agent_cmd_{int(time.time())}")
            client.connect(self.broker, self.port, 10)
            client.publish(topic, json.dumps(command))
            client.disconnect()
            
            return f"✅ 指令已发送: {device_id} -> {action}" + (f" (value={value})" if value is not None else "")
        except Exception as e:
            return f"❌ 指令发送失败: {e}"
    
    def query_knowledge(self, alert_type: str) -> str:
        if alert_type not in self.knowledge_base:
            return f"知识库中未找到 '{alert_type}'。已知告警类型: {list(self.knowledge_base.keys())}"
        
        kb = self.knowledge_base[alert_type]
        result = f"告警类型: {alert_type}\n"
        result += f"严重级别: {kb['severity']}\n\n"
        result += "可能原因:\n"
        for cause in kb["causes"]:
            result += f"  • {cause}\n"
        result += "\n建议操作:\n"
        for action in kb["actions"]:
            result += f"  • {action}\n"
        
        return result
    
    def get_all_alerts(self) -> str:
        alerts_found = []
        current_time = time.time()
        for device_id, info in self.device_cache.items():
            if info.get("last_alert"):
                alert = info["last_alert"]
                alert_time = alert.get("alert_time", 0)
                if current_time - alert_time < 30:
                    alerts_found.append(f"  {device_id}: {alert['alert_type']} = {alert['value']} ({alert['severity']})")
        
        if not alerts_found:
            return "当前没有活跃告警。"
        
        return "当前告警列表:\n" + "\n".join(alerts_found)
    
    def update_device_cache(self, device_id, device_type, data=None):
        old_alert = None
        if device_id in self.device_cache:
            old_alert = self.device_cache[device_id].get("last_alert")
        
        self.device_cache[device_id] = {
            "device_type": device_type,
            "last_seen": time.time(),
            "last_data": data
        }
        
        if old_alert:
            self.device_cache[device_id]["last_alert"] = old_alert
        
        if data:
            self.telemetry_history[device_id].append(data)
            if len(self.telemetry_history[device_id]) > self.max_history:
                self.telemetry_history[device_id] = self.telemetry_history[device_id][-self.max_history:]
    
    def update_alert_cache(self, device_id, alert_data):
        alert_data["alert_time"] = time.time()
        if device_id not in self.device_cache:
            self.device_cache[device_id] = {
                "device_type": "unknown",
                "last_seen": time.time(),
                "last_data": None
            }
        self.device_cache[device_id]["last_alert"] = alert_data
