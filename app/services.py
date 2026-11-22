"""业务逻辑层：上传草稿 / 发布并固化统计 / 仪器对容差查询。

所有写操作都在单个 SQLite 事务中完成；统计生成或保存失败时整体回滚，
新版不会被发布，草稿保留可重试。容差查询只读取发布时固化的统计索引
（pair_stat / diff_histogram），不触碰原始读数表 reading。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from collections import defaultdict
from itertools import combinations
from typing import Any

from .db import (
    MAX_ABS_MICROVOLT,
    MAX_DEVICES_PER_BATCH,
    MAX_READINGS_PER_BATCH,
    get_db,
)

# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #


class ServiceError(Exception):
    """业务校验失败（HTTP 400/404/409 由路由层映射）。"""

    status = 400


class NotFoundError(ServiceError):
    status = 404


class ConflictError(ServiceError):
    status = 409


class CommitFailure(ServiceError):
    """模拟/真实的数据库写入失败：调用方看到失败且数据未变、版本未发布。"""

    status = 500


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #

HEADER_ALIASES = {
    "batch": "batch_name",
    "batch_name": "batch_name",
    "批次": "batch_name",
    "批次名": "batch_name",
    "device": "device_id",
    "device_id": "device_id",
    "仪器": "device_id",
    "仪器编号": "device_id",
    "设备": "device_id",
    "设备编号": "device_id",
    "sample_seq": "sample_seq",
    "seq": "sample_seq",
    "样本序号": "sample_seq",
    "序号": "sample_seq",
    "microvolt": "microvolt",
    "microvolts": "microvolt",
    "uv": "microvolt",
    "voltage": "microvolt",
    "value": "microvolt",
    "读数": "microvolt",
    "电压": "microvolt",
    "电压读数": "microvolt",
    "微伏": "microvolt",
}

REQUIRED = ("batch_name", "device_id", "sample_seq", "microvolt")


def _parse_int(raw: str, field: str, lineno: int) -> int:
    """严格解析整数：拒绝 3.0、空格包裹之外的花哨写法。"""
    text = raw.strip()
    if not text:
        raise ServiceError(f"第 {lineno} 行 {field} 为空")
    try:
        return int(text, 10)
    except ValueError as exc:
        raise ServiceError(
            f"第 {lineno} 行 {field} 必须是整数，收到: {raw!r}"
        ) from exc


def parse_reading_csv(text: str) -> dict[str, Any]:
    """解析电压读数 CSV 并完成全部校验，不触碰数据库。

    同批次同仪器同样本序号：
    - 完全重复（含读数）→ 去重并计数；
    - 异值冲突 → 抛 ConflictError，拒绝整份文件。
    同时校验：整数微伏、|读数|<=1,000,000、每批 ≤10 万条、每批 ≤6 台仪器。
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ServiceError("CSV 为空")

    cols: list[str] = []
    for name in header:
        key = HEADER_ALIASES.get(name.strip().lower())
        if key is None and name.strip() in HEADER_ALIASES:
            key = HEADER_ALIASES[name.strip()]
        cols.append(key or name.strip())
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        raise ServiceError(f"CSV 缺少必需列: {', '.join(missing)}")

    # batch -> (device, seq) -> microvolt
    seen: dict[str, dict[tuple[str, int], int]] = defaultdict(dict)
    rows_by_batch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    duplicates_by_batch: dict[str, int] = defaultdict(int)

    for lineno, raw_row in enumerate(reader, start=2):
        if not raw_row or all(not cell.strip() for cell in raw_row):
            continue
        if len(raw_row) < len(cols):
            raw_row = raw_row + [""] * (len(cols) - len(raw_row))
        record = dict(zip(cols, raw_row))

        batch_name = record["batch_name"].strip()
        device_id = record["device_id"].strip()
        if not batch_name or not device_id:
            raise ServiceError(f"第 {lineno} 行批次/仪器不能为空")
        sample_seq = _parse_int(record["sample_seq"], "样本序号", lineno)
        microvolt = _parse_int(record["microvolt"], "电压读数(微伏)", lineno)
        if abs(microvolt) > MAX_ABS_MICROVOLT:
            raise ServiceError(
                f"第 {lineno} 行读数 {microvolt} 超出范围 "
                f"[-{MAX_ABS_MICROVOLT}, {MAX_ABS_MICROVOLT}] 微伏"
            )

        key = (device_id, sample_seq)
        batch_seen = seen[batch_name]
        if key in batch_seen:
            if batch_seen[key] == microvolt:
                duplicates_by_batch[batch_name] += 1
                continue
            raise ConflictError(
                f"冲突: 批次 {batch_name} 仪器 {device_id} 样本序号 {sample_seq} "
                f"存在不同读数（第 {lineno} 行），整份文件已拒绝"
            )
        batch_seen[key] = microvolt
        rows_by_batch[batch_name].append(
            {
                "batch_name": batch_name,
                "device_id": device_id,
                "sample_seq": sample_seq,
                "microvolt": microvolt,
            }
        )

    if not rows_by_batch:
        raise ServiceError("CSV 没有有效数据行")

    for batch_name, rows in rows_by_batch.items():
        if len(rows) > MAX_READINGS_PER_BATCH:
            raise ServiceError(
                f"批次 {batch_name} 有 {len(rows)} 条读数，"
                f"超过每批最多 {MAX_READINGS_PER_BATCH} 条"
            )
        device_count = len({r["device_id"] for r in rows})
        if device_count > MAX_DEVICES_PER_BATCH:
            raise ServiceError(
                f"批次 {batch_name} 有 {device_count} 台仪器，"
                f"超过每批最多 {MAX_DEVICES_PER_BATCH} 台"
            )

    return {
        "rows_by_batch": rows_by_batch,
        "duplicates_by_batch": dict(duplicates_by_batch),
    }


