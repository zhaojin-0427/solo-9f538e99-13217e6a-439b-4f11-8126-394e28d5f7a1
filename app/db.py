"""SQLite 连接与建表。

设计要点：
- 批次以 (batch_name, version) 版本化，更正后重新上传会产生新版本；
- 已发布（status='published'）版本不可变，只允许把 draft 置为 published；
- 测量值带 (batch_name, version, device_id, sample_seq) 唯一约束，
  同批次同设备同样本序号异值会在写入时触发 IntegrityError，由上层整批回滚；
- 核查结果记录当时使用的批次版号，旧结论不被重算覆盖，只新增核查记录。
"""
from __future__ import annotations

import os
import sqlite3

from flask import current_app, g

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS batch_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_name    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'published')),
    content_hash  TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at  TEXT,
    note          TEXT,
    UNIQUE (batch_name, version)
);

CREATE TABLE IF NOT EXISTS measurement (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_name   TEXT NOT NULL,
    version      INTEGER NOT NULL,
    device_id    TEXT NOT NULL,
    sample_seq   INTEGER NOT NULL,
    utc_ts       TEXT NOT NULL,
    metric_value REAL NOT NULL,
    UNIQUE (batch_name, version, device_id, sample_seq),
    FOREIGN KEY (batch_name, version)
        REFERENCES batch_version (batch_name, version)
);

CREATE INDEX IF NOT EXISTS idx_measurement_lookup
    ON measurement (batch_name, version, device_id, utc_ts);

CREATE TABLE IF NOT EXISTS drift_check (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    baseline_batch   TEXT NOT NULL,
    baseline_version INTEGER NOT NULL,
    target_batch     TEXT NOT NULL,
    target_version   INTEGER NOT NULL,
    mad_multiplier   REAL NOT NULL CHECK (mad_multiplier > 0),
    supersedes       INTEGER REFERENCES drift_check (id),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS check_device_result (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id          INTEGER NOT NULL REFERENCES drift_check (id),
    device_id         TEXT NOT NULL,
    baseline_count    INTEGER NOT NULL,
    baseline_median   REAL,
    baseline_mad      REAL,
    threshold_low     REAL,
    threshold_high    REAL,
    target_count      INTEGER NOT NULL,
    segment_count     INTEGER NOT NULL,
    baseline_hash     TEXT,
    target_hash       TEXT,
    note              TEXT,
    UNIQUE (check_id, device_id)
);

CREATE TABLE IF NOT EXISTS anomaly_segment (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id        INTEGER NOT NULL REFERENCES drift_check (id),
    device_id       TEXT NOT NULL,
    seg_index       INTEGER NOT NULL,
    start_seq       INTEGER NOT NULL,
    end_seq         INTEGER NOT NULL,
    start_utc       TEXT NOT NULL,
    end_utc         TEXT NOT NULL,
    sample_count    INTEGER NOT NULL CHECK (sample_count >= 3),
    max_deviation   REAL NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('high', 'low')),
    UNIQUE (check_id, device_id, seg_index)
);

CREATE INDEX IF NOT EXISTS idx_segment_query
    ON anomaly_segment (device_id, start_utc);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DB_PATH"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        g.db = conn
    return g.db


def close_db(exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
