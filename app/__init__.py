"""仪器电压读数比对应用包。"""
from __future__ import annotations

import os

from flask import Flask

from .db import close_db, init_db


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "VOLT_DB_PATH", os.path.join(os.getcwd(), "data", "volt.db")
    )
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB 上传上限

    init_db(app.config["DB_PATH"])
    app.teardown_appcontext(close_db)

    from .routes import bp

    app.register_blueprint(bp)
    return app
