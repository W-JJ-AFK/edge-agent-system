"""SQLite 持久化层。

为什么不是 InfluxDB：本场景 5 台设备 × 2s ≈ 21 万点/天，SQLite 单表加索引查询是毫秒级，
引入时序库要多维护一个服务、多一套查询语言，收益不成正比。
真正的迁移触发条件：数据量到百万点/天，或者需要连续聚合/降采样策略。

两个与可靠性相关的设计：

1. **告警时间戳以本地接收时间为准**
   设备可能没有同步时间（无 RTC + SNTP 未完成），它报的 ``timestamp`` 可能是
   开机秒数。直接落库会把告警记到 1970 年，索引和展示全错。这里统一走
   :func:`agent.protocol.resolve_timestamp`：不可信就用接收时间。

2. **遥测主键带 seq**
   早期主键是 ``(device_id, ts, metric)``，``ts`` 是浮点秒。正常 2s 一条不会撞，
   但重连补发、缩短上报周期到 500ms 时，同一设备的同一指标可能落在同一个浮点值上，
   ``INSERT OR REPLACE`` 会静默覆盖掉一条真实数据。加上设备自增的 ``seq`` 之后，
   只有"同一时刻同一条报文"才会覆盖，那本来就应该覆盖。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..protocol import resolve_timestamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_id    TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    alert_type  TEXT NOT NULL,
    value       REAL,
    threshold   REAL,
    severity    TEXT,
    ts          INTEGER,
    received_at REAL,
    raw         TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS telemetry (
    device_id TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    seq       INTEGER NOT NULL DEFAULT -1,
    metric    TEXT    NOT NULL,
    value     REAL    NOT NULL,
    PRIMARY KEY (device_id, ts, seq, metric)
);
CREATE INDEX IF NOT EXISTS idx_telemetry_device ON telemetry(device_id, ts);

CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    device_id  TEXT,
    action     TEXT,
    value      REAL,
    status     TEXT,
    detail     TEXT,
    issued_at  REAL,
    latency_ms REAL
);
"""

_TELEMETRY_V2 = """
CREATE TABLE telemetry (
    device_id TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    seq       INTEGER NOT NULL DEFAULT -1,
    metric    TEXT    NOT NULL,
    value     REAL    NOT NULL,
    PRIMARY KEY (device_id, ts, seq, metric)
);
"""


class Store:
    """线程安全的轻量 DAO。"""

    def __init__(self, path: str = "data/agent.db") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_telemetry()
            self._conn.commit()

    # ---------------- 迁移 ----------------

    def _migrate_telemetry(self) -> None:
        """老库的 telemetry 主键没有 seq，重建表并搬数据。

        不能只 `ALTER TABLE ADD COLUMN`：SQLite 改不了主键，而主键正是要修的东西。
        数据量小（单机演示级），整体重建比写半个 upsert 更安全。
        """
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(telemetry)")
        }
        if not columns or "seq" in columns:
            return

        # 顺序很关键：索引名是全局唯一的。`ALTER TABLE ... RENAME` 会把旧索引
        # 一起带到 telemetry_legacy 名下，此时 CREATE INDEX IF NOT EXISTS 会因为
        # "同名索引已存在"而**静默跳过**，紧接着 DROP TABLE 又把那个索引一起删掉
        # —— 迁移完成后新表一个索引都没有，查询从毫秒级退化成全表扫，
        # 而且没有任何报错。所以必须在 DROP 之后再建索引。
        self._conn.executescript(
            "ALTER TABLE telemetry RENAME TO telemetry_legacy;"
            + _TELEMETRY_V2
            + "INSERT INTO telemetry(device_id, ts, seq, metric, value)"
            " SELECT device_id, ts, -1, metric, value FROM telemetry_legacy;"
            "DROP TABLE telemetry_legacy;"
            "CREATE INDEX IF NOT EXISTS idx_telemetry_device ON telemetry(device_id, ts);"
        )

    # ---------------- 写入 ----------------

    def insert_alert(self, payload: dict[str, Any]) -> bool:
        """按 alert_id 幂等写入，重复返回 False。"""
        alert_id = str(payload.get("alert_id") or "")
        if not alert_id:
            return False

        # 优先用上层（engine）在同一条消息里打的接收时间：缓存里有、库里也有，
        # 两处时间必须一致，否则仪表盘按 received_at 排序会和内存里的顺序对不上。
        received_at = _as_float(payload.get("received_at")) or time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO alerts"
                "(alert_id, device_id, alert_type, value, threshold, severity, ts, received_at, raw)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    alert_id,
                    str(payload.get("device_id", "")),
                    str(payload.get("alert_type", "")),
                    _as_float(payload.get("value")),
                    _as_float(payload.get("threshold")),
                    str(payload.get("severity", "info")),
                    int(resolve_timestamp(payload, received_at)),
                    received_at,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def insert_telemetry(
        self,
        device_id: str,
        ts: float,
        data: dict[str, Any],
        seq: int | None = None,
    ) -> int:
        rows = [
            (device_id, float(ts), -1 if seq is None else int(seq), key, float(value))
            for key, value in data.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO telemetry(device_id, ts, seq, metric, value)"
                " VALUES(?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def record_command(
        self,
        command_id: str,
        device_id: str,
        action: str,
        value: float | None,
        status: str,
        detail: str,
        latency_ms: float | None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO commands"
                "(command_id, device_id, action, value, status, detail, issued_at, latency_ms)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (command_id, device_id, action, value, status, detail, time.time(), latency_ms),
            )
            self._conn.commit()

    # ---------------- 保留策略 ----------------

    def prune(
        self,
        telemetry_days: float | None = 7.0,
        alert_days: float | None = 90.0,
    ) -> dict[str, int]:
        """按时间清理历史数据，返回各表删除的行数。

        没有保留策略时，21 万点/天意味着一年 7600 万行，``agent.db`` 会涨到 GB 级，
        而演示环境没人会去清。这里给一个保守默认：遥测留 7 天、告警留 90 天。
        """
        now = time.time()
        removed = {"telemetry": 0, "alerts": 0}
        with self._lock:
            if telemetry_days is not None and telemetry_days > 0:
                cursor = self._conn.execute(
                    "DELETE FROM telemetry WHERE ts < ?",
                    (now - telemetry_days * 86400.0,),
                )
                removed["telemetry"] = max(0, cursor.rowcount)
            if alert_days is not None and alert_days > 0:
                cursor = self._conn.execute(
                    "DELETE FROM alerts WHERE received_at < ?",
                    (now - alert_days * 86400.0,),
                )
                removed["alerts"] = max(0, cursor.rowcount)
            self._conn.commit()
        return removed

    # ---------------- 读取 ----------------

    def recent_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT alert_id, device_id, alert_type, value, threshold, severity, ts"
                " FROM alerts ORDER BY received_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def alert_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
        return int(row["n"]) if row else 0

    def command_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM commands").fetchone()
        return int(row["n"]) if row else 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            alerts = self._conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()
            telemetry = self._conn.execute("SELECT COUNT(*) AS n FROM telemetry").fetchone()
            commands = self._conn.execute("SELECT COUNT(*) AS n FROM commands").fetchone()
        return {
            "path": self._path,
            "alerts": int(alerts["n"]) if alerts else 0,
            "telemetry_points": int(telemetry["n"]) if telemetry else 0,
            "commands": int(commands["n"]) if commands else 0,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
