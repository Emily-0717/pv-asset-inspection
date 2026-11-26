# -*- coding: utf-8 -*-
"""MVP 启动入口：读 config.yaml 后交给 uvicorn 单进程运行。

本地：
    python run.py
    # 或：uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8010

线上（容器 / 云平台）：
    HOST=0.0.0.0 PORT=8080 python run.py
    # app.env=dev 且库为空时自动灌合成演示数据；SOLAR_DEMO_SEED=1 可强制灌
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


def _seed_if_needed(settings) -> dict[str, int] | None:
    """空库自动灌演示数据；已有数据或非 dev 环境一律不碰。

    判定顺序：
        1) SOLAR_DEMO_SEED=1 → 强制灌（脚本幂等，不会覆盖已有数据）
        2) app.env == "dev" 且 users 表为空 → 自动灌（云上临时磁盘每次冷启动都会重建库）
        3) 其余情况（env=production / 已有账号）→ 不介入，避免"部署顺手造一批人"
    """
    force = os.environ.get("SOLAR_DEMO_SEED") == "1"
    if not force:
        if settings.app.env != "dev":
            return None
        try:
            from app.db import connect

            conn = connect(settings.storage.db_path)
            try:
                empty = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            finally:
                conn.close()
        except Exception:
            empty = True  # 表还没建（首次启动），交给 bootstrap 建表并灌
        if not empty:
            return None

    from scripts.bootstrap import bootstrap  # 延迟导入：不灌就不加载

    return bootstrap()


if __name__ == "__main__":
    settings = load_settings()
    counts = _seed_if_needed(settings)
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
