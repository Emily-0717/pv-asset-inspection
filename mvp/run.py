# -*- coding: utf-8 -*-
"""MVP 启动入口：读 config.yaml 后交给 uvicorn 单进程运行。

用法：
    python run.py
    # 或：uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010
"""
from __future__ import annotations

import uvicorn

from app.config import load_settings
from app.main import create_app

if __name__ == "__main__":
    settings = load_settings()
    uvicorn.run(
        create_app(settings=settings),
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,  # 保留我们自己的 JSON 日志，不让 uvicorn 覆盖
    )
