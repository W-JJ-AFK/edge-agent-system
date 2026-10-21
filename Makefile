# 端云协同设备运维 Agent —— 常用操作入口
#
#   make install     安装依赖
#   make up          启动 broker + LLM（Docker）
#   make demo        一条命令跑通演示：起 broker → 起模拟设备 → 起 Agent
#
# Windows 上如果没有 make，直接用 README 里列出的等价命令即可。

PY      ?= python
PIP     ?= $(PY) -m pip
COMPOSE ?= docker compose -f deploy/docker-compose.yml

.DEFAULT_GOAL := help
.PHONY: help install up down broker llm logs sim agent dashboard test test-v fault recover demo clean

help:
	@echo "端云协同设备运维 Agent"
	@echo ""
	@echo "  make install    安装 Python 依赖"
	@echo "  make up         启动 mosquitto + ollama"
	@echo "  make broker     只启动 mosquitto（不用 LLM 时够用）"
	@echo "  make down       停止容器"
	@echo "  make sim        启动 5 台虚拟设备"
	@echo "  make agent      启动 Agent 交互终端"
	@echo "  make dashboard  启动 Streamlit 仪表盘"
	@echo "  make test       跑全部单元测试"
	@echo "  make fault      注入一次振动超限故障（20s）"
	@echo "  make recover    发送故障恢复报文"
	@echo "  make demo       打印完整演示步骤"

install:
	$(PIP) install -r requirements.txt

up:
	$(COMPOSE) up -d
	@echo ""
	@echo "首次使用需拉模型：docker compose -f deploy/docker-compose.yml exec ollama ollama pull qwen2.5:3b"

broker:
	$(COMPOSE) up -d mosquitto

llm:
	$(COMPOSE) up -d ollama

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=50

sim:
	$(PY) -m agent.simulator.runner

agent:
	$(PY) -m agent.cli

dashboard:
	$(PY) -m streamlit run agent/dashboard/app.py

test:
	$(PY) -m pytest -q

test-v:
	$(PY) -m pytest -v

fault:
	$(PY) scripts/demo_fault.py --device vibration_01 --seconds 20

recover:
	$(PY) scripts/demo_fault.py --device vibration_01 --recover

demo:
	@echo "1) 启动依赖      make up"
	@echo "2) 启动虚拟设备   make sim          # 另开一个终端"
	@echo "3) 启动 Agent     make agent        # 再开一个终端"
	@echo "4) 注入故障       make fault        # 第四个终端"
	@echo "5) 在 Agent 里问  振动设备现在什么情况？"
	@echo "6) 下发指令       把电机转速调到 900"
	@echo "7) 打开仪表盘     make dashboard"

clean:
	rm -rf data/ .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
