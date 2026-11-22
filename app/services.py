"""业务逻辑层：上传/发布/漂移核查/重算/查询。

所有写操作都在单个 SQLite 事务中完成；冲突或写入失败时整体回滚，
当前已发布的数据与既有核查结论不受影响。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import get_db

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
    """模拟/真实的数据库写入失败，保证调用方看到失败且数据未变。"""

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
    "设备": "device_id",
    "设备编号": "device_id",
    "sample_seq": "sample_seq",
    "seq": "sample_seq",
    "样本序号": "sample_seq",
    "序号": "sample_seq",
    "utc_ts": "utc_ts",
    "timestamp": "utc_ts",
    "utc": "utc_ts",
    "utc时刻": "utc_ts",
    "时刻": "utc_ts",
    "时间": "utc_ts",
    "metric_value": "metric_value",
    "value": "metric_value",
    "指标值": "metric_value",
    "测量值": "metric_value",
}

REQUIRED = ("batch_name", "device_id", "sample_seq", "utc_ts", "metric_value")


def _parse_utc(raw: str) -> str:
    """把 UTC 时刻归一化为 'YYYY-MM-DDTHH:MM:SS.ffffffZ' 文本。"""
    raw = raw.strip()
    if not raw:
        raise ValueError("空时刻")
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # 兼容 "YYYY-MM-DD HH:MM:SS"
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_measurement_csv(text: str) -> list[dict[str, Any]]:
    """解析测量 CSV，完成全部字段校验。

    同批次同设备同样本序号：
    - 完全重复（含值与时刻）→ 去重并计数；
    - 异值冲突 → 抛 ConflictError，拒绝整个文件。
    本函数不触碰数据库，保证“先全量校验、后写入”。
    """
    buffer = io.StringIO(text)
    reader = csv.reader(buffer)
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

    rows: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, int], tuple[str, float]] = {}
    duplicates_by_batch: dict[str, int] = {}
    for lineno, raw_row in enumerate(reader, start=2):
        if not raw_row or all(not cell.strip() for cell in raw_row):
            continue
        if len(raw_row) < len(cols):
            raw_row = raw_row + [""] * (len(cols) - len(raw_row))
        record = dict(zip(cols, raw_row))
        try:
            batch_name = record["batch_name"].strip()
            device_id = record["device_id"].strip()
            sample_seq = int(record["sample_seq"].strip())
            utc_ts = _parse_utc(record["utc_ts"])
            metric_value = float(record["metric_value"].strip())
        except (ValueError, KeyError) as exc:
            raise ServiceError(f"第 {lineno} 行字段非法: {exc}") from exc
        if not batch_name or not device_id:
            raise ServiceError(f"第 {lineno} 行批次/设备不能为空")
        if not math.isfinite(metric_value):
            raise ServiceError(f"第 {lineno} 行指标值必须是有限数值")

        key = (batch_name, device_id, sample_seq)
        signature = (utc_ts, metric_value)
        if key in seen:
            if seen[key] == signature:
                duplicates_by_batch[batch_name] = (
                    duplicates_by_batch.get(batch_name, 0) + 1
                )
                continue
            raise ConflictError(
                f"冲突: 批次 {batch_name} 设备 {device_id} 样本序号 {sample_seq} "
                f"存在不同测量值（第 {lineno} 行），整份文件已拒绝"
            )
        seen[key] = signature
        rows.append(
            {
                "batch_name": batch_name,
                "device_id": device_id,
                "sample_seq": sample_seq,
                "utc_ts": utc_ts,
                "metric_value": metric_value,
            }
        )

    if not rows:
        raise ServiceError("CSV 没有有效数据行")
    return {"rows": rows, "duplicates_by_batch": duplicates_by_batch}


# --------------------------------------------------------------------------- #
# 哈希与统计
# --------------------------------------------------------------------------- #


def _hash_rows(rows: list[tuple[Any, ...]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_hash(measurements: list[dict[str, Any]]) -> str:
    rows = sorted(
        (m["device_id"], m["sample_seq"], m["utc_ts"], m["metric_value"])
        for m in measurements
    )
    return _hash_rows(rows)


def device_hash(measurements: list[sqlite3.Row] | list[dict[str, Any]]) -> str:
    rows = sorted(
        (m["sample_seq"], m["utc_ts"], m["metric_value"]) for m in measurements
    )
    return _hash_rows(rows)


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def mad(values: list[float], med: float) -> float:
    return median([abs(v - med) for v in values])


# --------------------------------------------------------------------------- #
# 上传与发布
# --------------------------------------------------------------------------- #


def _next_version(conn: sqlite3.Connection, batch_name: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM batch_version WHERE batch_name=?",
        (batch_name,),
    ).fetchone()
    return int(row["v"])


def upload_csv(text: str, note: str | None = None,
               fail_before_commit: bool = False) -> list[dict[str, Any]]:
    """上传测量 CSV：可包含多个批次，每个批次生成一个新版本（draft）。

    全量解析通过后才开启写事务；任何写入失败整体回滚。
    fail_before_commit=True 用于自测“失败提交不改变当前结果”。
    """
    parsed = parse_measurement_csv(text)
    measurements_all = parsed["rows"]
    dup_by_batch = parsed["duplicates_by_batch"]

    by_batch: dict[str, list[dict[str, Any]]] = {}
    for row in measurements_all:
        by_batch.setdefault(row["batch_name"], []).append(row)

    conn = get_db()
    created: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for batch_name, measurements in by_batch.items():
            version = _next_version(conn, batch_name)
            chash = content_hash(measurements)
            if fail_before_commit:
                raise CommitFailure("注入的写入失败：事务将回滚")
            conn.execute(
                "INSERT INTO batch_version (batch_name, version, status, "
                "content_hash, row_count, note) VALUES (?,?, 'draft', ?, ?, ?)",
                (batch_name, version, chash, len(measurements), note),
            )
            conn.executemany(
                "INSERT INTO measurement (batch_name, version, device_id, "
                "sample_seq, utc_ts, metric_value) VALUES (?,?,?,?,?,?)",
                [
                    (
                        batch_name,
                        version,
                        m["device_id"],
                        m["sample_seq"],
                        m["utc_ts"],
                        m["metric_value"],
                    )
                    for m in measurements
                ],
            )
            created.append(
                {
                    "batch_name": batch_name,
                    "version": version,
                    "row_count": len(measurements),
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


def publish_batch(batch_name: str, version: int | None = None) -> dict[str, Any]:
    """发布指定版本；省略 version 时发布该批次最新版本。"""
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
            version = row["version"]
        bv = conn.execute(
            "SELECT * FROM batch_version WHERE batch_name=? AND version=?",
            (batch_name, version),
        ).fetchone()
        if bv is None:
            raise NotFoundError(f"批次版本不存在: {batch_name} v{version}")
        if bv["status"] == "published":
            raise ConflictError(f"{batch_name} v{version} 已发布，不可重复发布或修改")
        conn.execute(
            "UPDATE batch_version SET status='published', "
            "published_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE batch_name=? AND version=?",
            (batch_name, version),
        )
        conn.commit()
    except (NotFoundError, ConflictError):
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {"batch_name": batch_name, "version": version, "status": "published"}


def list_batches() -> list[sqlite3.Row]:
    conn = get_db()
    return conn.execute(
        "SELECT * FROM batch_version ORDER BY batch_name, version"
    ).fetchall()


def list_published() -> list[sqlite3.Row]:
    conn = get_db()
    return conn.execute(
        "SELECT * FROM batch_version WHERE status='published' "
        "ORDER BY batch_name, version"
    ).fetchall()


def get_published_version(batch_name: str) -> sqlite3.Row:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM batch_version WHERE batch_name=? AND status='published' "
        "ORDER BY version DESC LIMIT 1",
        (batch_name,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"批次 {batch_name} 没有已发布版本")
    return row


def get_measurements(batch_name: str, version: int,
                     device_id: str | None = None) -> list[sqlite3.Row]:
    conn = get_db()
    sql = (
        "SELECT * FROM measurement WHERE batch_name=? AND version=? "
        + ("AND device_id=? " if device_id else "")
        + "ORDER BY device_id, utc_ts, sample_seq"
    )
    params: tuple[Any, ...] = (
        (batch_name, version, device_id) if device_id else (batch_name, version)
    )
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- #
# 漂移分析
# --------------------------------------------------------------------------- #


@dataclass
class Segment:
    device_id: str
    seg_index: int
    start_seq: int
    end_seq: int
    start_utc: str
    end_utc: str
    sample_count: int
    max_deviation: float
    direction: str


def build_segments(rows: list[sqlite3.Row], median_value: float,
                   mad_value: float, multiplier: float,
                   threshold_low: float, threshold_high: float) -> list[Segment]:
    """按 UTC 排序扫描待检样本，找出持续 >=3 个点的偏离段。

    断开条件：
    1. 样本回到阈值带内；
    2. 缺测 —— 相邻样本序号不连续（缺号即视为缺测）；
    3. UTC 时刻不严格递增（时刻相同/倒退视为断链）；
    4. 设备切换 —— 本函数按单设备调用，设备间天然分段。
    """
    ordered = sorted(rows, key=lambda r: (r["utc_ts"], r["sample_seq"]))
    segments: list[Segment] = []
    run: list[sqlite3.Row] = []
    prev: sqlite3.Row | None = None

    def flush() -> None:
        if len(run) >= 3:
            deviations = [r["metric_value"] - median_value for r in run]
            max_dev = max(deviations, key=abs)
            direction = "high" if max_dev > 0 else "low"
            segments.append(
                Segment(
                    device_id=run[0]["device_id"],
                    seg_index=len(segments) + 1,
                    start_seq=run[0]["sample_seq"],
                    end_seq=run[-1]["sample_seq"],
                    start_utc=run[0]["utc_ts"],
                    end_utc=run[-1]["utc_ts"],
                    sample_count=len(run),
                    max_deviation=max_dev,
                    direction=direction,
                )
            )

    for row in ordered:
        is_out = (
            row["metric_value"] < threshold_low
            or row["metric_value"] > threshold_high
        )
        broken = False
        if prev is not None:
            seq_gap = row["sample_seq"] != prev["sample_seq"] + 1
            time_gap = row["utc_ts"] <= prev["utc_ts"]
            broken = seq_gap or time_gap
        if not is_out or broken:
            flush()
            run = [row] if is_out else []
        else:
            run.append(row)
        prev = row
    flush()
    return segments


def analyze_device(baseline_rows: list[sqlite3.Row],
                   target_rows: list[sqlite3.Row],
                   multiplier: float) -> dict[str, Any]:
    """单设备分析，返回统计结果与异常段（无基线数据则跳过该设备）。"""
    if not baseline_rows:
        return {
            "skipped": True,
            "baseline_count": 0,
            "target_count": len(target_rows),
            "segments": [],
            "baseline_hash": None,
            "target_hash": device_hash(target_rows) if target_rows else None,
        }
    base_values = [r["metric_value"] for r in baseline_rows]
    med = median(base_values)
    mad_value = mad(base_values, med)
    # MAD=0（基线常量/高度集中）时阈值带退化为中位数点，
    # 任何不等于中位数的测量都判为偏离，避免除零与漏报。
    if mad_value == 0.0:
        low = high = med
    else:
        delta = multiplier * mad_value
        low, high = med - delta, med + delta
    segments = build_segments(target_rows, med, mad_value, multiplier, low, high)
    return {
        "skipped": False,
        "baseline_count": len(baseline_rows),
        "baseline_median": med,
        "baseline_mad": mad_value,
        "threshold_low": low,
        "threshold_high": high,
        "target_count": len(target_rows),
        "segments": segments,
        "baseline_hash": device_hash(baseline_rows),
        "target_hash": device_hash(target_rows),
    }


def _group_by_device(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["device_id"], []).append(row)
    return grouped


def _persist_check(conn: sqlite3.Connection, check_id: int,
                   device_results: dict[str, dict[str, Any]]) -> None:
    for device_id, result in sorted(device_results.items()):
        note = "基线无该设备数据，未参与判定" if result["skipped"] else None
        conn.execute(
            "INSERT INTO check_device_result (check_id, device_id, baseline_count, "
            "baseline_median, baseline_mad, threshold_low, threshold_high, "
            "target_count, segment_count, baseline_hash, target_hash, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                check_id,
                device_id,
                result["baseline_count"],
                result.get("baseline_median"),
                result.get("baseline_mad"),
                result.get("threshold_low"),
                result.get("threshold_high"),
                result["target_count"],
                len(result["segments"]),
                result["baseline_hash"],
                result["target_hash"],
                note,
            ),
        )
        for seg in result["segments"]:
            conn.execute(
                "INSERT INTO anomaly_segment (check_id, device_id, seg_index, "
                "start_seq, end_seq, start_utc, end_utc, sample_count, "
                "max_deviation, direction) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    check_id,
                    device_id,
                    seg.seg_index,
                    seg.start_seq,
                    seg.end_seq,
                    seg.start_utc,
                    seg.end_utc,
                    seg.sample_count,
                    seg.max_deviation,
                    seg.direction,
                ),
            )


def create_check(baseline_batch: str, target_batch: str,
                 mad_multiplier: float) -> dict[str, Any]:
    """选择两个已发布批次创建核查，固化当时版号。"""
    if not isinstance(mad_multiplier, (int, float)) or mad_multiplier <= 0:
        raise ServiceError("MAD 倍数阈值必须是正数")
    baseline = get_published_version(baseline_batch)
    target = get_published_version(target_batch)

    base_rows = get_measurements(baseline_batch, baseline["version"])
    target_rows = get_measurements(target_batch, target["version"])
    base_by_dev = _group_by_device(base_rows)
    target_by_dev = _group_by_device(target_rows)
    all_devices = sorted(set(base_by_dev) | set(target_by_dev))

    device_results: dict[str, dict[str, Any]] = {}
    for device_id in all_devices:
        device_results[device_id] = analyze_device(
            base_by_dev.get(device_id, []),
            target_by_dev.get(device_id, []),
            float(mad_multiplier),
        )

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO drift_check (baseline_batch, baseline_version, "
            "target_batch, target_version, mad_multiplier) VALUES (?,?,?,?,?)",
            (
                baseline_batch,
                baseline["version"],
                target_batch,
                target["version"],
                float(mad_multiplier),
            ),
        )
        check_id = int(cur.lastrowid)
        _persist_check(conn, check_id, device_results)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"check_id": check_id, "device_results": _serializable(device_results)}


def rerun_check(check_id: int) -> dict[str, Any]:
    """对历史核查做重算：只重新分析基线/待检数据哈希发生变化的设备。

    旧核查记录原样保留（结论仍可查），新核查记录通过 supersedes 关联。
    若两个批次当前都没有已发布版本则拒绝重算。
    """
    conn = get_db()
    old = conn.execute(
        "SELECT * FROM drift_check WHERE id=?", (check_id,)
    ).fetchone()
    if old is None:
        raise NotFoundError(f"核查记录不存在: #{check_id}")
    old_device_rows = conn.execute(
        "SELECT * FROM check_device_result WHERE check_id=?", (check_id,)
    ).fetchall()
    old_hashes = {
        r["device_id"]: (r["baseline_hash"], r["target_hash"])
        for r in old_device_rows
    }

    baseline = get_published_version(old["baseline_batch"])
    target = get_published_version(old["target_batch"])
    multiplier = float(old["mad_multiplier"])

    base_by_dev = _group_by_device(
        get_measurements(old["baseline_batch"], baseline["version"])
    )
    target_by_dev = _group_by_device(
        get_measurements(old["target_batch"], target["version"])
    )
    all_devices = sorted(set(base_by_dev) | set(target_by_dev) | set(old_hashes))

    device_results: dict[str, dict[str, Any]] = {}
    affected: list[str] = []
    unchanged: list[str] = []
    for device_id in all_devices:
        result = analyze_device(
            base_by_dev.get(device_id, []),
            target_by_dev.get(device_id, []),
            multiplier,
        )
        device_results[device_id] = result
        sig = (result["baseline_hash"], result["target_hash"])
        if old_hashes.get(device_id) != sig:
            affected.append(device_id)
        else:
            unchanged.append(device_id)

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO drift_check (baseline_batch, baseline_version, "
            "target_batch, target_version, mad_multiplier, supersedes) "
            "VALUES (?,?,?,?,?,?)",
            (
                old["baseline_batch"],
                baseline["version"],
                old["target_batch"],
                target["version"],
                multiplier,
                check_id,
            ),
        )
        new_check_id = int(cur.lastrowid)
        _persist_check(conn, new_check_id, device_results)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "check_id": new_check_id,
        "supersedes": check_id,
        "affected_devices": affected,
        "unchanged_devices": unchanged,
        "device_results": _serializable(device_results),
    }


def _serializable(device_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for device_id, result in device_results.items():
        item = {k: v for k, v in result.items() if k != "segments"}
        item["segments"] = [vars(seg) for seg in result["segments"]]
        out[device_id] = item
    return out


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def list_checks() -> list[sqlite3.Row]:
    conn = get_db()
    return conn.execute(
        "SELECT c.*, "
        "(SELECT COUNT(*) FROM anomaly_segment s WHERE s.check_id=c.id) "
        "AS total_segments FROM drift_check c ORDER BY c.id"
    ).fetchall()


def get_check(check_id: int) -> dict[str, Any]:
    conn = get_db()
    check = conn.execute(
        "SELECT * FROM drift_check WHERE id=?", (check_id,)
    ).fetchone()
    if check is None:
        raise NotFoundError(f"核查记录不存在: #{check_id}")
    devices = conn.execute(
        "SELECT * FROM check_device_result WHERE check_id=? ORDER BY device_id",
        (check_id,),
    ).fetchall()
    segments = conn.execute(
        "SELECT * FROM anomaly_segment WHERE check_id=? "
        "ORDER BY device_id, seg_index",
        (check_id,),
    ).fetchall()
    return {"check": check, "devices": devices, "segments": segments}


def query_segments(device_id: str | None = None,
                   batch_name: str | None = None,
                   direction: str | None = None,
                   min_samples: int | None = None) -> list[sqlite3.Row]:
    """异常段查询，支持设备/批次/方向/最小持续样本数过滤。"""
    sql = (
        "SELECT s.*, c.baseline_batch, c.baseline_version, "
        "c.target_batch, c.target_version, c.mad_multiplier "
        "FROM anomaly_segment s JOIN drift_check c ON s.check_id=c.id WHERE 1=1"
    )
    params: list[Any] = []
    if device_id:
        sql += " AND s.device_id=?"
        params.append(device_id)
    if batch_name:
        sql += " AND (c.baseline_batch=? OR c.target_batch=?)"
        params.extend([batch_name, batch_name])
    if direction:
        if direction not in ("high", "low"):
            raise ServiceError("direction 只能是 high 或 low")
        sql += " AND s.direction=?"
        params.append(direction)
    if min_samples is not None:
        if min_samples < 1:
            raise ServiceError("min_samples 必须 >=1")
        sql += " AND s.sample_count>=?"
        params.append(int(min_samples))
    sql += " ORDER BY s.device_id, s.start_utc, s.check_id"
    return get_db().execute(sql, params).fetchall()


def get_measurement_count() -> int:
    return int(
        get_db().execute("SELECT COUNT(*) AS n FROM measurement").fetchone()["n"]
    )
