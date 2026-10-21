"""虚拟设备启动器。

    python -m agent.simulator.runner                       # 全部 5 台
    python -m agent.simulator.runner --devices motor_01    # 只跑电机
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from .devices import EnvMonitor, MotorController, SmartLight, TempSensor, VibrationSensor

FACTORY = {
    "temp_sensor_01": TempSensor,
    "vibration_01": VibrationSensor,
    "motor_01": MotorController,
    "light_01": SmartLight,
    "env_01": EnvMonitor,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="虚拟 IoT 设备模拟器")
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument(
        "--devices",
        default=",".join(FACTORY),
        help=f"逗号分隔的设备 ID，可选：{','.join(FACTORY)}",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("simulator")

    wanted = [d.strip() for d in args.devices.split(",") if d.strip()]
    unknown = [d for d in wanted if d not in FACTORY]
    if unknown:
        logger.error("未知设备: %s", ", ".join(unknown))
        return 2

    threads: list[tuple[object, threading.Thread]] = []
    for device_id in wanted:
        device = FACTORY[device_id](device_id=device_id, mqtt_broker=args.broker, mqtt_port=args.port)
        if not device.connect():
            logger.error("%s 连接 broker 失败，跳过", device_id)
            continue
        thread = threading.Thread(target=device.run, name=f"dev-{device_id}", daemon=True)
        thread.start()
        threads.append((device, thread))
        time.sleep(0.3)

    print(f"\n{len(threads)} 台设备已上线，broker={args.broker}:{args.port}")
    print("Ctrl+C 停止\n")

    try:
        while threads:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止设备...")
    finally:
        for device, _ in threads:
            device.disconnect()
        for _, thread in threads:
            thread.join(timeout=3.0)
    print("已全部停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
