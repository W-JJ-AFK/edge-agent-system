"""IoT virtual device simulator entry point."""

from __future__ import annotations

import argparse
import logging
import threading
import time
from typing import Sequence

from devices.device_simulator import (
    EnvMonitor,
    MotorController,
    SmartLight,
    TempSensor,
    VibrationSensor,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("Main")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run five MQTT IoT device simulators")
    parser.add_argument("--broker", default="localhost", help="MQTT broker hostname")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Create, connect, and run all virtual devices."""
    args = parse_args(argv)

    print("=" * 60)
    print("IoT Virtual Device Simulator v1.0")
    print("=" * 60)

    connection = {"mqtt_broker": args.broker, "mqtt_port": args.port}
    devices = [
        TempSensor(device_id="temp_sensor_01", **connection),
        VibrationSensor(device_id="vibration_01", **connection),
        MotorController(device_id="motor_01", **connection),
        SmartLight(device_id="light_01", **connection),
        EnvMonitor(device_id="env_01", **connection),
    ]

    running: list[tuple[object, threading.Thread]] = []
    for device in devices:
        if device.connect():
            thread = threading.Thread(
                target=device.run,
                name=f"device-{device.device_id}",
                daemon=True,
            )
            thread.start()
            running.append((device, thread))
            logger.info("Device %s started", device.device_id)
            time.sleep(0.5)
        else:
            logger.error("Device %s failed to start", device.device_id)

    print(f"\n{len(running)} device(s) online")
    print("Telemetry topic: devices/+/telemetry")
    print("Alert topic:     devices/alerts")
    print("Command topic:   devices/+/command")
    print("\nPress Ctrl+C to stop.\n")

    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping all devices...")
    finally:
        for device, _ in running:
            device.disconnect()
        for _, thread in running:
            thread.join(timeout=3.0)

    print("All devices stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

