"""持久化测试。"""

from __future__ import annotations

import sqlite3
import time

from agent.storage.db import Store

ALERT = {
    "device_id": "vibration_01",
    "alert_id": "vibration_01-1000-1",
    "alert_type": "vibration_high",
    "value": 0.62,
    "threshold": 0.55,
    "severity": "critical",
    "timestamp": 1000,
}


def test_insert_alert_is_idempotent(tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    try:
        assert store.insert_alert(ALERT) is True
        assert store.insert_alert(ALERT) is False
        assert store.alert_count() == 1
    finally:
        store.close()


def test_insert_alert_without_id_is_ignored(tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    try:
        assert store.insert_alert({"device_id": "x"}) is False
        assert store.alert_count() == 0
    finally:
        store.close()


def test_telemetry_rows(tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    try:
        rows = store.insert_telemetry("vibration_01", 1000.0, {"rms": 0.1, "state": "NORMAL"})
        assert rows == 1  # state 是字符串，不入库
        stats = store.stats()
        assert stats["telemetry_points"] == 1
    finally:
        store.close()


def test_recent_alerts_order(tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    try:
        store.insert_alert({**ALERT, "alert_id": "a", "timestamp": 1})
        store.insert_alert({**ALERT, "alert_id": "b", "timestamp": 2})
        alerts = store.recent_alerts()
        assert len(alerts) == 2
        assert {a["alert_id"] for a in alerts} == {"a", "b"}
    finally:
        store.close()


def test_command_records(tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    try:
        store.record_command("cmd-1", "motor_01", "set_speed", 800, "ok", "目标转速 800", 12.5)
        assert store.command_count() == 1
        # 同一 command_id 覆盖而不是重复
        store.record_command("cmd-1", "motor_01", "set_speed", 800, "timeout", "超时", 10000)
        assert store.command_count() == 1
    finally:
        store.close()


def test_memory_store():
    store = Store(":memory:")
    try:
        assert store.alert_count() == 0
    finally:
        store.close()


def test_alert_ts_falls_back_to_received_at(tmp_path):
    """设备时钟不可信时，ts 必须落在接收时间附近，而不是 1970。

    修复前：ts 直接取 payload["timestamp"]，而固件在 SNTP 同步前发的是
    开机秒数，于是真机告警在库里全变成 1970 年，索引和展示都错。
    """
    store = Store(str(tmp_path / "agent.db"))
    try:
        before = int(time.time())
        assert store.insert_alert({**ALERT, "alert_id": "uptime-1", "timestamp": 3600})
        rows = store.recent_alerts()
        assert rows[0]["ts"] >= before
        assert rows[0]["ts"] <= int(time.time()) + 1
    finally:
        store.close()


def test_telemetry_rows_survive_same_timestamp(tmp_path):
    """主键加 seq 之前，同一浮点 ts 上的两条真实数据会互相覆盖。"""
    store = Store(str(tmp_path / "agent.db"))
    try:
        ts = time.time()
        store.insert_telemetry("vibration_01", ts, {"rms": 0.06}, seq=1)
        store.insert_telemetry("vibration_01", ts, {"rms": 0.99}, seq=2)
        assert store.stats()["telemetry_points"] == 2
    finally:
        store.close()


def test_prune_removes_only_old_rows(tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    try:
        now = time.time()
        store.insert_telemetry("vibration_01", now - 10 * 86400, {"rms": 0.1}, seq=1)
        store.insert_telemetry("vibration_01", now, {"rms": 0.2}, seq=2)
        removed = store.prune(telemetry_days=7)
        assert removed["telemetry"] == 1
        assert store.stats()["telemetry_points"] == 1
    finally:
        store.close()


def test_legacy_telemetry_table_is_migrated(tmp_path):
    """老库的 telemetry 没有 seq 列，打开时必须能自动重建并保留数据。"""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE telemetry (
            device_id TEXT NOT NULL,
            ts        REAL NOT NULL,
            metric    TEXT NOT NULL,
            value     REAL NOT NULL,
            PRIMARY KEY (device_id, ts, metric)
        );
        INSERT INTO telemetry VALUES ('vibration_01', 100.0, 'rms', 0.06);
        """
    )
    conn.commit()
    conn.close()

    store = Store(str(path))
    try:
        assert store.stats()["telemetry_points"] == 1, "迁移不能丢数据"
        # 迁移后新主键生效：同一 ts、不同 seq 不再互相覆盖
        store.insert_telemetry("vibration_01", 100.0, {"rms": 0.07}, seq=5)
        assert store.stats()["telemetry_points"] == 2
    finally:
        store.close()


def test_migration_keeps_telemetry_index(tmp_path):
    """迁移后索引必须还在。

    ALTER TABLE ... RENAME 会把旧索引带到 telemetry_legacy 名下，此时
    CREATE INDEX IF NOT EXISTS 会因为"同名索引已存在"而静默跳过，
    紧接着 DROP TABLE 又把索引删掉 —— 迁移完新表没有索引、也没有任何报错，
    只能靠这条断言兜住。
    """
    path = tmp_path / "legacy_idx.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE telemetry (
            device_id TEXT NOT NULL,
            ts        REAL NOT NULL,
            metric    TEXT NOT NULL,
            value     REAL NOT NULL,
            PRIMARY KEY (device_id, ts, metric)
        );
        CREATE INDEX idx_telemetry_device ON telemetry(device_id, ts);
        INSERT INTO telemetry VALUES ('vibration_01', 100.0, 'rms', 0.06);
        """
    )
    conn.commit()
    conn.close()

    store = Store(str(path))
    try:
        names = {
            row["name"]
            for row in store._conn.execute(  # noqa: SLF001 - 需要看 sqlite_master
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='telemetry'"
            )
        }
        assert "idx_telemetry_device" in names, f"迁移后索引丢失: {names}"
        # 顺带确认索引真的建在 telemetry 上（而不是残留的 telemetry_legacy）
        plan = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM telemetry WHERE device_id='x' AND ts>1"
        ).fetchall()
        assert any("idx_telemetry_device" in str(row["detail"]) for row in plan), (
            f"查询计划未走索引: {[dict(r) for r in plan]}"
        )
    finally:
        store.close()
