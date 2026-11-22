"""SQLite 连接与建表（仪器电压读数比对）。

设计要点：
- 批次以 (batch_name, version) 版本化；重新上传更正数据产生新版本草稿，
  发布后不可变，旧版统计永久可查。
- 读数为整数微伏，带 (batch_name, version, device_id, sample_seq) 唯一约束；
  同批同仪器同样本序号异值在写入时触发 IntegrityError，由上层整批回滚。
- 每对仪器的比对统计在“发布时”一次性算好并固化：
  * pair_stat 存共测数、最大绝对差及对应样本序号；
  * diff_histogram 按绝对差值分桶计数并建立索引，换容差时只做
    SELECT 聚合（SUM/MAX），无需遍历原始读数或成对样品。
- 每批最多 100000 条读数、最多 6 台仪器，约束在服务层校验并落库为 CHECK。
"""
from __future__ import annotations

import os
import sqlite3

from flask import current_app, g

MAX_READINGS_PER_BATCH = 100_000
MAX_DEVICES_PER_BATCH = 6
MAX_ABS_MICROVOLT = 1_000_000

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS batch_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_name    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'published')),
    content_hash  TEXT NOT NULL,
    row_count     INTEGER NOT NULL CHECK (row_count >= 0
                                          AND row_count <= 100000),
    device_count  INTEGER NOT NULL CHECK (device_count >= 1
                                          AND device_count <= 6),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    published_at  TEXT,
    note          TEXT,
    UNIQUE (batch_name, version)
);

CREATE TABLE IF NOT EXISTS reading (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_name   TEXT NOT NULL,
    version      INTEGER NOT NULL,
    device_id    TEXT NOT NULL,
    sample_seq   INTEGER NOT NULL,
    microvolt    INTEGER NOT NULL
                 CHECK (microvolt >= -1000000 AND microvolt <= 1000000),
    UNIQUE (batch_name, version, device_id, sample_seq),
    FOREIGN KEY (batch_name, version)
        REFERENCES batch_version (batch_name, version)
);

CREATE INDEX IF NOT EXISTS idx_reading_lookup
    ON reading (batch_name, version, device_id, sample_seq);

-- 每对仪器一条统计（仪器按编号字典序规范化为 device_a / device_b）。
CREATE TABLE IF NOT EXISTS pair_stat (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_name       TEXT NOT NULL,
    version          INTEGER NOT NULL,
    device_a         TEXT NOT NULL,
    device_b         TEXT NOT NULL,
    common_count     INTEGER NOT NULL CHECK (common_count >= 0),
    max_abs_diff     INTEGER NOT NULL CHECK (max_abs_diff >= 0),
    max_diff_seq     INTEGER,
    UNIQUE (batch_name, version, device_a, device_b),
    FOREIGN KEY (batch_name, version)
        REFERENCES batch_version (batch_name, version)
);

-- 绝对差值 -> 出现次数 的统计索引；容差查询只扫这张表。
CREATE TABLE IF NOT EXISTS diff_histogram (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_stat_id INTEGER NOT NULL REFERENCES pair_stat (id),
    abs_diff    INTEGER NOT NULL CHECK (abs_diff >= 0),
    freq        INTEGER NOT NULL CHECK (freq >= 1),
    UNIQUE (pair_stat_id, abs_diff)
);

CREATE INDEX IF NOT EXISTS idx_hist_tolerance
    ON diff_histogram (pair_stat_id, abs_diff);
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