# --------------------------------------------------------------------------- #
# 哈希
# --------------------------------------------------------------------------- #


def _hash_rows(rows: list[tuple[Any, ...]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_hash(rows: list[dict[str, Any]]) -> str:
    ordered = sorted(
        (r["device_id"], r["sample_seq"], r["microvolt"]) for r in rows
    )
    return _hash_rows(ordered)


# --------------------------------------------------------------------------- #
# 上传草稿
# --------------------------------------------------------------------------- #


def _next_version(conn: sqlite3.Connection, batch_name: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM batch_version "
        "WHERE batch_name=?",
        (batch_name,),
    ).fetchone()
    return int(row["v"])


def upload_csv(text: str, note: str | None = None,
               fail_before_commit: bool = False) -> list[dict[str, Any]]:
    """上传读数 CSV：可含多个批次，每个批次生成一个 draft 新版本。

    全量解析通过后才开启写事务；任何写入失败整体回滚，不产生新版本。
    """
    parsed = parse_reading_csv(text)
    rows_by_batch = parsed["rows_by_batch"]
    dup_by_batch = parsed["duplicates_by_batch"]

    conn = get_db()
    created: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for batch_name, rows in rows_by_batch.items():
            version = _next_version(conn, batch_name)
            chash = content_hash(rows)
            device_count = len({r["device_id"] for r in rows})
            if fail_before_commit:
                raise CommitFailure("注入的写入失败：事务将回滚")
            conn.execute(
                "INSERT INTO batch_version (batch_name, version, status, "
                "content_hash, row_count, device_count, note) "
                "VALUES (?,?, 'draft', ?, ?, ?, ?)",
                (batch_name, version, chash, len(rows), device_count, note),
            )
            conn.executemany(
                "INSERT INTO reading (batch_name, version, device_id, "
                "sample_seq, microvolt) VALUES (?,?,?,?,?)",
                [
                    (
                        batch_name,
                        version,
                        r["device_id"],
                        r["sample_seq"],
                        r["microvolt"],
                    )
                    for r in rows
                ],
            )
            created.append(
                {
                    "batch_name": batch_name,
                    "version": version,
                    "row_count": len(rows),
                    "device_count": device_count,
                    "content_hash": chash,
                    "duplicate_rows_skipped": dup_by_batch.get(batch_name, 0),
                }
            )
        conn.commit()
    except CommitFailure:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ConflictError(f"写入冲突，已整体回滚: {exc}") from exc
    except Exception:
        conn.rollback()
        raise

    return created


# --------------------------------------------------------------------------- #
# 发布：一次性计算并固化仪器对统计
# --------------------------------------------------------------------------- #


def _load_readings_map(conn: sqlite3.Connection, batch_name: str,
                       version: int) -> dict[str, dict[int, int]]:
    """返回 {device_id: {sample_seq: microvolt}}。"""
    out: dict[str, dict[int, int]] = defaultdict(dict)
    cur = conn.execute(
        "SELECT device_id, sample_seq, microvolt FROM reading "
        "WHERE batch_name=? AND version=?",
        (batch_name, version),
    )
    for device_id, sample_seq, microvolt in cur:
        out[device_id][sample_seq] = microvolt
    return out


def compute_pair_stats(
    readings_by_device: dict[str, dict[int, int]],
) -> list[dict[str, Any]]:
    """对每对仪器只比较双方都测过的样品，生成统计与差值直方图。

    最大绝对差并列时取样本序号最小者（确定性规则）。
    """
    results: list[dict[str, Any]] = []
    for device_a, device_b in combinations(sorted(readings_by_device), 2):
        map_a = readings_by_device[device_a]
        map_b = readings_by_device[device_b]
        common_seqs = map_a.keys() & map_b.keys()

        histogram: dict[int, int] = defaultdict(int)
        max_abs_diff = 0
        max_diff_seq: int | None = None
        for seq in common_seqs:
            diff = abs(map_a[seq] - map_b[seq])
            histogram[diff] += 1
            # 严格大于才更新：并列时保留先遇到的，而 seq 来自集合无序，
            # 因此用“差值更大，或差值相同但序号更小”的显式规则。
            if max_diff_seq is None or diff > max_abs_diff or (
                diff == max_abs_diff and seq < max_diff_seq
            ):
                max_abs_diff = diff
                max_diff_seq = seq

        results.append(
            {
                "device_a": device_a,
                "device_b": device_b,
                "common_count": len(common_seqs),
                "max_abs_diff": max_abs_diff,
                "max_diff_seq": max_diff_seq,
                "histogram": dict(sorted(histogram.items())),
            }
        )
    return results


def publish_batch(batch_name: str, version: int | None = None,
                  fail_before_commit: bool = False) -> dict[str, Any]:
    """发布草稿版本：同事务内计算并写入全部仪器对统计。

    统计生成或保存失败 → 回滚，版本保持 draft，可更正后重试。
    """
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if version is None:
            row = conn.execute(
                "SELECT version FROM batch_version WHERE batch_name=? "
                "ORDER BY version DESC LIMIT 1",
                (batch_name,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"批次不存在: {batch_name}")
            version = int(row["version"])

        bv = conn.execute(
            "SELECT * FROM batch_version WHERE batch_name=? AND version=?",
            (batch_name, version),
        ).fetchone()
        if bv is None:
            raise NotFoundError(f"批次版本不存在: {batch_name} v{version}")
        if bv["status"] == "published":
            raise ConflictError(
                f"{batch_name} v{version} 已发布，不可重复发布或修改"
            )

        readings_by_device = _load_readings_map(conn, batch_name, version)
        pair_stats = compute_pair_stats(dict(readings_by_device))

        if fail_before_commit:
            raise CommitFailure("注入的发布失败：统计事务将回滚")

        for stat in pair_stats:
            cur = conn.execute(
                "INSERT INTO pair_stat (batch_name, version, device_a, "
                "device_b, common_count, max_abs_diff, max_diff_seq) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    batch_name,
                    version,
                    stat["device_a"],
                    stat["device_b"],
                    stat["common_count"],
                    stat["max_abs_diff"],
                    stat["max_diff_seq"],
                ),
            )
            pair_id = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO diff_histogram (pair_stat_id, abs_diff, freq) "
                "VALUES (?,?,?)",
                [(pair_id, diff, freq) for diff, freq in stat["histogram"].items()],
            )

        conn.execute(
            "UPDATE batch_version SET status='published', "
            "published_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE batch_name=? AND version=?",
            (batch_name, version),
        )
        conn.commit()
    except (NotFoundError, ConflictError, CommitFailure):
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise

    return {
        "batch_name": batch_name,
        "version": version,
        "status": "published",
        "pair_count": len(
            [s for s in pair_stats if s["common_count"] >= 0]
        ),
    }


# --------------------------------------------------------------------------- #
# 批次/版本查询
# --------------------------------------------------------------------------- #


def list_batches() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM batch_version ORDER BY batch_name, version"
    ).fetchall()


