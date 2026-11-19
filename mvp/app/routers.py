# -*- coding: utf-8 -*-
"""HTTP 路由：鉴权 → 参数校验 → 业务 → 审计 → 统一信封。"""
from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from .services import rag, wiki
from .config import Settings
from .db import audit, get_conn, new_id, now_iso
from .errors import AppError, ErrorCode
from .response import ok
from .schemas import (
    AskIn,
    DocIn,
    TenantIn,
    TriageIn,
    UserIn,
    UserPatchIn,
    WikiEdgeIn,
    WikiNodeIn,
)
from .security import (
    CAP_ADMIN_TENANTS,
    CAP_ADMIN_USERS,
    CAP_AGENT_ASK,
    CAP_AUDIT_READ,
    CAP_KNOWLEDGE_READ,
    CAP_KNOWLEDGE_WRITE,
    CAP_TRIAGE_ADVISE,
    CAP_WIKI_READ,
    CAP_WIKI_WRITE,
    ROLE_POLICY,
    Principal,
    get_principal,
    hash_key,
    require,
    resolve_tenant,
)
from .services.harness import Harness
from .services.triage import advise

router = APIRouter()


def _settings(request: Request) -> Settings:
    """配置在 app.state 上，路由通过依赖注入式函数取，避免全局单例。"""
    return request.app.state.settings


def _rid(request: Request) -> str:
    """请求 ID 由中间件塞入；直接调用（测试）时兜底为 unknown。"""
    return getattr(request.state, "request_id", "unknown")


