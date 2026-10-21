"""缓存测试：字段级更新、告警幂等、离线判定、趋势拟合。"""

from __future__ import annotations

import time

from agent.cache import DeviceCache


def _telemetry(device_id="vibration_01", rms=0.06, ts=None, seq=1):
    return dict(
        device_id=device_id,
        device_type="vibration_sensor",
        data={"rms": rms, "temperature": 41.0},
        ts=ts,
        seq=seq,
    )


def _alert(alert_id="a-1", device_id="vibration_01", alert_type="vibration_high", ts=None):
    return (
        {
            "device_id": device_id,
            "alert_id": alert_id,
            "alert_type": alert_type,
            "value": 0.62,
            "threshold": 0.55,
            "severity": "critical",
        },
        ts,
    )


def test_telemetry_keeps_last_alert(cache: DeviceCache):
    """回归测试：遥测更新不能把告警字段冲掉（旧版本的真实 bug）。"""
    cache.update_telemetry("vibration_01", "vibration_sensor", {"rms": 0.7}, ts=1000.0)
    payload, _ = _alert()
    assert cache.update_alert(payload, ts=1001.0) is True

    cache.update_telemetry("vibration_01", "vibration_sensor", {"rms": 0.1}, ts=1002.0)

    state = cache.get("vibration_01")
    assert state is not None
    assert state.last_alert is not None
    assert state.last_alert["alert_type"] == "vibration_high"
    assert state.last_data["rms"] == 0.1


def test_alert_dedupe_by_id(cache: DeviceCache):
    payload, _ = _alert(alert_id="dup-1")
    assert cache.update_alert(payload, ts=1000.0) is True
    assert cache.update_alert(payload, ts=1001.0) is False
    assert len(cache.recent_alerts()) == 1


def test_alert_dedupe_by_type_window(cache: DeviceCache):
    first, _ = _alert(alert_id="a-1")
    second, _ = _alert(alert_id="a-2")  # 设备重发时换了新 ID，但仍是同一故障
    assert cache.update_alert(first, ts=1000.0) is True
    assert cache.update_alert(second, ts=1010.0) is False
    assert len(cache.recent_alerts()) == 1


def test_alert_after_dedupe_window_is_new(cache: DeviceCache):
    first, _ = _alert(alert_id="a-1")
    second, _ = _alert(alert_id="a-2")
    cache.update_alert(first, ts=1000.0)
    assert cache.update_alert(second, ts=1000.0 + 31) is True


def test_offline_detection(cache: DeviceCache):
    cache.update_telemetry("vibration_01", "vibration_sensor", {"rms": 0.1}, ts=1000.0)
    assert cache.is_online("vibration_01", now=1001.0) is True
    # 2s 周期 × 3 = 6s TTL
    assert cache.is_online("vibration_01", now=1010.0) is False


def test_status_offline_overrides(cache: DeviceCache):
    cache.update_telemetry("vibration_01", "vibration_sensor", {"rms": 0.1}, ts=1000.0)
    cache.update_status("vibration_01", "offline", ts=1001.0)
    assert cache.is_online("vibration_01", now=1001.5) is False


def test_mark_offline_returns_changed(cache: DeviceCache):
    cache.update_telemetry("vibration_01", "vibration_sensor", {"rms": 0.1}, ts=1000.0)
    changed = cache.mark_offline(now=1100.0)
    assert "vibration_01" in changed
    assert cache.mark_offline(now=1200.0) == []  # 已经标记过，不重复返回


def test_trend_slope_detects_rise(cache: DeviceCache):
    for index in range(10):
        cache.update_telemetry(
            "motor_01",
            "motor_controller",
            {"current": 2.0 + index * 0.1},
            ts=1000.0 + index,
        )
    trend = cache.trend("motor_01", "current")
    assert trend is not None
    assert trend.direction == "上升"
    assert 0.09 < trend.slope_per_s < 0.11
    assert trend.samples == 10


def test_trend_requires_two_points(cache: DeviceCache):
    cache.update_telemetry("motor_01", "motor_controller", {"current": 2.0}, ts=1000.0)
    assert cache.trend("motor_01", "current") is None


def test_history_is_bounded():
    cache = DeviceCache(history_size=5)
    for index in range(20):
        cache.update_telemetry("x", "t", {"v": index}, ts=float(index))
    assert len(cache.history("x")) == 5
    assert cache.history("x")[-1]["data"]["v"] == 19


def test_metrics_lists_numeric_only(cache: DeviceCache):
    cache.update_telemetry(
        "vibration_01", "vibration_sensor", {"rms": 0.1, "state": "NORMAL"}, ts=time.time()
    )
    assert cache.metrics("vibration_01") == ["rms"]


def test_restore_alerts_from_storage(cache: DeviceCache):
    """重启回填：历史告警要能看到，且不能被同一条重发再算一次。"""
    now = time.time()
    # 与 Store.recent_alerts() 一致：按 received_at 倒序（最新在前）
    rows = [
        {"alert_id": "old-2", "device_id": "motor_01",
         "alert_type": "current_overload", "value": 5.8, "threshold": 5.0,
         "severity": "critical", "ts": int(now - 5)},
        {"alert_id": "old-1", "device_id": "vibration_01",
         "alert_type": "vibration_high", "value": 0.72, "threshold": 0.55,
         "severity": "critical", "ts": int(now - 10)},
    ]
    assert cache.restore_alerts(rows) == 2
    assert len(cache.recent_alerts()) == 2
    # recent_alerts 最新的在最前
    assert cache.recent_alerts()[0]["alert_id"] == "old-2"

    # 再回填一次不应重复
    assert cache.restore_alerts(rows) == 0
    assert len(cache.recent_alerts()) == 2

    # 设备重发同一 alert_id 仍然被去重
    assert cache.update_alert({**rows[1], "timestamp": int(now)}) is False
    assert len(cache.recent_alerts()) == 2


def test_restore_alerts_feeds_diagnosis_window(cache: DeviceCache):
    """回填的告警必须落在 alerts_since 窗口里，否则诊断报告还是"无活跃告警"。"""
    now = time.time()
    cache.restore_alerts([
        {"alert_id": "recent-1", "device_id": "vibration_01",
         "alert_type": "vibration_high", "value": 0.8, "threshold": 0.55,
         "severity": "critical", "ts": int(now - 30)},
    ])
    assert len(cache.alerts_since(now - 300)) == 1
