"""
设备运维 Dashboard - Streamlit 可视化面板
"""

import json
import time
from datetime import datetime

import streamlit as st
import paho.mqtt.client as mqtt

st.set_page_config(
    page_title="IoT 设备运维中心",
    page_icon="🤖",
    layout="wide"
)

BROKER = "localhost"
PORT = 1883

# 获取 MQTT 数据
@st.cache_resource
def get_mqtt_client():
    client = mqtt.Client(client_id=f"dashboard_{int(time.time())}")
    client.connect(BROKER, PORT, 60)
    client.loop_start()
    return client

def fetch_device_data():
    """通过临时订阅获取当前设备快照"""
    client = get_mqtt_client()
    data = {}
    received = threading.Event()
    
    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            device_id = payload.get("device_id")
            if device_id:
                data[device_id] = {
                    "type": payload.get("device_type", "unknown"),
                    "data": payload.get("data", {}),
                    "timestamp": datetime.now().strftime("%H:%M:%S")
                }
                received.set()
        except Exception:
            pass
    
    client.subscribe("devices/+/telemetry")
    client.on_message = on_message
    
    # 等待收集数据
    time.sleep(3)
    client.unsubscribe("devices/+/telemetry")
    
    return data

def fetch_alerts():
    """获取最近的告警"""
    client = get_mqtt_client()
    alerts = []
    received = threading.Event()
    
    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            alerts.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "device": payload.get("device_id", "unknown"),
                "type": payload.get("alert_type", "unknown"),
                "value": payload.get("value", 0),
                "severity": payload.get("severity", "info")
            })
            received.set()
        except Exception:
            pass
    
    client.subscribe("devices/alerts")
    client.on_message = on_message
    
    time.sleep(2)
    client.unsubscribe("devices/alerts")
    
    return alerts

import threading

st.title("🤖 IoT 设备运维中心")
st.markdown("---")

# 刷新按钮
col_btn, col_info = st.columns([1, 5])
with col_btn:
    refresh = st.button("🔄 刷新数据", use_container_width=True)
with col_info:
    st.caption("点击刷新按钮获取最新设备数据")

st.markdown("---")

# 获取数据
device_data = fetch_device_data()
alerts = fetch_alerts()

# 统计栏
col1, col2, col3 = st.columns(3)
online_count = len(device_data)
alert_count = len([a for a in alerts if a['severity'] == 'critical'])
warning_count = len([a for a in alerts if a['severity'] == 'warning'])

col1.metric("在线设备", online_count)
col2.metric("严重告警", alert_count)
col3.metric("警告", warning_count)

st.markdown("---")

# 设备卡片
st.subheader("📡 设备实时数据")

if device_data:
    cols = st.columns(len(device_data))
    icon_map = {
        "temperature_sensor": "🌡️",
        "vibration_sensor": "📳",
        "motor_controller": "⚙️",
        "smart_light": "💡",
        "env_monitor": "🌿"
    }
    
    for i, (device_id, info) in enumerate(device_data.items()):
        with cols[i]:
            device_type = info.get("type", "unknown")
            icon = icon_map.get(device_type, "📦")
            data = info.get("data", {})
            
            st.markdown(f"### {icon} {device_id}")
            st.caption(f"{device_type}")
            
            for key, value in data.items():
                if isinstance(value, float):
                    st.metric(key, f"{value:.1f}")
                else:
                    st.metric(key, value)
            
            st.caption(f"🕐 {info.get('timestamp', 'N/A')}")
else:
    st.warning("未获取到设备数据。请确认 main.py 正在运行。")

st.markdown("---")

# 告警记录
st.subheader("🚨 告警记录")

if alerts:
    st.dataframe(
        [
            {
                "时间": a['time'],
                "级别": f"{'🔴' if a['severity']=='critical' else '🟡'} {a['severity']}",
                "设备": a['device'],
                "类型": a['type'],
                "值": a['value']
            }
            for a in alerts
        ],
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("当前无告警。设备运行正常。")

st.markdown("---")
st.caption("💡 提示：告警由设备模拟器随机触发，点击刷新查看最新状态。")
