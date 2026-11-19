# -*- coding: utf-8 -*-
"""RAG：分块 → FTS5 索引 → 可选向量 → 混合排序。

为什么不用向量库：SQLite 自带 FTS5，向量存 BLOB/JSON 用 Python 算余弦，
在单站规模（万级 chunk）足够，且不引入额外中间件。
中文分词：unicode61 会把连续汉字当作一个 token，导致"积灰"搜不到
"组件积灰严重"；这里把汉字逐字加空格后再入索引，检索时同样处理。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..db import new_id, now_iso
from ..errors import AppError, ErrorCode
from ..security import Principal
from .llm import ModelGateway

_CJK = re.compile(r"([\u4e00-\u9fff])")
_WS = re.compile(r"\s+")


def segment(text: str) -> str:
    """把汉字逐字隔开，英文/数字保持原样，供 FTS5 做单字匹配。"""
    spaced = _CJK.sub(r" \1 ", text)
    return _WS.sub(" ", spaced).strip()


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """按段落聚合到目标长度再滑窗切分；重叠保证跨段语义不被切断。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paras:
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}".strip()
            continue
        if buf:
            chunks.append(buf)
        # 单段就超长时按窗口硬切
        start = 0
        while start < len(para):
            chunks.append(para[start : start + size])
            start += max(1, size - overlap)
        buf = ""
    if buf:
        chunks.append(buf)
    if not chunks:
        return []
    # 相邻块补重叠，避免答案刚好落在切口
    if overlap > 0:
        chunks = [chunks[0]] + [
            (chunks[i - 1][-overlap:] + "\n" + chunks[i]) if i else chunks[i]
            for i in range(1, len(chunks))
        ]
    return [c for c in chunks if c.strip()]


def _fts_query(query: str) -> str:
    """把自然语句拆成中文字符二元组 + 英文词，用 OR 交给 bm25 排序。

    为什么不整句当短语：用户问"积灰怎么排查"，库里是"组件积灰的典型特征"，
    整句短语必然零命中；二元组能把两边的"积灰"接上，又比单字精确。
    """
    terms: list[str] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    terms.extend(re.findall(r"[A-Za-z0-9_]+", query))
    # 去重但保持顺序，减少重复打分带来的抖动
    seen: dict[str, None] = {}
    for term in terms:
        seen.setdefault(term, None)
    if not seen:
        return '""'
    return " OR ".join(f'"{segment(t)}"' for t in seen)


def _insert_fts(conn: sqlite3.Connection, chunk_id: str, tenant_id: str, title: str, body: str) -> None:
    """写 FTS 表；body 存分词后的文本，标题同理。"""
    conn.execute(
        "INSERT INTO knowledge_fts(chunk_id, tenant_id, title, body) VALUES(?,?,?,?)",
        (chunk_id, tenant_id, segment(title), segment(body)),
    )


def _delete_fts(conn: sqlite3.Connection, tenant_id: str, title: str) -> None:
    """同租户同标题重入时先清旧索引，避免一份文档两份结果。"""
    conn.execute(
        "DELETE FROM knowledge_fts WHERE tenant_id = ? AND title = ?",
        (tenant_id, segment(title)),
    )


