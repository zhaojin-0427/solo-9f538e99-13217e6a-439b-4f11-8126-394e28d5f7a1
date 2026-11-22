"""本地开发入口：python run.py（容器内使用 gunicorn，见 Dockerfile）。"""
from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
