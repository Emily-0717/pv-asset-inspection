# -*- coding: utf-8 -*-
"""SQLite 访问层：只用手写 SQL，不引入 ORM。

连接策略：每个请求一条连接，用完即关。FastAPI 的同步依赖跑在线程池、
异步路由跑在事件循环，连接会跨线程使用，因此必须 check_same_thread=False；
由于一条连接只服务一个请求且顺序使用，不存在并发写同一连接的问题。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import Request

from .config import Settings
from .errors import AppError, ErrorCode


def now_iso() -> str:
    """统一时间格式（UTC、秒级、带 Z），审计与排序都依赖它可比较。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    """生成短 ID；前缀用于人工排查时一眼看出对象类型。"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect(db_path: str) -> sqlite3.Connection:
    """打开连接并设置行工厂与外键；外键必须每个连接单独开。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # 单进程读写并存，WAL 更稳
    return conn


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：产出请求级连接，请求结束自动关闭。"""
    settings: Settings = request.app.state.settings
    conn = connect(settings.storage.db_path)
    try:
        yield conn
    finally:
        conn.close()


def init_schema(settings: Settings) -> None:
    """执行 sql 目录下按文件名排序的脚本；脚本本身必须可重复执行。"""
    sql_dir = Path(settings.storage.sql_dir)
    if not sql_dir.exists():
        raise AppError(ErrorCode.CONFIG_INVALID, 500, f"SQL 目录不存在: {sql_dir}")
    conn = connect(settings.storage.db_path)
    try:
        for script in sorted(sql_dir.glob("*.sql")):
            conn.executescript(script.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def audit(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None,
    actor_id: str,
    actor_role: str,
    action: str,
    object_type: str,
    object_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """写一条审计；append-only，不提供更新/删除接口。

    为什么每条关键动作都要写：权限变更、导出、结论覆盖一旦出事，
    没有"改之前是什么"就无法复核；这里用 before/after 两个 JSON 兜住。
    """
    conn.execute(
        """INSERT INTO audit(audit_id, tenant_id, actor_id, actor_role, action,
                              object_type, object_id, before_json, after_json, request_id, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_id("aud"),
            tenant_id,
            actor_id,
            actor_role,
            action,
            object_type,
            object_id,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            request_id,
            now_iso(),
        ),
    )
    conn.commit()
