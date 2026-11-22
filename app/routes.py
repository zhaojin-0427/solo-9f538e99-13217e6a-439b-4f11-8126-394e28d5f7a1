"""路由：网页表单与 JSON API 共用同一套服务层。"""
from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import services as svc

bp = Blueprint("main", __name__)


# --------------------------------------------------------------------------- #
# 异常 → HTTP 错误处理
# --------------------------------------------------------------------------- #


@bp.app_errorhandler(svc.ServiceError)
def handle_service_error(err: svc.ServiceError):
    if request.path.startswith("/api/"):
        return jsonify({"error": str(err)}), err.status
    flash(str(err), "error")
    return redirect(url_for("main.index"))


# --------------------------------------------------------------------------- #
# 网页
# --------------------------------------------------------------------------- #


@bp.route("/")
def index():
    return render_template(
        "index.html",
        batches=svc.list_batches(),
        checks=svc.list_checks(),
    )


@bp.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        abort(400, "请选择 CSV 文件")
    # utf-8-sig 兼容带 BOM 的 Excel 导出
    text = file.read().decode("utf-8-sig")
    note = request.form.get("note") or None
    fail = request.form.get("simulate_fail") == "1"
    created = svc.upload_csv(text, note=note, fail_before_commit=fail)
    names = ", ".join(f"{c['batch_name']} v{c['version']}" for c in created)
    flash(f"上传成功（草稿）：{names}，共 {sum(c['row_count'] for c in created)} 行",
          "success")
    return redirect(url_for("main.index"))


@bp.route("/publish", methods=["POST"])
def publish():
    batch_name = request.form["batch_name"].strip()
    version = request.form.get("version")
    version = int(version) if version else None
    result = svc.publish_batch(batch_name, version)
    flash(f"已发布 {result['batch_name']} v{result['version']}", "success")
    return redirect(url_for("main.index"))


@bp.route("/checks/new", methods=["POST"])
def create_check():
    try:
        multiplier = float(request.form["mad_multiplier"])
    except (KeyError, ValueError):
        abort(400, "MAD 倍数阈值必须是正数")
    result = svc.create_check(
        request.form["baseline_batch"].strip(),
        request.form["target_batch"].strip(),
        multiplier,
    )
    flash(f"核查完成，编号 #{result['check_id']}", "success")
    return redirect(url_for("main.check_detail", check_id=result["check_id"]))


@bp.route("/checks/<int:check_id>/rerun", methods=["POST"])
def rerun(check_id: int):
    result = svc.rerun_check(check_id)
    flash(
        f"已重算为新核查 #{result['check_id']}（旧 #{check_id} 保留）；"
        f"受影响设备: {', '.join(result['affected_devices']) or '无'}",
        "success",
    )
    return redirect(url_for("main.check_detail", check_id=result["check_id"]))


@bp.route("/checks/<int:check_id>")
def check_detail(check_id: int):
    data = svc.get_check(check_id)
    return render_template("check.html", **data)


@bp.route("/segments")
def segments_page():
    device_id = request.args.get("device_id") or None
    batch_name = request.args.get("batch_name") or None
    direction = request.args.get("direction") or None
    min_samples = request.args.get("min_samples")
    min_samples = int(min_samples) if min_samples else None
    rows = svc.query_segments(device_id, batch_name, direction, min_samples)
    return render_template(
        "segments.html",
        segments=rows,
        filters={
            "device_id": device_id or "",
            "batch_name": batch_name or "",
            "direction": direction or "",
            "min_samples": min_samples or "",
        },
    )


# --------------------------------------------------------------------------- #
# JSON API（便于自动化测试/集成）
# --------------------------------------------------------------------------- #


@bp.post("/api/uploads")
def api_upload():
    if "file" in request.files:
        text = request.files["file"].read().decode("utf-8-sig")
    else:
        payload = request.get_json(silent=True) or {}
        text = payload.get("csv", "")
    fail = bool(request.args.get("simulate_fail"))
    created = svc.upload_csv(
        text,
        note=(request.form.get("note") if request.form else None),
        fail_before_commit=fail,
    )
    return jsonify({"created": created}), 201


@bp.post("/api/batches/<batch_name>/publish")
def api_publish(batch_name: str):
    version = request.args.get("version", type=int)
    return jsonify(svc.publish_batch(batch_name, version))


@bp.get("/api/batches")
def api_batches():
    return jsonify({"batches": [dict(r) for r in svc.list_batches()]})


@bp.post("/api/checks")
def api_create_check():
    payload = request.get_json(force=True)
    result = svc.create_check(
        payload["baseline_batch"],
        payload["target_batch"],
        float(payload["mad_multiplier"]),
    )
    return jsonify(result), 201


@bp.post("/api/checks/<int:check_id>/rerun")
def api_rerun(check_id: int):
    return jsonify(svc.rerun_check(check_id)), 201


@bp.get("/api/checks/<int:check_id>")
def api_check_detail(check_id: int):
    data = svc.get_check(check_id)
    return jsonify(
        {
            "check": dict(data["check"]),
            "devices": [dict(r) for r in data["devices"]],
            "segments": [dict(r) for r in data["segments"]],
        }
    )


@bp.get("/api/checks")
def api_checks():
    return jsonify({"checks": [dict(r) for r in svc.list_checks()]})


@bp.get("/api/segments")
def api_segments():
    rows = svc.query_segments(
        request.args.get("device_id") or None,
        request.args.get("batch_name") or None,
        request.args.get("direction") or None,
        request.args.get("min_samples", type=int),
    )
    return jsonify({"segments": [dict(r) for r in rows]})