def _audit(
    conn: sqlite3.Connection,
    request: Request,
    principal: Principal,
    action: str,
    object_type: str,
    object_id: str | None,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """路由内审计的薄封装，保证 request_id 与 actor 不遗漏。"""
    audit(
        conn,
        tenant_id=principal.tenant_id,
        actor_id=principal.user_id,
        actor_role=principal.role,
        action=action,
        object_type=object_type,
        object_id=object_id,
        before=before,
        after=after,
        request_id=_rid(request),
    )


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    """存活与能力探针；不返回任何密钥或库路径。"""
    s = _settings(request)
    return ok(
        {
            "status": "ok",
            "env": s.app.env,
            "synthetic_only": s.app.synthetic_only,
            "model_enabled": s.model.chat_enabled,
            "embedding_enabled": s.model.embedding_enabled,
            "rag_mode": s.rag.mode,
            "threshold_version": s.threshold_version,
            "timezone": s.app.timezone,
        }
    )


@router.get("/auth/me")
def me(principal: Principal = Depends(get_principal)) -> dict[str, Any]:
    """返回当前身份与能力，便于前端按能力渲染而不是按角色硬编码。"""
    return ok(
        {
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "name": principal.name,
            "role": principal.role,
            "tier": principal.tier,
            "caps": sorted(principal.caps),
        }
    )


@router.post("/knowledge/docs")
async def create_doc(
    payload: DocIn,
    request: Request,
    principal: Principal = Depends(require(CAP_KNOWLEDGE_WRITE)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """文档入库并建索引；仅具备 knowledge:write 的角色可调用。"""
    result = await rag.ingest(
        conn,
        _settings(request),
        request.app.state.gateway,
        principal,
        title=payload.title,
        content=payload.content,
        doc_type=payload.doc_type,
        source=payload.source,
        version=payload.version,
        synthetic=payload.synthetic,
    )
    _audit(conn, request, principal, "KNOWLEDGE_INGEST", "doc", result["doc_id"],
           after={"title": payload.title, "chunks": result["chunks"]})
    return ok(result)


@router.get("/knowledge/docs")
def list_docs(
    request: Request,
    tenant: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require(CAP_KNOWLEDGE_READ)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """按租户列文档；tenant 参数只对平台管理员生效。"""
    tenant_id = resolve_tenant(principal, tenant)
    rows = conn.execute(
        """SELECT doc_id, title, doc_type, source, version, synthetic, created_at
           FROM knowledge_docs WHERE tenant_id=? AND status='active'
           ORDER BY created_at DESC LIMIT ?""",
        (tenant_id, limit),
    ).fetchall()
    return ok({"items": [dict(r) for r in rows], "count": len(rows)})


@router.get("/knowledge/search")
async def search_knowledge(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    top_k: int = Query(default=5, ge=1, le=10),
    mode: str | None = Query(default=None, pattern="^(fts|vector|hybrid)$"),
    tenant: str | None = Query(default=None),
    principal: Principal = Depends(require(CAP_KNOWLEDGE_READ)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """检索接口；返回命中片段与检索元信息（含是否降级）。"""
    tenant_id = resolve_tenant(principal, tenant)
    hits, meta = await rag.search(
        conn, _settings(request), request.app.state.gateway, tenant_id, q, top_k, mode
    )
    return ok(
        {
            "query": q,
            "meta": meta,
            "items": [
                {
                    "chunk_id": h.chunk_id,
                    "doc_id": h.doc_id,
                    "title": h.title,
                    "snippet": h.content[:220],
                    "score": round(h.score, 6),
                    "source": h.source,
                }
                for h in hits
            ],
        }
    )


@router.post("/wiki/nodes")
def add_node(
    payload: WikiNodeIn,
    request: Request,
    principal: Principal = Depends(require(CAP_WIKI_WRITE)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """新增图谱节点；重复标签返回 409。"""
    node = wiki.create_node(
        conn, principal.tenant_id, kind=payload.kind, label=payload.label,
        summary=payload.summary, props=payload.props,
    )
    _audit(conn, request, principal, "WIKI_NODE_CREATE", "wiki_node", node["node_id"],
           after={"label": payload.label})
    return ok(node)


@router.post("/wiki/edges")
def add_edge(
    payload: WikiEdgeIn,
    request: Request,
    principal: Principal = Depends(require(CAP_WIKI_WRITE)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """新增有向边；跨租户连边会被拒绝。"""
    edge = wiki.create_edge(
        conn, principal.tenant_id, src_id=payload.src_id, dst_id=payload.dst_id,
        relation=payload.relation, weight=payload.weight, evidence_id=payload.evidence_id,
    )
    _audit(conn, request, principal, "WIKI_EDGE_CREATE", "wiki_edge", edge["edge_id"],
           after={"relation": payload.relation})
    return ok(edge)


@router.get("/wiki/nodes")
def get_nodes(
    request: Request,
    kind: str | None = Query(default=None),
    tenant: str | None = Query(default=None),
    principal: Principal = Depends(require(CAP_WIKI_READ)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """列出节点，可按类型过滤。"""
    tenant_id = resolve_tenant(principal, tenant)
    nodes = wiki.list_nodes(conn, tenant_id, kind)
    return ok({"items": nodes, "count": len(nodes)})


@router.get("/wiki/nodes/{node_id}/neighbors")
def get_neighbors(
    node_id: str,
    request: Request,
    principal: Principal = Depends(require(CAP_WIKI_READ)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """取一跳邻居：回答"这个原因需要哪些证据"。"""
    data = wiki.neighbors(conn, principal.tenant_id, node_id)
    if not data["nodes"]:
        raise AppError(ErrorCode.NOT_FOUND, 404, "节点不存在")
    return ok(data)


@router.get("/wiki/graph")
def get_graph(
    request: Request,
    tenant: str | None = Query(default=None),
    principal: Principal = Depends(require(CAP_WIKI_READ)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """导出整张图，供前端渲染与调试。"""
    tenant_id = resolve_tenant(principal, tenant)
    return ok(wiki.graph(conn, tenant_id))


@router.get("/wiki/view", response_class=HTMLResponse)
def wiki_view(
    request: Request,
    principal: Principal = Depends(require(CAP_WIKI_READ)),
) -> HTMLResponse:
    """单文件 HTML 图视图；浏览器无法带自定义头，故允许 api_key 查询参数。"""
    # routers.py 位于 app/ 下，static 与之同级，取 parents[0]
    path = Path(__file__).resolve().parents[0] / "static" / "wiki.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@router.post("/agent/ask")
async def agent_ask(
    payload: AskIn,
    request: Request,
    principal: Principal = Depends(require(CAP_AGENT_ASK)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Agent 问答：工具检索 + 引用 + 轨迹；无模型时退化为检索摘录。"""
    harness = Harness(_settings(request), request.app.state.gateway)
    result = await harness.run(
        conn, principal, payload.question, top_k=payload.top_k, use_wiki=payload.use_wiki
    )
    _audit(conn, request, principal, "AGENT_ASK", "agent_run", result["run_id"],
           after={"degraded": result["degraded"], "citations": len(result["citations"])})
    return ok(result)


@router.post("/triage/advise")
def triage_advise(
    payload: TriageIn,
    request: Request,
    principal: Principal = Depends(require(CAP_TRIAGE_ADVISE)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """分流建议（不落库、不出结论），证据来自知识图谱。"""
    result = advise(
        conn,
        _settings(request),
        principal.tenant_id,
        unit_code=payload.unit_code,
        actual_kwh=payload.actual_kwh,
        theoretical_kwh=payload.theoretical_kwh,
        irradiance=payload.irradiance,
        alarms=payload.alarms,
        days_since_cleaning=payload.days_since_cleaning,
        shading_logged=payload.shading_logged,
        data_quality_ok=payload.data_quality_ok,
    )
    _audit(conn, request, principal, "TRIAGE_ADVISE", "unit", payload.unit_code,
           after={"candidates": [c["type"] for c in result["candidates"]]})
    return ok(result)


@router.get("/admin/users")
def list_users(
    request: Request,
    tenant: str | None = Query(default=None),
    principal: Principal = Depends(require(CAP_ADMIN_USERS)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """列出租户内用户；平台管理员可指定租户。"""
    tenant_id = resolve_tenant(principal, tenant)
    rows = conn.execute(
        "SELECT user_id, name, role, tier, status, created_at FROM users WHERE tenant_id=? ORDER BY created_at",
        (tenant_id,),
    ).fetchall()
    return ok({"items": [dict(r) for r in rows], "count": len(rows)})


@router.post("/admin/users")
def create_user(
    payload: UserIn,
    request: Request,
    tenant: str | None = Query(default=None),
    principal: Principal = Depends(require(CAP_ADMIN_USERS)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """创建用户并一次性返回 Key；提权路径在这里被掐断。"""
    if payload.role not in ROLE_POLICY:
        raise AppError(ErrorCode.VALIDATION_FAILED, 422, f"未登记角色: {payload.role}")
    # 租户管理员不得创建平台/租户管理员，否则一次误操作就能自建同级账号
    if payload.role in ("platform_admin", "tenant_admin") and not principal.is_platform_admin:
        raise AppError(ErrorCode.ROLE_FORBIDDEN, 403, "只有平台管理员可创建管理员角色")

    tenant_id = resolve_tenant(principal, tenant)
    raw_key = "sk_" + secrets.token_urlsafe(18)
    user_id = new_id("u")
    conn.execute(
        """INSERT INTO users(user_id, tenant_id, name, role, tier, api_key_hash, status, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (user_id, tenant_id, payload.name, payload.role, ROLE_POLICY[payload.role]["tier"],
         # 只存哈希；明文 Key 只在此响应出现一次
         hash_key(raw_key), "active", now_iso()),
    )
    conn.commit()
    _audit(conn, request, principal, "USER_CREATE", "user", user_id,
           after={"role": payload.role, "tenant": tenant_id})
    return ok({"user_id": user_id, "tenant_id": tenant_id, "role": payload.role,
               "api_key": raw_key, "notice": "Key 仅显示一次，请立即保存"})


@router.patch("/admin/users/{user_id}")
def patch_user(
    user_id: str,
    payload: UserPatchIn,
    request: Request,
    principal: Principal = Depends(require(CAP_ADMIN_USERS)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """改角色/状态；before 与 after 都写审计，便于回溯"谁把权限放开的"。"""
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, 404, "用户不存在")
    if row["tenant_id"] != principal.tenant_id and not principal.is_platform_admin:
        raise AppError(ErrorCode.TENANT_FORBIDDEN, 403, "不得修改其他租户用户")
    if row["role"] == "platform_admin" and not principal.is_platform_admin:
        raise AppError(ErrorCode.ROLE_FORBIDDEN, 403, "不得修改平台管理员")

    new_role = payload.role or row["role"]
    if new_role not in ROLE_POLICY:
        raise AppError(ErrorCode.VALIDATION_FAILED, 422, f"未登记角色: {new_role}")
    if new_role in ("platform_admin", "tenant_admin") and not principal.is_platform_admin:
        raise AppError(ErrorCode.ROLE_FORBIDDEN, 403, "只有平台管理员可授予管理员角色")

    new_status = payload.status or row["status"]
    conn.execute(
        "UPDATE users SET role=?, tier=?, status=? WHERE user_id=?",
        (new_role, ROLE_POLICY[new_role]["tier"], new_status, user_id),
    )
    conn.commit()
    _audit(conn, request, principal, "USER_UPDATE", "user", user_id,
           before={"role": row["role"], "status": row["status"]},
           after={"role": new_role, "status": new_status})
    return ok({"user_id": user_id, "role": new_role, "status": new_status})


@router.post("/admin/tenants")
def create_tenant(
    payload: TenantIn,
    request: Request,
    principal: Principal = Depends(require(CAP_ADMIN_TENANTS)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """创建租户；仅平台管理员。"""
    tenant_id = new_id("t")
    conn.execute(
        "INSERT INTO tenants(tenant_id, name, plan, status, created_at) VALUES(?,?,?,?,?)",
        (tenant_id, payload.name, payload.plan, "active", now_iso()),
    )
    conn.commit()
    _audit(conn, request, principal, "TENANT_CREATE", "tenant", tenant_id,
           after={"name": payload.name, "plan": payload.plan})
    return ok({"tenant_id": tenant_id, "name": payload.name, "plan": payload.plan})


@router.get("/audit")
def list_audit(
    request: Request,
    tenant: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(require(CAP_AUDIT_READ)),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """审计查询：按时间倒序，默认只返回本租户。"""
    tenant_id = resolve_tenant(principal, tenant)
    rows = conn.execute(
        """SELECT audit_id, actor_id, actor_role, action, object_type, object_id,
                  before_json, after_json, request_id, created_at
           FROM audit WHERE tenant_id=? ORDER BY created_at DESC LIMIT ?""",
        (tenant_id, limit),
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        for key in ("before_json", "after_json"):  # 还原成对象，前端不用二次 parse
            try:
                item[key] = json.loads(item[key]) if item[key] else None
            except json.JSONDecodeError:
                pass
        items.append(item)
    return ok({"items": items, "count": len(items)})
