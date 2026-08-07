# 边缘智能设备运维 Agent 系统

基于 MQTT + AI Agent 的 IoT 设备运维监控系统，实现设备模拟、实时监控、智能诊断和远程控制。

## 架构

虚拟设备层 (5类IoT设备) -> MQTT -> Agent 引擎层 -> 可视化层 (Streamlit)

## 快速启动

### 1. 启动 Mosquitto
mosquitto -d

### 2. 启动设备模拟器
python main.py

### 3. 启动 Agent
python run_agent.py

### 4. 启动 Dashboard
streamlit run dashboard/app.py

## 功能

- 设备模拟: 5 类虚拟设备，支持故障注入
- Agent 诊断: 扫描设备、查询遥测、趋势分析、知识库检索
- 远程控制: 电机调速、灯光控制
- 实时监控: Streamlit 可视化面板

## 技术栈

Python | MQTT | Streamlit | 规则引擎 | 故障注入
