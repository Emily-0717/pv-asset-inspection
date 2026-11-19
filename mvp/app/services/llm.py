# -*- coding: utf-8 -*-
"""模型网关：httpx 直连 OpenAI 兼容接口，超时/重试/降级集中在这里。

为什么单独一层：业务代码不应该知道 base_url、重试次数、JSON 形状；
换模型只改配置，不碰业务。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ..config import Settings
from ..errors import AppError, ErrorCode


class ModelGateway:
    """封装 chat 与 embeddings 两个调用；未配置时抛 MODEL_DISABLED 由上层降级。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg = settings.model

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """带重试的 POST；只对超时与 5xx 重试，4xx 立即失败（重试也没用）。"""
        if not self.cfg.chat_enabled:
            raise AppError(ErrorCode.MODEL_DISABLED, 503, "未配置模型，能力已关闭")

        url = self.cfg.base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        last_error: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code >= 500:  # 上游抖动，值得再试
                    last_error = AppError(
                        ErrorCode.MODEL_UPSTREAM, 502, f"模型服务 {resp.status_code}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                if resp.status_code >= 400:  # 鉴权/参数错误，重试无意义
                    raise AppError(
                        ErrorCode.MODEL_UPSTREAM, 502, f"模型服务拒绝请求: {resp.status_code}"
                    )
                return resp.json()
            except httpx.TimeoutException as exc:  # 超时是最常见的失败
                last_error = AppError(ErrorCode.MODEL_TIMEOUT, 504, "模型调用超时")
                await asyncio.sleep(0.2 * (attempt + 1))
                _ = exc
            except httpx.HTTPError as exc:
                last_error = AppError(ErrorCode.MODEL_UPSTREAM, 502, f"模型网络错误: {exc}")
                await asyncio.sleep(0.2 * (attempt + 1))
        raise last_error or AppError(ErrorCode.MODEL_UPSTREAM, 502, "模型调用失败")

    async def chat(self, messages: list[dict[str, str]]) -> str:
        """一次对话补全；返回纯文本，空内容视为不合规。

        失败行为：抛 MODEL_DISABLED / MODEL_TIMEOUT / MODEL_UPSTREAM / MODEL_INVALID_OUTPUT。
        """
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
        }
        data = await self._post("/chat/completions", payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "模型返回缺少 choices") from exc
        if not isinstance(content, str) or not content.strip():
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "模型返回空内容")
        return content.strip()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量取向量；数量对不上直接判不合规，避免错位入库。"""
        payload = {"model": self.cfg.embedding_model, "input": texts}
        data = await self._post("/embeddings", payload)
        try:
            rows = sorted(data["data"], key=lambda x: x["index"])
            vectors = [[float(v) for v in r["embedding"]] for r in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "向量返回结构不合规") from exc
        if len(vectors) != len(texts):
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "向量数量与输入不一致")
        return vectors

    def dump_fields(self, fields: list[str]) -> str:
        """记录"本次送了哪些字段"；即使字段值为空也留字段名，便于审计复现。"""
        return json.dumps({"fields": fields}, ensure_ascii=False)
