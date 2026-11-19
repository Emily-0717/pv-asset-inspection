# -*- coding: utf-8 -*-
"""JSON 行日志：每行一个对象，方便 grep 与后续采集。"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """把 LogRecord 压成单行 JSON；异常堆栈放 exc 字段，不破坏行结构。"""

    def format(self, record: logging.LogRecord) -> str:
        """固定字段优先，extra 里的自定义字段平铺进对象。"""
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "user_id", "tenant_id", "action", "latency_ms", "status"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """配置根 logger；重复调用不叠加 handler（测试会多次建 app）。"""
    root = logging.getLogger()
    root.setLevel(level)
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    logging.getLogger("uvicorn.access").disabled = True  # 访问日志由中间件统一输出
