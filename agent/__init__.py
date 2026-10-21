"""设备运维 Agent 包。

分层：
    agent.core      意图路由、设备注册表、指令服务、共享缓存
    agent.llm       Ollama 客户端、工具定义、提示词
    agent.storage   SQLite 持久化
    agent.simulator 虚拟设备（无硬件时提供同格式数据）
    agent.dashboard Streamlit 面板
"""

__version__ = "2.0.0"
