# -*- coding: utf-8 -*-
"""Agent Harness：进程内工具调用循环（预算/超时/降级/轨迹）。

为什么自建而不是上框架：需求只要"检索→图谱→回答"三步，框架带来的
抽象成本远高于收益；这里 200 行就能把预算、降级、审计全部握在手里。
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from typing import Any

from ..config import Settings
from ..db import new_id, now_iso
from ..errors import AppError, ErrorCode
from ..security import Principal
from . import rag, wiki
from .llm import ModelGateway

# 禁止出现的收益承诺；命中即判模型输出不合规，转人工而不是"改写一下继续发"
_FORBIDDEN = re.compile(r"(预计增发|可节省\s*\d|增发\s*\d+\s*%|节省\s*\d+\s*元)")


class Harness:
    """执行一次问答：规划工具调用、收集证据、产出带引用的回答。"""

    SYSTEM = (
        "你是光伏电站运维知识助手，只依据检索结果回答，不得编造。\n"
        "可用工具：\n"
        "1) search_knowledge(query: 检索词) 检索知识库\n"
        "2) wiki_lookup(label: 原因/证据名称) 查询知识图谱\n"
        "每一步只输出一个 JSON，不要输出解释文字：\n"
        '{"action":"tool","tool":"search_knowledge","args":{"query":"积灰"}}\n'
        '或 {"action":"final","answer":"...","used":["chunk_id"]}\n'
        "最终回答必须给出来源 chunk_id 列表；证据不足时直接说明缺什么材料。"
    )

    def __init__(self, settings: Settings, gateway: ModelGateway) -> None:
        self.settings = settings
        self.gateway = gateway

    def _parse(self, raw: str) -> dict[str, Any]:
        """解析模型输出的 JSON；容忍 ```json 包裹，其它情况一律判不合规。"""
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "模型未按 JSON 协议返回") from exc
        if not isinstance(data, dict) or data.get("action") not in ("tool", "final"):
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "模型动作字段不合法")
        if data["action"] == "final" and not str(data.get("answer", "")).strip():
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "模型最终回答为空")
        return data

    async def _run_tool(
        self,
        conn: sqlite3.Connection,
        principal: Principal,
        name: str,
        args: dict[str, Any],
        top_k: int,
        collected: dict[str, rag.Hit],
        wiki_hits: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """执行一个工具；只暴露白名单工具，未知工具直接拒绝。"""
        if name == "search_knowledge":
            query = str(args.get("query", "")).strip()
            if not query:
                return {"error": "query 为空"}
            hits, meta = await rag.search(
                conn, self.settings, self.gateway, principal.tenant_id, query, top_k
            )
            for hit in hits:
                collected.setdefault(hit.chunk_id, hit)
            return {
                "mode": meta["mode"],
                "degraded": meta["degraded"],
                "items": [
                    {"chunk_id": h.chunk_id, "title": h.title, "text": h.content[:300]}
                    for h in hits
                ],
            }
        if name == "wiki_lookup":
            label = str(args.get("label", "")).strip()
            nodes = wiki.lookup(conn, principal.tenant_id, label, limit=5)
            for node in nodes:
                wiki_hits[node["node_id"]] = node
            return {"items": nodes}
        raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, f"未注册的工具: {name}")

    @staticmethod
    def _guard(answer: str) -> None:
        """收益承诺属于红线，命中即失败，不做"自动删句子"这种掩盖式处理。"""
        if _FORBIDDEN.search(answer):
            raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "回答包含收益承诺，已拦截")

    async def _retrieval_only(
        self,
        conn: sqlite3.Connection,
        principal: Principal,
        question: str,
        top_k: int,
        use_wiki: bool,
        collected: dict[str, rag.Hit],
        wiki_hits: dict[str, dict[str, Any]],
    ) -> str:
        """未配置模型时的确定性路径：只做检索与拼接，不生成任何推断。"""
        hits, _ = await rag.search(
            conn, self.settings, self.gateway, principal.tenant_id, question, top_k
        )
        for hit in hits:
            collected[hit.chunk_id] = hit
        if use_wiki:
            for node in wiki.lookup(conn, principal.tenant_id, question, limit=3):
                wiki_hits[node["node_id"]] = node

        if not hits:
            return "知识库中没有检索到相关内容。请补充文档，或联系数据工程师确认口径。"
        lines = ["（未启用大模型，以下为知识库原文摘录，仅供人工核对）", ""]
        for i, hit in enumerate(hits, 1):
            lines.append(f"{i}. 《{hit.title}》[{hit.chunk_id}]")
            lines.append(f"   {hit.content[:180]}")
        return "\n".join(lines)

    async def run(
        self,
        conn: sqlite3.Connection,
        principal: Principal,
        question: str,
        top_k: int = 5,
        use_wiki: bool = True,
    ) -> dict[str, Any]:
        """跑完一次问答并落库。

        出参：{run_id, answer, citations, wiki_links, status, degraded, trace, model, latency_ms}
        失败行为：模型超时/不合规抛对应 AppError（不返回半成品答案）。
        """
        started = time.perf_counter()
        run_id = new_id("run")
        collected: dict[str, rag.Hit] = {}
        wiki_hits: dict[str, dict[str, Any]] = {}
        trace: list[dict[str, Any]] = []
        degraded = not self.settings.model.chat_enabled
        status = "degraded" if degraded else "ok"

        if degraded:
            answer = await self._retrieval_only(
                conn, principal, question, top_k, use_wiki, collected, wiki_hits
            )
        else:
            messages = [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": question},
            ]
            answer = ""
            for step in range(1, self.settings.harness.max_steps + 1):
                raw = await self.gateway.chat(messages)
                action = self._parse(raw)
                if action["action"] == "final":
                    answer = str(action["answer"]).strip()
                    trace.append({"step": step, "action": "final"})
                    break
                tool = str(action.get("tool", ""))
                args = action.get("args") or {}
                try:
                    # 工具超时由 harness 预算控制，避免一次检索把整个请求拖死
                    result = await asyncio.wait_for(
                        self._run_tool(conn, principal, tool, args, top_k, collected, wiki_hits),
                        timeout=self.settings.harness.tool_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    raise AppError(ErrorCode.MODEL_TIMEOUT, 504, f"工具 {tool} 执行超时") from exc
                trace.append(
                    {
                        "step": step,
                        "action": "tool",
                        "tool": tool,
                        "args": args,
                        "items": len(result.get("items", [])),
                    }
                )
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {"role": "user", "content": "工具结果：" + json.dumps(result, ensure_ascii=False)[:1800]}
                )
            if not answer:  # 预算耗尽仍未收敛，按不合规处理，绝不返回半成品
                raise AppError(ErrorCode.MODEL_INVALID_OUTPUT, 502, "模型未在步数预算内给出结论")
            self._guard(answer)

        # 引用只保留真实召回过的 chunk，模型自报的 id 不作数
        citations = [
            {"chunk_id": h.chunk_id, "doc_id": h.doc_id, "title": h.title, "score": round(h.score, 6)}
            for h in sorted(collected.values(), key=lambda x: x.score, reverse=True)[:8]
        ]
        wiki_links = [{"node_id": n["node_id"], "label": n["label"]} for n in wiki_hits.values()][:6]
        latency_ms = int((time.perf_counter() - started) * 1000)

        conn.execute(
            """INSERT INTO agent_runs(run_id, tenant_id, user_id, question, status, answer, citations,
                                      trace, model, degraded, latency_ms, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, principal.tenant_id, principal.user_id, question, status, answer,
                json.dumps(citations, ensure_ascii=False), json.dumps(trace, ensure_ascii=False),
                self.settings.model.model or "none", int(degraded), latency_ms, now_iso(),
            ),
        )
        conn.commit()

        return {
            "run_id": run_id,
            "answer": answer,
            "citations": citations,
            "wiki_links": wiki_links,
            "status": status,
            "degraded": degraded,
            "trace": trace,
            "model": self.settings.model.model or "none",
            "latency_ms": latency_ms,
        }
