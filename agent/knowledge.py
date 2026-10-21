"""故障诊断知识库。

结构化字典而非向量检索：当前只有 4 类告警、条目少且需要精确匹配，
引入向量库属于过度设计。等设备类型增长到几十类、需要模糊语义匹配时再换 RAG。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class KnowledgeEntry:
    alert_type: str
    severity: str
    causes: tuple[str, ...]
    actions: tuple[str, ...]
    keywords: tuple[str, ...] = field(default=())

    def as_text(self) -> str:
        causes = "；".join(self.causes)
        actions = "；".join(self.actions)
        return (
            f"[{self.alert_type}] 严重级别: {self.severity}\n"
            f"  可能原因: {causes}\n"
            f"  建议操作: {actions}"
        )


_ENTRIES: tuple[KnowledgeEntry, ...] = (
    KnowledgeEntry(
        alert_type="temp_high",
        severity="critical",
        causes=("散热风扇故障", "环境温度过高", "传感器漂移", "设备过载"),
        actions=("检查散热系统", "降低设备负载", "校准传感器", "启用备用冷却"),
        keywords=("温度", "高温", "过热", "temp", "temperature"),
    ),
    KnowledgeEntry(
        alert_type="vibration_high",
        severity="critical",
        causes=("轴承磨损", "转子不平衡", "安装松动", "共振"),
        actions=("停机检查轴承", "做动平衡校正", "紧固安装螺栓", "改变运行频率"),
        keywords=("振动", "震动", "抖动", "vibration", "rms"),
    ),
    KnowledgeEntry(
        alert_type="current_overload",
        severity="critical",
        causes=("机械卡滞", "绕组短路", "电源波动", "负载突变"),
        actions=("立即降速", "检查机械传动", "测量绕组电阻", "检查电源质量"),
        keywords=("电流", "过载", "过流", "current", "overload"),
    ),
    KnowledgeEntry(
        alert_type="co2_high",
        severity="warning",
        causes=("通风不足", "人员密集", "燃烧源泄漏", "新风系统故障"),
        actions=("开启新风系统", "疏散人员", "检查通风管道", "启用空气净化"),
        keywords=("二氧化碳", "co2", "空气", "通风", "空气质量"),
    ),
)

_BY_TYPE = {entry.alert_type: entry for entry in _ENTRIES}


def all_entries() -> tuple[KnowledgeEntry, ...]:
    return _ENTRIES


def get(alert_type: str) -> KnowledgeEntry | None:
    return _BY_TYPE.get(alert_type)


def lookup(text: str) -> KnowledgeEntry | None:
    """按关键词在用户输入里匹配告警类型。"""
    lowered = text.lower()
    for entry in _ENTRIES:
        if entry.alert_type.replace("_", "") in lowered.replace(" ", ""):
            return entry
        if any(kw in lowered for kw in entry.keywords):
            return entry
    return None


def alert_types() -> list[str]:
    return [entry.alert_type for entry in _ENTRIES]
