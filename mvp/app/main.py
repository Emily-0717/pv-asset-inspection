# -*- coding: utf-8 -*-
"""FastAPI 应用装配：配置、日志、中间件、异常处理、路由。"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, load_settings
from .db import init_schema, new_id
from .errors import AppError, ErrorCode
from .logging_conf import setup_logging
from .response import fail
from .routers import router
from .services.llm import ModelGateway

logger = logging.getLogger("solar_mvp")


def create_app(settings: Settings | None = None, config_path: str | None = None) -> FastAPI:
    """构建应用；测试可传入现成 Settings，生产走 config.yaml。

    失败行为：配置非法时在启动阶段抛 CONFIG_INVALID，不进入监听。
    """
    setup_logging()
    settings = settings or load_settings(config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """启动时确保表结构存在（脚本可重复执行，不会覆盖数据）。"""
        init_schema(settings)
        logger.info("app started", extra={"action": "startup"})
        yield

    app = FastAPI(
        title="晨曦光伏 · 知识增强分流 MVP",
        version="0.1.0",
        description="只读核对 · 证据可追溯 · 人工交接（合成数据）",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.gateway = ModelGateway(settings)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any):
        """给每个请求打 request_id 并输出一条 JSON 访问日志。"""
        request.state.request_id = new_id("req")
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 异常也记录耗时与路径，否则排障时缺一条最关键的日志
            logger.exception("request failed", extra={"request_id": request.state.request_id})
            raise
        latency = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request",
            extra={
                "request_id": request.state.request_id,
                "action": f"{request.method} {request.url.path}",
                "latency_ms": latency,
                "status": response.status_code,
            },
        )
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        """业务异常 → 统一信封；不暴露堆栈。"""
        logger.warning(
            "app error",
            extra={"request_id": getattr(request.state, "request_id", None), "status": exc.status},
        )
        return fail(exc.code, exc.msg, exc.status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        """把 Pydantic 的首条错误压成一句可读文案。"""
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        msg = f"{loc}: {first.get('msg', '参数不合法')}"
        return fail(ErrorCode.VALIDATION_FAILED, msg, 422)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException):
        """404/405 等框架异常也走同一信封，避免出现两种错误格式。"""
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.VALIDATION_FAILED
        return fail(code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unknown(request: Request, exc: Exception):
        """兜底：记录堆栈，对外只给错误码与 request_id。"""
        rid = getattr(request.state, "request_id", "unknown")
        logger.exception("unhandled error", extra={"request_id": rid})
        _ = exc
        return fail(ErrorCode.INTERNAL_ERROR, f"服务内部错误，request_id={rid}", 500)

    app.include_router(router)
    return app


# 不在此处直接实例化：uvicorn 使用 `--factory app.main:create_app`，
# 避免"一 import 就读配置"，测试与脚本可以自己传 Settings。
