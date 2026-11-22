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
    flash(
        f"上传成功（草稿）：{names}，共 "
        f"{sum(c['row_count'] for c in created)} 条读数",
        "success",
    )
    return redirect(url_for("main.index"))


@bp.route("/publish", methods=["POST"])
def publish():
    batch_name = request.form["batch_name"].strip()
    version = request.form.get("version")
    version = int(version) if version else None
    fail = request.form.get("simulate_fail") == "1"
    result = svc.publish_batch(batch_name, version, fail_before_commit=fail)
    flash(
        f"已发布 {result['batch_name']} v{result['version']}，"
        f"仪器对统计已随版本固化",
        "success",
    )
    return redirect(url_for("main.index"))


@bp.route("/compare")
def compare_page():
    """比对页：选已发布批次版号 + 两台仪器 + 容差，结果服务端渲染。"""
    devices_map = svc.published_devices_map()
    result = None
    form = {
        "batch_name": "",
        "version": "",
        "device_x": "",
        "device_y": "",
        "tolerance": "",
    }

    batch_name = request.args.get("batch_name")
    if batch_name:
        form["batch_name"] = batch_name
        form["version"] = request.args.get("version", "")
        form["device_x"] = request.args.get("device_x", "")
        form["device_y"] = request.args.get("device_y", "")
        form["tolerance"] = request.args.get("tolerance", "")
        try:
            version = int(form["version"])
            tolerance = int(form["tolerance"])
            result = svc.compare_devices(
                batch_name,
                version,
                form["device_x"].strip(),
                form["device_y"].strip(),
                tolerance,
            )
        except (ValueError, TypeError):
            abort(400, "版号与容差必须是整数，容差非负")

    return render_template(
        "compare.html",
        devices_map=devices_map,
        result=result,
        form=form,
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
    fail = bool(request.args.get("simulate_fail"))
    return jsonify(
        svc.publish_batch(batch_name, version, fail_before_commit=fail)
    )


@bp.get("/api/batches")
def api_batches():
    return jsonify({"batches": [dict(r) for r in svc.list_batches()]})


@bp.get("/api/batches/<batch_name>/versions/<int:version>/devices")
def api_version_devices(batch_name: str, version: int):
    svc.get_published_version(batch_name, version)
    return jsonify(
        {
            "batch_name": batch_name,
            "version": version,
            "devices": svc.list_devices(batch_name, version),
        }
    )


@bp.get("/api/compare")
def api_compare():
    """容差查询：只查已固化的统计索引，不遍历原始读数。"""
    batch_name = request.args.get("batch_name", "").strip()
    version = request.args.get("version", type=int)
    device_x = request.args.get("device_x", "").strip()
    device_y = request.args.get("device_y", "").strip()
    tolerance = request.args.get("tolerance", type=int)
    if not batch_name or version is None or not device_x or not device_y:
        abort(400, "缺少 batch_name/version/device_x/device_y 参数")
    if tolerance is None:
        abort(400, "tolerance 必须是非负整数微伏")
    return jsonify(
        svc.compare_devices(batch_name, version, device_x, device_y, tolerance)
    )


@bp.get("/api/batches/<batch_name>/versions/<int:version>/pair-stats")
def api_pair_stats(batch_name: str, version: int):
    rows = svc.list_pair_stats(batch_name, version)
    return jsonify({"pair_stats": [dict(r) for r in rows]})
