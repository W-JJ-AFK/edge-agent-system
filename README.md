markdown
# 🤖 边缘设备运维 AI Agent 系统

基于 MQTT + Ollama LLM 的 IoT 设备智能运维系统，实现设备模拟、实时监控、AI 智能诊断和远程控制。

## 🏗️ 架构
虚拟设备层 (5类IoT设备)
↓ MQTT
Agent 引擎层 (规则引擎 + LLM智能诊断)
↓
可视化层 (Streamlit Dashboard)

text

## ✨ 核心功能

- **设备模拟**: 5 类虚拟 IoT 设备，支持故障注入和命令响应
- **LLM 智能诊断**: 基于 Ollama qwen2.5 本地模型，自然语言交互
- **规则引擎**: 关键词匹配快速响应，LLM 不可用时的后备方案
- **远程控制**: 电机调速、灯光控制等设备指令下发
- **实时监控**: Streamlit 可视化面板，设备状态一目了然
- **知识库**: 内置故障诊断知识库，自动匹配解决建议

## 🚀 快速启动

### 环境要求
- Python 3.10+
- Mosquitto MQTT Broker
- Ollama（可选，用于 LLM 智能诊断）

### 1. 安装依赖
```bash
pip install paho-mqtt streamlit langchain langchain-ollama
2. 启动 Mosquitto
bash
mosquitto -d
3. 启动设备模拟器
bash
python main.py
4. 启动 Agent
bash
python run_agent.py
5. 启动 Dashboard（可选）
bash
streamlit run dashboard/app.py
🎮 使用示例
text
💬 You: 检查所有设备状态

🤖 Agent:
### 设备状态总览
所有设备均在线...

### 异常分析
- temp_sensor_01：温度趋势轻微下降...

### 建议操作
建议进行定期维护和检查...

### 风险评估
当前风险等级：中等
📁 项目结构
text
edge-agent-project/
├── devices/              # 虚拟设备层
│   ├── base_device.py    # 设备基类
│   └── device_simulator.py  # 5类具体设备
├── agent/                # Agent 引擎层
│   ├── tools.py          # 工具函数集
│   ├── agent_engine.py   # Agent 主引擎
│   └── llm_engine.py     # LLM 诊断引擎
├── dashboard/            # 可视化层
│   └── app.py            # Streamlit 面板
├── main.py               # 设备模拟器入口
├── run_agent.py          # Agent 入口
└── requirements.txt      # 依赖清单
🛠️ 技术栈
Python MQTT Ollama qwen2.5 Streamlit LangChain IoT