def list_published() -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM batch_version WHERE status='published' "
        "ORDER BY batch_name, version"
    ).fetchall()


def get_published_version(batch_name: str, version: int) -> sqlite3.Row:
    row = get_db().execute(
        "SELECT * FROM batch_version WHERE batch_name=? AND version=? "
        "AND status='published'",
        (batch_name, version),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"已发布版本不存在: {batch_name} v{version}（草稿不可用于比对）"
        )
    return row


def list_devices(batch_name: str, version: int) -> list[str]:
    """该版本下的仪器编号（去重、字典序）。"""
    rows = get_db().execute(
        "SELECT DISTINCT device_id FROM reading WHERE batch_name=? AND version=? "
        "ORDER BY device_id",
        (batch_name, version),
    ).fetchall()
    return [r["device_id"] for r in rows]


def published_devices_map() -> dict[str, list[dict[str, Any]]]:
    """{batch_name: [{version, devices:[...]}]}，供网页下拉框渲染。"""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for bv in list_published():
        out[bv["batch_name"]].append(
            {
                "version": bv["version"],
                "devices": list_devices(bv["batch_name"], bv["version"]),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 容差查询：只读统计索引
# --------------------------------------------------------------------------- #


def _canonical_pair(device_x: str, device_y: str) -> tuple[str, str]:
    if device_x == device_y:
        raise ServiceError("请选择两台不同的仪器")
    return tuple(sorted((device_x, device_y)))  # type: ignore[return-value]


def compare_devices(batch_name: str, version: int,
                    device_x: str, device_y: str,
                    tolerance: int) -> dict[str, Any]:
    """按容差查询仪器对比对结果，仅访问 pair_stat / diff_histogram。

    返回共测数、绝对差超过容差的样品数、最大绝对差及对应样本序号。
    共测数为 0 时由上层给出明确提示（仍返回结构化结果）。
    """
    if not isinstance(tolerance, int) or tolerance < 0:
        raise ServiceError("容差必须是非负整数微伏")
    device_a, device_b = _canonical_pair(device_x, device_y)
    get_published_version(batch_name, version)
    conn = get_db()

    pair = conn.execute(
        "SELECT * FROM pair_stat WHERE batch_name=? AND version=? "
        "AND device_a=? AND device_b=?",
        (batch_name, version, device_a, device_b),
    ).fetchone()
    if pair is None:
        # 两台仪器至少有一台不在该版本中
        raise NotFoundError(
            f"批次 {batch_name} v{version} 中找不到仪器对 "
            f"{device_a} / {device_b} 的统计"
        )

    common_count = int(pair["common_count"])
    if common_count == 0:
        return {
            "batch_name": batch_name,
            "version": version,
            "device_a": device_a,
            "device_b": device_b,
            "tolerance_uv": tolerance,
            "common_count": 0,
            "over_tolerance_count": 0,
            "max_abs_diff": 0,
            "max_diff_seq": None,
            "has_common": False,
            "message": "两台仪器没有共测样品，无法比对",
        }

    # 仅对直方图索引做聚合：abs_diff > tolerance 的频数之和。
    row = conn.execute(
        "SELECT COALESCE(SUM(freq), 0) AS n FROM diff_histogram "
        "WHERE pair_stat_id=? AND abs_diff > ?",
        (pair["id"], tolerance),
    ).fetchone()
    over_count = int(row["n"])

    return {
        "batch_name": batch_name,
        "version": version,
        "device_a": device_a,
        "device_b": device_b,
        "tolerance_uv": tolerance,
        "common_count": common_count,
        "over_tolerance_count": over_count,
        "max_abs_diff": int(pair["max_abs_diff"]),
        "max_diff_seq": pair["max_diff_seq"],
        "has_common": True,
        "message": None,
    }


def list_pair_stats(batch_name: str, version: int) -> list[sqlite3.Row]:
    get_published_version(batch_name, version)
    return get_db().execute(
        "SELECT * FROM pair_stat WHERE batch_name=? AND version=? "
        "ORDER BY device_a, device_b",
        (batch_name, version),
    ).fetchall()
