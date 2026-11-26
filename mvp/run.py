# -*- coding: utf-8 -*-
"""MVP 启动入口：读 config.yaml 后交给 uvicorn 单进程运行。

本地：
    python run.py
    # 或：uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010

线上（容器 / 云平台）：
    HOST=0.0.0.0 PORT=8080 SOLAR_DEMO_SEED=1 python run.py
    # SOLAR_DEMO_SEED=1 时先执行 bootstrap，把合成演示数据灌进空库（脚本可重复执行）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # 允许 import app / scripts（容器里也可以 python run.py 直接起）

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402


def _seed_if_needed() -> dict[str, int] | None:
    """演示环境要有数据才有页面可看；只在显式开启时灌库，生产不自动造账号。"""
    if os.environ.get("SOLAR_DEMO_SEED") != "1":
        return None
    from scripts.bootstrap import bootstrap  # 延迟导入：不开就不加载

    return bootstrap()


if __name__ == "__main__":
    settings = load_settings()
    counts = _seed_if_needed()
    if counts is not None:
        print(f"[seed] {counts}", flush=True)

    # 云平台用 PORT 注入；本地仍按 config.yaml 的 127.0.0.1:8010
    host = os.environ.get("HOST", settings.server.host)
    port = int(os.environ.get("PORT", str(settings.server.port)))
    uvicorn.run(
        create_app(settings=settings),
        host=host,
        port=port,
        log_config=None,  # 保留我们自己的 JSON 日志，不让 uvicorn 覆盖
    )