async def ingest(
    conn: sqlite3.Connection,
    settings: Settings,
    gateway: ModelGateway,
    principal: Principal,
    *,
    title: str,
    content: str,
    doc_type: str,
    source: str,
    version: str,
    synthetic: bool,
) -> dict[str, Any]:
    """文档入库：切块、写库、建索引，配置了 embedding 时补向量。

    出参：{"doc_id","chunks","embedded","degraded"}
    失败行为：数据库错误向上抛；向量失败不阻断入库，degraded=true 并在审计里留因。
    """
    # 合成隔离：库里是合成数据时，禁止悄悄混入真实文档
    if settings.app.synthetic_only and not synthetic:
        raise AppError(
            ErrorCode.SYNTHETIC_MIX_FORBIDDEN, 409, "当前为合成隔离模式，禁止写入非 synthetic 文档"
        )

    doc_id = new_id("doc")
    chunks = chunk_text(content, settings.rag.chunk_size, settings.rag.chunk_overlap)
    if not chunks:
        raise AppError(ErrorCode.VALIDATION_FAILED, 422, "内容为空，无法切块")

    _delete_fts(conn, principal.tenant_id, title)
    conn.execute(
        """INSERT INTO knowledge_docs(doc_id, tenant_id, title, doc_type, source, version,
                                      synthetic, status, created_by, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (doc_id, principal.tenant_id, title, doc_type, source, version,
         int(synthetic), "active", principal.user_id, now_iso()),
    )

    vectors: list[list[float]] | None = None
    degraded = False
    if settings.model.embedding_enabled:
        try:
            vectors = await gateway.embed(chunks)
        except AppError:  # 向量失败不阻断主链路，只是退化为词法检索
            degraded = True

    for seq, chunk in enumerate(chunks):
        chunk_id = f"{doc_id}-{seq}"
        emb = json.dumps(vectors[seq]) if vectors else None
        conn.execute(
            """INSERT INTO knowledge_chunks(chunk_id, doc_id, tenant_id, seq, content, embedding, created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (chunk_id, doc_id, principal.tenant_id, seq, chunk, emb, now_iso()),
        )
        _insert_fts(conn, chunk_id, principal.tenant_id, title, chunk)

    conn.commit()
    return {"doc_id": doc_id, "chunks": len(chunks), "embedded": bool(vectors), "degraded": degraded}


@dataclass
class Hit:
    """一条召回结果；score 越大越相关（已统一方向）。"""

    chunk_id: str
    doc_id: str
    title: str
    content: str
    score: float
    source: str


def _fts_search(conn: sqlite3.Connection, tenant_id: str, query: str, limit: int) -> list[Hit]:
    """词法召回：bm25 越小越相关，这里取负号统一成"越大越好"。"""
    rows = conn.execute(
        """SELECT f.chunk_id, c.doc_id, d.title, c.content, bm25(knowledge_fts) AS s
           FROM knowledge_fts f
           JOIN knowledge_chunks c ON c.chunk_id = f.chunk_id
           JOIN knowledge_docs d ON d.doc_id = c.doc_id
           WHERE knowledge_fts MATCH ? AND f.tenant_id = ?
           ORDER BY s LIMIT ?""",
        (_fts_query(query), tenant_id, limit),
    ).fetchall()
    return [
        Hit(r["chunk_id"], r["doc_id"], r["title"], r["content"], -float(r["s"]), "fts")
        for r in rows
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；长度不一致直接 0，避免脏向量把排序带偏。"""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _vector_search(
    conn: sqlite3.Connection,
    settings: Settings,
    gateway: ModelGateway,
    tenant_id: str,
    query: str,
    limit: int,
) -> list[Hit]:
    """向量召回：取出有向量的 chunk 暴力算余弦。

    规模上限由 rag.max_scan 控制；超过就先靠 FTS 收敛候选，避免 O(n) 拖慢请求。
    """
    vector = (await gateway.embed([query]))[0]
    rows = conn.execute(
        """SELECT c.chunk_id, c.doc_id, d.title, c.content, c.embedding
           FROM knowledge_chunks c JOIN knowledge_docs d ON d.doc_id = c.doc_id
           WHERE c.tenant_id = ? AND c.embedding IS NOT NULL LIMIT ?""",
        (tenant_id, settings.rag.max_scan),
    ).fetchall()
    scored: list[Hit] = []
    for r in rows:
        try:
            emb = json.loads(r["embedding"])
        except (TypeError, json.JSONDecodeError):
            continue  # 脏向量跳过，不影响其它结果
        scored.append(
            Hit(r["chunk_id"], r["doc_id"], r["title"], r["content"],
                _cosine(vector, emb), "vector")
        )
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:limit]


async def search(
    conn: sqlite3.Connection,
    settings: Settings,
    gateway: ModelGateway,
    tenant_id: str,
    query: str,
    top_k: int,
    mode: str | None = None,
) -> tuple[list[Hit], dict[str, Any]]:
    """检索入口：fts / vector / hybrid（RRF 融合）。

    出参：(命中列表, 元信息{"mode","degraded","reason"})
    失败行为：词法检索异常向上抛；向量失败自动退化为 fts 并在元信息里说明。
    """
    mode = (mode or settings.rag.mode).lower()
    meta: dict[str, Any] = {"mode": mode, "degraded": False, "reason": None}

    fts_hits: list[Hit] = []
    if mode in ("fts", "hybrid"):
        fts_hits = _fts_search(conn, tenant_id, query, top_k * 3)

    vec_hits: list[Hit] = []
    if mode in ("vector", "hybrid"):
        if not settings.model.embedding_enabled:
            meta.update(degraded=True, reason="embedding_not_configured")
        else:
            try:
                vec_hits = await _vector_search(conn, settings, gateway, tenant_id, query, top_k * 3)
            except AppError as exc:  # 向量挂了不影响出结果，但要如实告知
                meta.update(degraded=True, reason=exc.code)

    if mode == "vector" and not vec_hits and meta["degraded"]:
        fts_hits = _fts_search(conn, tenant_id, query, top_k * 3)
        meta["mode"] = "fts"

    if mode == "hybrid" and fts_hits and vec_hits:
        # RRF：只关心名次，不关心两边分数量纲不同
        scores: dict[str, float] = {}
        pool: dict[str, Hit] = {}
        for hits in (fts_hits, vec_hits):
            for rank, hit in enumerate(hits, start=1):
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (60 + rank)
                pool.setdefault(hit.chunk_id, hit)
        merged = [pool[cid] for cid in sorted(scores, key=scores.get, reverse=True)]
        for h in merged:
            h.score = round(scores[h.chunk_id], 6)
        return merged[:top_k], meta

    hits = fts_hits if mode != "vector" else vec_hits
    return hits[:top_k], meta
