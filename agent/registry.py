"""设备注册表：设备清单与指令护栏的单一来源。

旧版本把设备 ID 列表硬编码在 3 个文件里，新增设备要改 3 处；
现在统一从 config.yaml 读入，并在这里提供「这条指令该发给谁、参数是否合法」的判断。
"""

from __future__ import annotations

import math

from .config import DeviceSpec

#: 用户口语到设备类型的映射（顺序无关，先命中先返回）
TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "temperature_sensor": ("温度", "温湿度", "温度传感器", "temp"),
    "vibration_sensor": ("振动", "震动", "抖动", "vibration"),
    "motor_controller": ("电机", "马达", "motor"),
    "smart_light": ("灯", "照明", "light"),
    "env_monitor": ("环境", "co2", "二氧化碳", "空气质量", "env"),
}

#: 每个动作的「归属设备类型」：用户没点名设备时优先给这类设备，
#: 否则「上报间隔改成5秒」会被随机分给配置里排在最前的环境监测器。
ACTION_OWNER: dict[str, str] = {
    "set_speed": "motor_controller",
    "stop": "motor_controller",
    "start": "motor_controller",
    "set_brightness": "smart_light",
    "on": "smart_light",
    "off": "smart_light",
    "toggle": "smart_light",
    "set_threshold": "vibration_sensor",
    "set_interval": "temperature_sensor",
    "reboot": "temperature_sensor",
}


class DeviceRegistry:
    def __init__(self, specs: dict[str, DeviceSpec]) -> None:
        self._specs = dict(specs)

    # ---------------- 查询 ----------------

    def all(self) -> list[DeviceSpec]:
        return sorted(self._specs.values(), key=lambda s: s.device_id)

    def get(self, device_id: str | None) -> DeviceSpec | None:
        if not device_id:
            return None
        return self._specs.get(device_id)

    def by_type(self, device_type: str) -> list[DeviceSpec]:
        return [s for s in self.all() if s.device_type == device_type]

    def find(self, text: str) -> DeviceSpec | None:
        """从自然语言里解析出设备：先精确匹配 ID，再按类型关键词。"""
        if not text:
            return None
        lowered = text.lower()
        for spec in self.all():
            if spec.device_id.lower() in lowered:
                return spec
        for device_type, keywords in TYPE_KEYWORDS.items():
            if any(kw in lowered for kw in keywords):
                candidates = self.by_type(device_type)
                if candidates:
                    return candidates[0]
        return None

    def first_supporting(self, action: str) -> DeviceSpec | None:
        """找到第一个支持指定动作的设备（用于用户没点名设备的情况）。

        优先返回该动作的「归属设备类型」，避免设备清单顺序影响路由结果。
        """
        owner = ACTION_OWNER.get(action)
        if owner is not None:
            for spec in self.by_type(owner):
                if action in spec.capabilities:
                    return spec
        for spec in self.all():
            if action in spec.capabilities:
                return spec
        return None

    def ttl_map(self, offline_after_periods: int) -> dict[str, float]:
        """每个设备的离线判定阈值 = 上报周期 × 容忍周期数。"""
        periods = max(1, offline_after_periods)
        return {
            spec.device_id: max(3.0, spec.interval_s * periods)
            for spec in self.all()
        }

    # ---------------- 护栏 ----------------

    def validate(self, spec: DeviceSpec | None, action: str, value: float | None) -> str | None:
        """校验指令，合法返回 None，否则返回人类可读的错误原因。"""
        if spec is None:
            return "未知设备"
        if action not in spec.capabilities:
            supported = "、".join(sorted(spec.capabilities)) or "无"
            return f"{spec.device_id} 不支持动作 '{action}'（支持：{supported}）"

        expected = spec.capabilities[action]
        if expected is None:
            return None

        if value is None:
            return f"动作 '{action}' 缺少参数 value"
        if isinstance(value, bool):
            return "参数类型非法：不支持布尔值"
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return f"参数必须是数字，收到 {value!r}"
        if not math.isfinite(numeric):
            return "参数非法：不能为 NaN 或无穷大"

        low, high = expected
        if not (low <= numeric <= high):
            return f"参数超出范围：{action} 需在 {low:g}~{high:g} 之间，收到 {numeric:g}"
        return None
