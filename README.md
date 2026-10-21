# 端云协同 IoT 设备运维 Agent

[![ci](https://github.com/W-JJ-AFK/edge-agent-system/actions/workflows/ci.yml/badge.svg)](https://github.com/W-JJ-AFK/edge-agent-system/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![mqtt](https://img.shields.io/badge/MQTT-paho%202.x-660066)
![tests](https://img.shields.io/badge/tests-135%20passed-brightgreen)

用一句话管理一群 IoT 设备：**问状态、查趋势、下指令、要诊断**。

指令不是"建议"，而是带 `command_id` 下行、等设备回执、超时判失败的**闭环操作**。
这个仓库最想证明的一件事：**LLM 不该拿到控制权** —— 控制走确定性规则 + 白名单校验，
LLM 只负责解释发生了什么，两者边界清晰且都能单独降级。

| 指标 | 实测值 |
|---|---|
| 测试 | **135 passed**，10 个测试文件，2 秒内跑完（不需要 broker / LLM / 浏览器） |
| 代码规模 | `agent/` 共 24 个 `.py`，3,733 行 |
| 环境 | Python 3.10 / 3.11 / 3.12（CI 三版本矩阵） |
| 外部依赖 | `paho-mqtt` `streamlit` `requests` `PyYAML`（**没有 LangChain**） |

---

## 目录

- [1. 它解决什么问题](#1-它解决什么问题)
- [2. 60 秒看懂](#2-60-秒看懂)
- [3. 能力清单](#3-能力清单能力--代码--怎么自己验)
- [4. 架构](#4-架构)
- [5. 意图路由：为什么控制必须先判](#5-意图路由为什么控制必须先判)
- [6. 指令闭环](#6-指令闭环)
- [7. 护栏：双向校验](#7-护栏双向校验谁都不信谁)
- [8. 降级：三层，每一层都能用](#8-降级三层每一层都能用)
- [9. 快速开始](#9-快速开始)
- [10. 完整演示（含故障注入）](#10-完整演示含故障注入)
- [11. 测试](#11-测试)
- [12. 配置](#12-配置)
- [13. 明确的边界](#13-明确的边界)
- [14. 目录结构](#14-目录结构)
- [15. 关键设计取舍](#15-关键设计取舍)
- [16. 面试会追问什么](#16-面试会追问什么)

---

## 1. 它解决什么问题

"用大模型管设备"最容易做成一个漂亮的失败案例：

```
用户：电机降速到 800
LLM：建议将电机转速降低到 800 RPM，这样可以……
（电机没有任何变化）
```

模型看起来"懂了"，体验"像是对的"，但**指令从未下发**。
这是一个把 LLM 放在控制路径上的典型事故，而且极其隐蔽。

这个仓库把边界画清楚了：

| 职责 | 由谁负责 | 为什么 |
|---|---|---|
| 理解"用户想干什么" | 正则规则（确定性） | 控制类意图必须 100% 稳定、零延迟、零 token |
| 校验"能不能干" | 白名单 + 范围检查 | 可执行动作必须可预测、可审计、可拒绝 |
| 执行"干了没干成" | `command_id` + 回执 + 超时 | 不能靠"模型说成功了" |
| 解释"为什么出问题" | LLM（可降级到规则引擎） | 这里容错，说错一句不影响设备状态 |

**核心判断：概率模型可以参与"读"，不能参与"写"。**

---

## 2. 60 秒看懂

```bash
# 终端 1：broker + 5 台虚拟设备
make up && make sim

# 终端 2
make agent
```

```
💬 You: 振动传感器现在什么情况？
🤖 Agent: 📈 vibration_01［vibration_sensor］
           状态：在线，最近上报 2s 前

           · rms = 0.062
           · peak = 0.187
           · frequency = 50.1
           · state = NORMAL

           趋势：
           · rms: 均值 0.061, 范围 0.048~0.079, 趋势 stable (+0.0003/s, 24 点)

💬 You: 电机降速到 900
🤖 Agent: ✅ motor_01 -> set_speed(900)：目标转速 900 RPM（往返 12 ms）

💬 You: 为什么振动传感器告警了？
🤖 Agent: 一、设备状态总览 …
           二、异常分析 · vibration_01 触发 vibration_high：0.72（阈值 0.55）
              可能原因：轴承磨损、转子不平衡、安装松动、共振
           三、建议操作 …
           四、风险评估 风险等级：高
```

第三条走本地大模型 + 工具调用；把 Ollama 关掉，它会用同一套四段结构退回规则引擎，
并在末尾追加 `（本地模型暂不可用，已自动切换到规则引擎）`。

---

## 3. 能力清单（能力 → 代码 → 怎么自己验）

| 能力 | 实现位置 | 怎么自己验 |
|---|---|---|
| 五主题 MQTT 契约 | `agent/protocol.py` | `pytest tests/test_protocol.py`（21 项，含与固件对齐的主题/字段断言） |
| 意图路由（四路） | `agent/router.py` → `IntentRouter.route()` | `pytest tests/test_router.py`（28 项，含中文口语与单位换算） |
| **控制优先于 LLM** | `router.py` | `test_control_beats_diagnose_keywords`；`--no-llm` 下控制指令仍执行 |
| 指令闭环 + 回执 | `agent/command_service.py` | `pytest tests/test_command_service.py`（12 项，覆盖 ok/error/timeout/rejected/pending） |
| 能力白名单护栏 | `agent/config.py:CAPABILITIES` | 6 项 `test_reject_*`：未知设备、不支持的 action、负转速、布尔值、NaN、缺参数 |
| **点名设备不支持该动作 → 直接拒绝** | `router.py:_match_control`（不降级到"有这能力的另一台"） | `test_named_device_without_capability_is_rejected`：`action is None` 且一条报文都没发 |
| 指令时效双端校验 | `protocol.is_expired()` + `command_service.send(expires_in_s=)` | `test_expired_command_is_rejected_without_publishing`、`test_simulator_rejects_expired_command` |
| 设备注册与匹配 | `agent/registry.py` | `pytest tests/test_router.py` |
| 状态缓存 + 离线判定 | `agent/cache.py` → `mark_offline()` | `test_cache.py`（13 项，含 TTL 与字段级更新不丢 `last_alert`） |
| 告警双重去重 | `cache.py`（按 `alert_id` + 按 `(device,type)` 30 s 窗） | `test_alert_dedupe_by_id` / `_by_type_window` / `test_alert_after_dedupe_window_is_new` |
| 告警 ID 集合有上限 | `cache.py:_prune_alert_ids()`（TTL + 512 条插入序双保险） | 长时间运行不会因去重集合膨胀而 OOM |
| 趋势拟合 | `cache.py` → `trend()`（最小二乘斜率） | `test_trend_slope_detects_rise` / `test_trend_requires_two_points` |
| 故障知识库 | `agent/knowledge.py` | 诊断报告的"可能原因"就来自这里 |
| 规则诊断兜底 | `agent/engine.py` → `_rule_diagnosis()` | `make agent` 加 `--no-llm`，问诊断类问题 |
| 传感器失效如实表达 | `engine.py:_render_telemetry()` / `_data_summary()` | `test_invalid_frame_is_rendered_as_sensor_failure`：`data.valid=false` 时不显示数字 |
| LLM 工具调用 | `agent/llm/client.py` + `tools.py` | 需要 Ollama；`llm_available=False` 时自动跳过 |
| LLM 探测不会永久误判 | `llm/client.py`：可用则永久缓存，不可用 30 s 后重试 | `test_ollama_probe_retries_then_caches_positive` |
| 只读工具隔离 | `agent/llm/tools.py`（5 个工具全部只读） | 代码里没有任何 `send_command` 类工具 |
| 落库（幂等） | `agent/storage/db.py`（`INSERT OR IGNORE`，PK 含 `seq`） | `test_db.py`（11 项，含同秒多帧不覆盖、旧表迁移后索引仍在） |
| 落库失败不拖垮主链路 | `engine.py:_persist()`（吞异常 + `persist_errors` 计数） | `test_store_failure_does_not_break_message_handling` |
| 数据保留策略 | `db.prune(telemetry_days, alert_days)`，启动时执行 | `test_prune_removes_only_old_rows` |
| 重启回填历史告警 | `cache.py:restore_alerts()`，`engine.py` 构造时调用 | `test_restore_alerts_from_storage`：重填不重复、重发仍去重 |
| 虚拟设备 | `agent/simulator/` | `make sim`，抓包看报文与固件一致 |
| 仪表盘（不阻塞） | `agent/dashboard/app.py`：`send(wait=False)` + 每帧 `poll()` | `make dashboard`，下发指令时页面不卡 10 秒 |
| 端到端闭环 | `tests/test_integration.py` | 5 项：自然语言 → 指令 → 设备执行 → 回执 → 结果可见 |

---

## 4. 架构

```
                       ┌─────────────── 自然语言 ───────────────┐
                       │                                        │
                 ┌─────▼──────┐        ┌──────────────┐        │
                 │   Router   │        │   LLM 通道    │        │
                 │ 规则匹配    │        │ Ollama+tools │        │
                 └─────┬──────┘        └──────┬───────┘        │
         CONTROL       │  QUERY                │ DIAGNOSE       │
        （确定性）      │ （模板）              │（可降级）       │
                       ▼                       ▼                │
              ┌─────────────────┐     ┌─────────────────┐      │
              │ CommandService  │     │  ToolExecutor   │      │
              │ 护栏→发布→等回执 │     │  只读：查缓存    │      │
              └────────┬────────┘     └────────┬────────┘      │
                       │                       │               │
                       └───────────┬───────────┘               │
                                   ▼                           │
                          ┌────────────────┐                   │
                          │  DeviceCache   │◀──────────────────┘
                          │  状态/历史/告警 │
                          └────────┬───────┘
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              ┌──────────┐  ┌──────────┐  ┌──────────┐
              │  SQLite  │  │Dashboard │  │  MqttBus │
              └──────────┘  └──────────┘  └────┬─────┘
                                               │
        ┌──────────────────────────────────────┴──────────┐
        │  devices/{id}/telemetry  ← ┐                    │
        │  devices/{id}/status     ← ├─ 真实 ESP32 节点    │
        │  devices/{id}/alert      ← ┘  或 simulator      │
        │  devices/{id}/cmd        →                      │
        │  devices/{id}/cmd/ack    ←                      │
        └─────────────────────────────────────────────────┘
```

**分层原则：`protocol` / `cache` / `registry` 不 import `paho`，`engine` 不 import `streamlit`。**
这样核心逻辑全部可单测 —— 不需要 broker、不需要 LLM、不需要浏览器。

| 模块 | 职责 | 依赖 |
|---|---|---|
| `protocol.py` | 主题与报文契约（与固件 `main/net/protocol.c` 对应） | 无 |
| `config.py` | 配置 + 设备能力白名单 | PyYAML |
| `cache.py` | 设备状态、历史、告警、趋势 | 无 |
| `registry.py` | 设备查找、能力校验 | 无 |
| `router.py` | 意图分类 | 无 |
| `command_service.py` | 指令下发与回执等待 | 一个可 `publish` 的对象 |
| `engine.py` | 四路分发与渲染 | 以上 |
| `bus.py` | MQTT 收发（paho 1.x/2.x 兼容，断线后台重连） | paho |
| `llm/` | Ollama 客户端 + 工具定义 | requests |
| `storage/db.py` | 落库 | sqlite3 |
| `dashboard/app.py` | 可视化 | streamlit |
| `simulator/` | 虚拟设备 | paho |

---

## 5. 意图路由：为什么控制必须先判

### 5.1 四路，顺序就是设计

```
1. CONTROL   ← 正则命中动作 + 参数，最高优先级，走确定性代码
2. DIAGNOSE  ← 强诊断词（为什么/原因/根因/分析）先于查询
3. QUERY     ← 设备列表 / 告警 / 趋势 / 遥测，模板渲染
4. DIAGNOSE  ← 兜底交给 LLM
```

两条顺序都很关键：

- **CONTROL 必须最先** —— 否则"把阈值调到 0.6"会被当成闲聊。
- **DIAGNOSE 必须早于 QUERY** —— 否则"为什么振动告警了"会被告警关键词吃掉，
  变成"列一下告警列表"，用户拿不到原因。

`test_router.py` 有 28 项覆盖，包括 `上报间隔改成 5 秒 → set_interval=5000`（毫秒换算）、
`调速到 900 → motor_01`（设备消歧，靠 `ACTION_OWNER` 找到能力拥有者）。

### 5.2 参数解析不做隐式猜测

"间隔改成 5" 是 5 秒还是 5 毫秒？`_parse_interval()` 的规则是：
带单位按单位换算；不带单位时 **小于 120 视为秒**，否则视为毫秒。

这条规则写死在代码里，也在 `test_router.py` 里被断言 —— **不做隐式猜测，是可测的猜测。**

---

## 6. 指令闭环

```
Agent                    Broker                   Device
  │                        │                        │
  ├─ 1. 白名单校验 ────────┤                        │
  │     CAPABILITIES       │                        │
  │                        │                        │
  ├─ 2. publish cmd (QoS1)▶│───────▶ devices/{id}/cmd
  │    command_id + expires_at                      │
  │                        │                        ├─ 3. 设备侧二次校验
  │                        │                        │     越界 → result=error
  │                        │◀────── devices/{id}/cmd/ack
  │◀─ 4. 唤醒等待中的线程 ─┤                        │
  │     event.set()        │                        │
  │                        │                        │
  └─ 5. 超时（默认 10s）→ status=timeout            │
```

五种结果，每一种都有测试：

| status | 含义 | 触发条件 |
|---|---|---|
| `ok` | 设备执行成功 | 回执 `result=ok` |
| `error` | 设备拒绝或执行失败 | 回执 `result=error` |
| `rejected` | **本地**护栏拦截，报文没发出去 | 设备不支持该 action / 参数越界 |
| `timeout` | 10 s 内没有回执 | 设备离线 / 网络断 |
| `pending` | 仍在等待（仅内部状态） | 异步观察用 |

`rejected` 与 `error` 必须分开：前者是"我没发"，后者是"我发了它不干"。
排障时这两者的排查方向完全相反。

### 6.1 点名了设备但它没这个能力时，不许"换个设备试试"

一个常见的"聪明"兜底是：`电机重启` → 电机不支持 `reboot` → 于是找"谁支持 `reboot`" →
把重启指令发给了**另一台设备**。

演示里看起来很像 AI，真实运维里是**误操作**：用户说的是电机，结果重启了振动传感器。

现在的规则是：

```python
if spec is not None and route.action is None:
    # 点名了设备但它没这个能力 → 直接拒绝，给出可用动作清单
    return Route(Intent.CONTROL, device_id=spec.device_id, action=None,
                 reason=f"{spec.device_id}（{spec.type}）不支持 {action}，可用动作：…")
# 只有「用户没点名」时才允许按能力找归属设备
if spec is None:
    owner = registry.first_supporting(action)
```

线上行为：

```
💬 电机重启
🚫 motor_01（motor_controller）不支持 reboot，可用动作：set_speed、start、stop
```

**一条报文都没发出去。** `test_named_device_without_capability_is_rejected` 断言了
`action is None` 且 publisher 的调用记录为空 —— **"没有副作用"比"提示得体"更重要。**

### 6.2 `expires_at` 是两端都实现的

网络延迟会让 5 分钟前的"降速"在设备已停机时才到达。所以：

- **Agent 侧**：`send(expires_in_s=30)` 生成报文后**立刻自检一次**，已过期就返回
  `rejected` 且不发布。
- **设备侧**：固件 `handle_command_message` / `simulator/base.py:_on_message` 收到后再判一次，
  过期回 `result=error, detail="command expired"`。

只在 Agent 侧做等于没做（设备可能被别的发布者直接投喂）；只在设备侧做则白占用一次 QoS1 往返。
`is_expired()` 同时接受 `dict` 和 JSON 字符串，因为两个位置传进来的形态不一样 ——
这类"看起来对称其实不对称"的地方最容易漏测。

---

## 7. 护栏：双向校验，谁都不信谁

```python
CAPABILITIES = {
    "vibration_sensor": {
        "set_threshold": (0.05, 4.0),       # g
        "set_interval":  (500.0, 60000.0),  # ms
        "reboot": None,                     # 无参数
    },
    "motor_controller": {
        "set_speed": (0.0, 3000.0),         # RPM
        ...
    },
    ...
}
```

- **Agent 侧**：`registry.validate()` 在下发前拦截，连报文都不发。
- **设备侧**：固件 `main/app_main.c` 收到指令后再校验一遍，越界返回 `error`。

为什么要两边都做：Agent 可能是旧版本、配置可能被改错、链路上可能有别的发布者。
嵌入式的基本假设是**不信任上游**。而且两边都校验才能区分"没发"和"发了被拒"。

LLM 通道**没有任何写操作工具**。`tools.py` 里 5 个工具全是 `get_*` / `list_*` / `query_*`。
即使模型被提示注入，它也没有能力下发指令 —— **这是能力隔离，不是提示词约束。**

---

## 8. 降级：三层，每一层都能用

| 缺什么 | 表现 | 怎么验证 |
|---|---|---|
| 没有 Ollama | `llm_available=False`，诊断走 `_rule_diagnosis()` 四段式报告 | `python -m agent.cli --no-llm` |
| 没有 broker | 启动打印警告，静态查询（设备列表、帮助）仍可用 | 见 CI 的 `smoke` job |
| 没装 streamlit | CLI 与核心逻辑完全不受影响 | `dashboard/app.py` 是唯一 `import streamlit` 的文件 |

诊断报告的四段结构是固定的：**设备状态总览 / 异常分析 / 建议操作 / 风险评估**。
LLM 可用时由模型填充，不可用时由知识库 + 缓存数据填充，并在末尾追加
`（本地模型暂不可用，已自动切换到规则引擎）`。

**输出结构不随降级改变**，这样上层（仪表盘、告警转发）不需要判断"当前是哪种模式"。

---

## 9. 快速开始

```bash
git clone https://github.com/W-JJ-AFK/edge-agent-system && cd edge-agent-system
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 1) 起 broker（只要 MQTT，不要 LLM 也行）
docker compose -f deploy/docker-compose.yml up -d mosquitto
#    或者本机 mosquitto：mosquitto -c deploy/mosquitto.conf

# 2) 起 5 台虚拟设备
python -m agent.simulator.runner

# 3) 起 Agent
python -m agent.cli
```

单次提问（适合脚本 / 无人值守）：

```bash
python -m agent.cli --no-llm --once "振动传感器现在什么情况？"
```

`--once` 会先等数据再来一帧（`--wait`，默认 3 s）再回答，否则订阅刚建立就问，
必然拿到"无数据"。等待条件分两种：点名了设备就等它一台；没点名则要**配置里的每台设备**
都至少来过一帧（`test_wait_requires_all_configured_devices`）。
只等"任意一台"会出现"电机 1 s 一帧到了就回答，2 s 一帧的振动传感器还是空的"这种半残结果。

想用 LLM 诊断，再多一步：

```bash
docker compose -f deploy/docker-compose.yml up -d ollama
docker compose -f deploy/docker-compose.yml exec ollama ollama pull qwen2.5:3b
```

没有 Docker 也可以：本机跑 `mosquitto`、`ollama serve`，配置项在 `config.yaml` 里改。

---

## 10. 完整演示（含故障注入）

```bash
# 终端 1
make up && make sim

# 终端 2
make agent

# 终端 3：注入振动超限，持续 20 秒
make fault
#   💥 注入故障：vibration_01 (vibration_sensor) → vibration_high=0.72 阈值 0.55
```

然后在 Agent 里依次问：

```
💬 振动传感器现在什么情况？     → rms 从 0.06 涨到 0.72，state=ALARM
💬 查看告警                    → vibration_high，severity=critical
💬 为什么振动传感器告警了？     → 四段式诊断报告
💬 把振动阈值调到 0.9           → 指令闭环，返回回执
💬 电机降速到 900               → 另一个设备，验证多设备路由
💬 有哪些设备                   → 5 台，2 台标记 hardware
💬 电机重启                     → 🚫 motor_01 不支持 reboot，可用动作：set_speed、start、stop
💬 把电机转速调到 5000          → 🚫 参数超出范围：set_speed 需在 0~3000 之间
```

后两条是**故意的**：它们证明护栏在真实链路上生效，而且拒绝发生在发布之前
（broker 上不会出现这条报文）。演示时先跑这两条，比事后解释更有说服力。

再开一个终端看仪表盘：

```bash
make dashboard     # http://localhost:8501
```

**故障恢复**：`make recover`，或等 20 秒自动结束，再问一次状态会回到 NORMAL。

> `make fault` 是以"设备身份"向 broker 发报文的（`scripts/demo_fault.py`），
> 所以即使不接真实硬件也能演示完整链路。它和真机走的是同一条主题、同一套字段。

---

## 11. 测试

```bash
make test          # 135 passed
make test-v        # 带用例名
```

| 文件 | 数量 | 覆盖什么 |
|---|---|---|
| `test_router.py` | 28 | 四路分类、中文口语、单位换算、设备消歧、CONTROL 优先 |
| `test_protocol.py` | 21 | 主题构造/解析、报文编解码、`command_id`、过期判定 |
| `test_reliability.py` | 20 | **可靠性契约**：时间戳不可信要回退、过期指令必拒、点名设备无能力必拒、坏 store 不拖垮链路、Ollama 探测重试、`--wait` 要等齐所有设备 |
| `test_cache.py` | 13 | TTL 离线、字段级更新保留 `last_alert`、双重去重、趋势、重启回填 |
| `test_command_service.py` | 12 | ok/error/timeout/rejected、护栏拦截、并发等待 |
| `test_simulator.py` | 12 | **一致性护栏**：模拟器实现了 `CAPABILITIES` 声明的每个动作，且越界必拒 |
| `test_db.py` | 11 | 建表、幂等写入、同秒多帧不覆盖、保留策略、旧表迁移 |
| `test_engine.py` | 9 | 四路渲染、规则诊断兜底、HELP、失效传感器渲染 |
| `test_integration.py` | 5 | **端到端**：自然语言 → 指令 → 模拟设备执行 → 回执 → 结果可见 |
| `test_cli.py` | 4 | 装配层：组件接线、TTL 传递、无 broker 时仍能回答 |
| **合计** | **135** | 全部不需要 broker / Ollama / 浏览器，2 秒内跑完 |

`test_integration.py` 用一个内存 `LoopbackPublisher` 把 Agent 和模拟设备直接连起来，
所以"端到端"是真的端到端，不是把中间层 mock 掉。

> `test_reliability.py` 是被一轮代码审查催生的：所有 P0/P1 级问题——设备时钟没同步、
> 指令过期没人管、点名设备被悄悄换掉、落库抛异常炸掉整个回调——都属于
> "单元测试全绿但线上会出事"的类型。修复这类问题的验收标准不是"改完了"，
> 而是**有一条会红的断言**。

> `test_cli.py` 是被一个真实 bug 催生的：`build_engine` 里把 `default_ttl_s` 写成了
> `default_ttl`，单元测试全绿，命令行一启动就 `TypeError`。
> **装配层没有测试，等于靠运气上线。**

---

## 12. 配置

`config.yaml`（环境变量优先级更高，便于容器化）：

```yaml
mqtt:
  host: localhost
  port: 1883
  keepalive: 30

llm:
  enabled: true
  base_url: http://localhost:11434
  model: qwen2.5:3b        # 1.5b 在长上下文下指令遵循不足
  temperature: 0.1         # 诊断要稳定，不要发散
  max_tool_rounds: 3

agent:
  offline_after_periods: 3   # 连续 3 个上报周期没消息即判离线
  command_timeout_s: 10

devices:
  vibration_01:
    type: vibration_sensor
    interval_s: 2
    source: hardware         # hardware = ESP32 真机；simulator = 虚拟设备
```

可用环境变量：`AGENT_MQTT_HOST`、`AGENT_MQTT_PORT`、`AGENT_LLM_BASE_URL`、
`AGENT_LLM_MODEL`、`AGENT_LLM_DISABLED`、`AGENT_DB_PATH`。

设备 ID 必须与固件 Kconfig 的 `CONFIG_APP_DEVICE_*_ID` 一致 —— 这是两个仓库唯一的强耦合点。

---

## 13. 明确的边界

- **没有认证授权**：任何能连上 broker 的客户端都能发指令。生产需要 mTLS + ACL。
- **没有多租户**：一套配置管所有设备，没有租户隔离。
- **没有流式输出**：LLM 回复整段返回，长诊断要等几秒。
- **没有真正的向量检索**：知识库是静态规则匹配，不是 RAG。
- **没有 OTA 编排**：只支持 `reboot`，不支持推固件。
- **没有生产级告警通道**：告警只落 SQLite + 仪表盘，没接钉钉/企业微信/邮件。
- **没有性能压测**：按 5 台设备设计，千级设备的连接与缓存策略未验证。
- **SQLite 单文件**：约 21 万点/天以内够用（启动时按保留期清理），更大规模需换时序库。
- **没有降采样归档**：保留期内原始点全留，过期直接删，没有"按小时聚合后长期保存"。

---

## 14. 目录结构

```
edge-agent-system/
├── agent/
│   ├── protocol.py          # ★ 报文契约（与固件唯一耦合点）
│   ├── config.py            # ★ 配置 + CAPABILITIES 白名单
│   ├── cache.py             # ★ 状态/历史/告警/趋势，含离线判定与去重
│   ├── registry.py          #   设备查找与能力校验
│   ├── router.py            # ★ 四路意图路由
│   ├── command_service.py   # ★ 指令闭环（发布/等待/超时）
│   ├── engine.py            #   四路分发与渲染
│   ├── knowledge.py         #   故障知识库
│   ├── bus.py               #   MQTT（paho 1.x/2.x 兼容 + 断线重连）
│   ├── cli.py               #   命令行入口
│   ├── llm/
│   │   ├── client.py        #   Ollama /api/chat + tools
│   │   ├── tools.py         #   5 个只读工具
│   │   └── prompts.py       #   按意图裁剪上下文的系统提示词
│   ├── storage/db.py        #   SQLite（alerts/telemetry/commands，幂等写）
│   ├── simulator/           #   5 类虚拟设备，报文与固件一致
│   └── dashboard/app.py     #   Streamlit（cache_resource + fragment）
├── tests/                   # 135 项，无外部依赖
├── scripts/demo_fault.py    # 故障注入器
├── deploy/                  # docker-compose + mosquitto.conf
├── config.yaml
└── Makefile
```

---

## 15. 关键设计取舍

**ADR-1：LLM 不碰控制路径。**
控制交确定性代码，LLM 只做解释。这不是"LLM 不够强"，而是
**可执行动作必须可预测、可审计、可回滚**，这三点和概率模型的本质冲突。

**ADR-2：不用 LangChain。**
实际只需要"发 messages、收 tools"，包一层换来 40+ 个依赖，出错时堆栈穿过 5 层抽象。
现在 `agent/llm/client.py` 直接 POST `{base_url}/api/chat`，行为完全可控。

**ADR-3：持久订阅，而不是 `sleep(3)` 快照。**
如果每次提问前 `sleep(3)` 等数据、再 `sleep(2)` 收结果，中间到达的告警会直接丢失。
现在 `MqttBus` 起一个常驻订阅线程，所有消息进 `DeviceCache`，提问只是读缓存。
**这才是实时系统该有的样子：数据一直在流，查询是瞬时的。**

**ADR-4：SQLite 而不是 InfluxDB。**
5 台设备 × 2 s 一帧 ≈ 21 万点/天，SQLite 单文件完全够用，且零运维。
换来的是"部署只需要 Python"。设备数上到百级、需要降采样与保留策略时再换时序库 ——
现在换是过度设计。

**ADR-5：告警双键去重。**
只按 `alert_id` 去重，设备重启后 ID 可能变，会重复入库；
只按 `(device, type)` 时间窗去重，同期两种不同故障会被合并。
两者都用：`alert_id` 精确去重，`(device, type)` 30 s 窗兜底，且**重复时不刷新窗口时间戳** ——
否则持续告警会让窗口无限延长，真正的第二次故障被吞掉。

**ADR-6：paho 1.x / 2.x 统一。**
2.0 改了回调签名（多了 `properties`）。`bus.create_client()` 里试一次 `CallbackAPIVersion`
是否存在，不存在就走 1.x 路径。升级依赖不该让整个项目起不来。

**ADR-7：遥测表主键为什么必须含 `seq`。**
如果主键是 `(device_id, ts, metric)`，而设备 1 Hz 上报、`ts` 只精确到秒，
同一秒内的两帧会互相覆盖 —— 数据静默丢失，而且丢得毫无痕迹
（`INSERT OR IGNORE` 本来就是为了"重复不报错"，于是"覆盖"也被当成"重复"）。

现在主键是 `(device_id, ts, seq, metric)`，`seq` 是设备侧的帧序号（固件和模拟器都发）。
没有 `seq` 的历史数据写 `-1`。老库需要启动时迁移，而 **SQLite 不支持改主键**，
只能走"改名 → 建新表 → 拷数据 → 删旧表"，并且有 `test_legacy_telemetry_table_is_migrated` 兜着。

> 迁移里有一个很隐蔽的坑：`ALTER TABLE ... RENAME` 会把索引一起带到旧表名下，
> 于是 `CREATE INDEX IF NOT EXISTS` 静默跳过，`DROP TABLE` 再把索引一起删掉 ——
> 结果是**迁移成功、数据完整、但索引全没了**，而且不报任何错。
> `test_migration_keeps_telemetry_index` 就是用 `EXPLAIN QUERY PLAN` 断言索引真的还在。

**ADR-8：落库失败不该让消息处理崩掉。**
`engine.handle_message()` 如果直接调 `self._store.insert_telemetry(...)`，
一旦 SQLite 被别处锁住、磁盘满、表结构不对，异常会沿着 paho 的回调线程抛出去 ——
**后续所有消息都不再被处理**，而缓存里还在正常更新，看起来"一切正常"。

现在所有写库走 `engine._persist(what, method_name, *args)`：捕获异常、计入 `persist_errors`、
继续处理下一条。注意这里传的是**方法名**而不是绑定方法 ——
传绑定方法会在 `if self._store is None` 判空**之前**就求值，`None` 时直接 `AttributeError`。

同类问题还有两处：`CommandService._pending` 会因设备永远不回执而无限增长
（现在按 `max(timeout*3, 30) s` TTL 清理）；`cache` 的告警 ID 去重集合会随运行时间膨胀
（现在上限 512 条，超了按插入序淘汰最旧的）。

**ADR-9：broker 晚于 Agent 启动时必须还能连上。**
paho 的自动重连只在"曾经连接成功过"之后才生效。如果 `connect()` 时 broker 还没起来，
`connect()` 抛异常 → 直接返回 → `loop_start()` 从没被调用 → **永久连不上**，
而且表面上看只是"没有数据"。

现在 `start()` 用 `connect_async()` + `loop_start()` + `reconnect_delay_set(min=1, max=10)`：
连接动作交给网络线程，失败就按退避重试。CLI 和仪表盘两处都改了 ——
只改一处等于没改，因为它们各自持有一个 bus。

---

## 16. 面试会追问什么

| 问题 | 关键点 | 代码位置 |
|---|---|---|
| 为什么不让 LLM 直接下发指令？ | 可预测/可审计/可回滚 vs 概率模型；反面案例是"建议了但没执行" | `router.py` |
| 意图识别为什么不用 LLM 分类？ | 控制指令必须 100% 确定；正则对固定句式准确率更高且零延迟零 token | `router.py` |
| 指令超时怎么办？会重复执行吗？ | `command_id` 幂等 + `expires_at` 过期丢弃；超时只报告不自动重试 | `command_service.py`、`protocol.is_expired` |
| `rejected` 和 `error` 为什么要分开？ | 一个没发出去、一个发了被拒，排查方向相反 | `command_service.py` |
| 告警去重为什么不能只用 `alert_id`？ | 设备重启 ID 变；只用时间窗又会合并不同故障 | `cache.py` |
| 为什么 `_type_ts` 重复时不刷新？ | 否则持续告警让窗口无限延长，第二次真故障被吞 | `cache.py` |
| 没有 broker 时为什么还能回答？ | 缓存与静态查询不依赖连接；`bus.start()` 失败只告警不抛 | `cli.py` |
| LLM 被提示注入会怎样？ | 工具全只读，没有下发能力 —— 能力隔离而非提示词约束 | `llm/tools.py` |
| 怎么保证和固件协议一致？ | 唯一契约点，两端字段表逐项对齐，改一边必须改另一边 | `protocol.py` / `protocol.c` |
| 135 个测试为什么跑得这么快？ | 核心层不 import paho/streamlit，用内存 Loopback 替掉传输 | `tests/conftest.py` |

---

## 17. 相关仓库

配套固件：[**W-JJ-AFK/esp32-sensor-node**](https://github.com/W-JJ-AFK/esp32-sensor-node)
—— ESP32-S3 边缘节点（100 Hz 采样 + 去重力 + 滑窗 RMS + 三态迟滞告警 + 断网积压补发）。

两个仓库通过 [第 6 节](#6-指令闭环)的主题矩阵通信，可以独立部署、独立演示：
没有硬件时用 `simulator`，没有 Agent 时固件照样在本地检测告警。
