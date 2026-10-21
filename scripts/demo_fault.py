"""故障注入器：主动把某台设备「弄坏」一段时间，用来演示完整告警链路。

    python scripts/demo_fault.py                          # vibration_01 振动超限 20s
    python scripts/demo_fault.py --device motor_01        # 电机过载
    python scripts/demo_fault.py --device temp_sensor_01 --seconds 60
    python scripts/demo_fault.py --recover                # 只发一条恢复报文

它做的事情和真实设备固件一模一样：往 devices/{id}/alert 发 QoS1 告警，
同时把遥测数据改成异常值，让 Agent 的缓存、诊断引擎、仪表盘都能看到。

设计要点：
  * 同一轮故障复用同一个 alert_id —— 用来验证 Agent 侧「按 ID + 时间窗」双重去重，
    不会因为设备 30s 重发一次就刷屏。
  * 恢复时先发正常遥测再发 status=online，模拟故障排除。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent import protocol  # noqa: E402
from agent.bus import create_client  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

# 按设备类型预置的故障剧本：(alert_type, threshold, alert_value, severity, 异常遥测)
SCENARIOS: dict[str, tuple[str, float, float, str, dict[str, Any]]] = {
    "temperature_sensor": (
        "temp_high",
        38.0,
        41.2,
        "critical",
        {"temperature": 41.2, "humidity": 61.0},
    ),
    "vibration_sensor": (
        "vibration_high",
        0.55,
        0.72,
        "critical",
        {"rms": 0.72, "peak": 2.31, "frequency": 52.4, "state": "ALARM"},
    ),
    "motor_controller": (
        "current_overload",
        5.0,
        5.8,
        "critical",
        {"speed": 1500, "current": 5.8, "temperature": 46.5},
    ),
    "env_monitor": (
        "co2_high",
        1500.0,
        2100,
        "warning",
        {"pm25": 34.0, "co2": 2100, "temperature": 24.6, "humidity": 55.0},
    ),
}

# 恢复后的正常读数
HEALTHY: dict[str, dict[str, Any]] = {
    "temperature_sensor": {"temperature": 24.5, "humidity": 55.0},
    "vibration_sensor": {"rms": 0.06, "peak": 0.19, "frequency": 50.1, "state": "NORMAL"},
    "motor_controller": {"speed": 1500, "current": 2.0, "temperature": 40.2},
    "env_monitor": {"pm25": 30.0, "co2": 780, "temperature": 24.0, "humidity": 55.0},
}


def publish_telemetry(client, device_id: str, device_type: str, data: dict[str, Any], seq: int) -> None:
    client.publish(
        protocol.telemetry_topic(device_id),
        json.dumps(
            {
                "device_id": device_id,
                "device_type": device_type,
                "timestamp": int(time.time()),
                "seq": seq,
                "data": data,
            },
            ensure_ascii=False,
        ),
        qos=0,
    )


def publish_status(client, device_id: str, state: str) -> None:
    client.publish(
        protocol.status_topic(device_id),
        json.dumps({"device_id": device_id, "state": state, "timestamp": int(time.time())}),
        qos=1,
        retain=True,
    )


def publish_alert(
    client,
    device_id: str,
    alert_id: str,
    alert_type: str,
    value: float,
    threshold: float,
    severity: str,
) -> None:
    client.publish(
        protocol.alert_topic(device_id),
        json.dumps(
            {
                "device_id": device_id,
                "alert_id": alert_id,
                "alert_type": alert_type,
                "value": value,
                "threshold": threshold,
                "severity": severity,
                "timestamp": int(time.time()),
            },
            ensure_ascii=False,
        ),
        qos=1,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="IoT 设备故障注入器")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--device", default="vibration_01", help="目标设备 ID")
    parser.add_argument("--seconds", type=float, default=20.0, help="故障持续秒数")
    parser.add_argument("--period", type=float, default=2.0, help="遥测发送间隔秒数")
    parser.add_argument("--recover", action="store_true", help="只发送恢复报文")
    args = parser.parse_args(argv)

    config = AgentConfig.load(args.config)
    spec = config.devices.get(args.device)
    if spec is None:
        print(f"❌ 配置里没有设备 {args.device}，可选：{', '.join(config.devices)}")
        return 2

    scenario = SCENARIOS.get(spec.device_type)
    if scenario is None:
        print(f"❌ 设备类型 {spec.device_type} 没有预置故障剧本（可选：{', '.join(SCENARIOS)}）")
        return 2
    alert_type, threshold, alert_value, severity, bad_data = scenario
    good_data = HEALTHY.get(spec.device_type, {})

    client = create_client(f"demo-fault-{args.device}")
    try:
        client.connect(config.mqtt.host, config.mqtt.port, config.mqtt.keepalive)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 连接 broker {config.mqtt.host}:{config.mqtt.port} 失败：{exc}")
        print("   请先执行：docker compose -f deploy/docker-compose.yml up -d mosquitto")
        return 1
    client.loop_start()
    time.sleep(0.3)

    seq = 0
    try:
        if args.recover:
            publish_telemetry(client, args.device, spec.device_type, good_data, seq)
            publish_status(client, args.device, "online")
            time.sleep(0.2)
            print(f"✅ 已发送 {args.device} 的恢复报文")
            return 0

        alert_id = f"{args.device}-{alert_type}-{int(time.time())}"
        print(f"💥 注入故障：{args.device} ({spec.device_type}) → {alert_type}={alert_value} 阈值 {threshold}")
        print(f"   持续 {args.seconds:.0f}s，每 {args.period:.0f}s 一条遥测；alert_id={alert_id}")

        publish_status(client, args.device, "online")
        deadline = time.time() + args.seconds
        last_alert = 0.0
        while time.time() < deadline:
            publish_telemetry(client, args.device, spec.device_type, bad_data, seq)
            seq += 1
            # 30s 重发一次告警 —— 刻意复用同一个 alert_id
            if time.time() - last_alert >= 30.0:
                publish_alert(client, args.device, alert_id, alert_type, alert_value, threshold, severity)
                last_alert = time.time()
            time.sleep(args.period)

        publish_telemetry(client, args.device, spec.device_type, good_data, seq)
        publish_status(client, args.device, "online")
        time.sleep(0.3)
        print("✅ 故障结束，已发送恢复遥测")
        print("   此时可以让 Agent 诊断一次，例如：")
        print(f'   python -m agent.cli --no-llm --once "{args.device} 现在什么状态"')
        return 0
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
