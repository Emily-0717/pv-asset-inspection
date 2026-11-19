# -*- coding: utf-8 -*-
"""身份与权限：API Key 认证 + 角色能力表 + 租户隔离。

设计取舍：
- 不做注册/密码，MVP 用一次性 API Key（管理员创建时返回一次），
  比"用户名密码"少一半攻击面，也符合"默认拒绝"。
- 角色能力表写在代码而不是库表：能力项与路由一一对应，改代码即改权限，
  避免"库里偷偷加了一行就多给了一个权限"。
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from fastapi import Depends, Request

from .db import get_conn
from .errors import AppError, ErrorCode

# 能力项：与路由一一对应，新增路由必须在这里出现
CAP_KNOWLEDGE_READ = "knowledge:read"
CAP_KNOWLEDGE_WRITE = "knowledge:write"
CAP_WIKI_READ = "wiki:read"
CAP_WIKI_WRITE = "wiki:write"
CAP_AGENT_ASK = "agent:ask"
CAP_TRIAGE_ADVISE = "triage:advise"
CAP_TRIAGE_CONFIRM = "triage:confirm"
CAP_ADMIN_USERS = "admin:users"
CAP_ADMIN_TENANTS = "admin:tenants"
CAP_AUDIT_READ = "audit:read"

ALL_CAPS = {
    CAP_KNOWLEDGE_READ, CAP_KNOWLEDGE_WRITE, CAP_WIKI_READ, CAP_WIKI_WRITE,
    CAP_AGENT_ASK, CAP_TRIAGE_ADVISE, CAP_TRIAGE_CONFIRM,
    CAP_ADMIN_USERS, CAP_ADMIN_TENANTS, CAP_AUDIT_READ,
}

# 角色 → 能力集合；tier 是给界面看的层级，cap 才是真正的闸门
ROLE_POLICY: dict[str, dict] = {
    # SaaS 管理员：跨租户，可建租户、建用户，但不碰业务结论
    "platform_admin": {"tier": "SaaS", "caps": ALL_CAPS},
    # 租户管理员：本租户内管人管知识，额度与租户由平台侧控制
    "tenant_admin": {
        "tier": "L3",
        "caps": {
            CAP_KNOWLEDGE_READ, CAP_KNOWLEDGE_WRITE, CAP_WIKI_READ, CAP_WIKI_WRITE,
            CAP_AGENT_ASK, CAP_TRIAGE_ADVISE, CAP_TRIAGE_CONFIRM,
            CAP_ADMIN_USERS, CAP_AUDIT_READ,
        },
    },
    # 资产经理：能确认分流结论，不能改原始数据（由写入能力缺失来体现）
    "asset_manager": {
        "tier": "L3",
        "caps": {CAP_KNOWLEDGE_READ, CAP_WIKI_READ, CAP_AGENT_ASK, CAP_TRIAGE_ADVISE,
                 CAP_TRIAGE_CONFIRM, CAP_AUDIT_READ},
    },
    # 安全专员：独立卡点，可看审计、可驳回
    "safety_officer": {
        "tier": "L3",
        "caps": {CAP_KNOWLEDGE_READ, CAP_WIKI_READ, CAP_AGENT_ASK, CAP_TRIAGE_CONFIRM,
                 CAP_AUDIT_READ},
    },
    # 清洗调度员：能写知识、能确认清洗类结论，不能确认限功率（服务层再判）
    "cleaning_dispatcher": {
        "tier": "L2",
        "caps": {CAP_KNOWLEDGE_READ, CAP_KNOWLEDGE_WRITE, CAP_WIKI_READ, CAP_AGENT_ASK,
                 CAP_TRIAGE_ADVISE, CAP_TRIAGE_CONFIRM},
    },
    # 数据工程师：管数据与知识，不改业务结论
    "data_engineer": {
        "tier": "L2",
        "caps": {CAP_KNOWLEDGE_READ, CAP_KNOWLEDGE_WRITE, CAP_WIKI_READ, CAP_WIKI_WRITE,
                 CAP_AGENT_ASK, CAP_TRIAGE_ADVISE},
    },
    # 巡检员：只能写现场知识，不能导出全站（MVP 未提供导出接口）
    "inspector": {
        "tier": "L2",
        "caps": {CAP_KNOWLEDGE_READ, CAP_KNOWLEDGE_WRITE, CAP_WIKI_READ, CAP_AGENT_ASK,
                 CAP_TRIAGE_ADVISE},
    },
    # 财务：只读汇总 + 图谱只读，不给任何写权限
    "finance_manager": {"tier": "L1", "caps": {CAP_KNOWLEDGE_READ, CAP_WIKI_READ, CAP_AGENT_ASK}},
    # 只读访客：可看知识与图谱（图谱只读不涉及明细）
    "viewer": {"tier": "L1", "caps": {CAP_KNOWLEDGE_READ, CAP_WIKI_READ, CAP_AGENT_ASK}},
}


@dataclass
class Principal:
    """当前请求主体；所有路由只能拿到它，拿不到原始 Key。"""

    user_id: str
    tenant_id: str
    name: str
    role: str
    tier: str
    caps: set[str] = field(default_factory=set)

    def has(self, cap: str) -> bool:
        """能力判断统一入口，避免各处自己拼集合。"""
        return cap in self.caps

    @property
    def is_platform_admin(self) -> bool:
        """平台管理员是唯一可跨租户的角色。"""
        return self.role == "platform_admin"


def hash_key(raw: str) -> str:
    """Key 只存哈希；库里泄露也无法直接拿来调用。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_key(request: Request) -> str | None:
    """优先请求头；HTML 视图无法自定义头，退化为 query 参数。"""
    key = request.headers.get("X-API-Key")
    if key:
        return key.strip()
    return request.query_params.get("api_key")


def get_principal(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Principal:
    """解析并校验调用方身份。

    失败行为：缺 Key 抛 401 AUTH_MISSING；Key 无效/停用/租户停用抛 401 AUTH_INVALID。
    """
    raw = _extract_key(request)
    if not raw:
        raise AppError(ErrorCode.AUTH_MISSING, 401, "缺少 X-API-Key")

    row = conn.execute(
        """SELECT u.user_id, u.tenant_id, u.name, u.role, u.status AS user_status,
                  t.status AS tenant_status
           FROM users u JOIN tenants t ON t.tenant_id = u.tenant_id
           WHERE u.api_key_hash = ?""",
        (hash_key(raw),),
    ).fetchone()
    # 未登记一律拒绝，不做"先放行后补权限"
    if row is None or row["user_status"] != "active" or row["tenant_status"] != "active":
        raise AppError(ErrorCode.AUTH_INVALID, 401, "API Key 无效或已停用")

    policy = ROLE_POLICY.get(row["role"])
    if policy is None:  # 角色不在白名单 = 视同未登记
        raise AppError(ErrorCode.AUTH_INVALID, 401, "角色未登记")

    return Principal(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        role=row["role"],
        tier=policy["tier"],
        caps=set(policy["caps"]),
    )


def require(*caps: str) -> Callable[..., Principal]:
    """生成路由依赖：要求同时具备全部能力。

    为什么用工厂：路由声明式写依赖，权限判断在进入业务函数之前完成，
    业务里不会出现"忘了判断"的分支。
    """

    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        missing = [c for c in caps if c not in principal.caps]
        if missing:  # 缺任意一项即拒绝，并说明缺哪项
            raise AppError(
                ErrorCode.ROLE_FORBIDDEN, 403, f"角色 {principal.role} 缺少权限: {','.join(missing)}"
            )
        return principal

    return _dep


def resolve_tenant(principal: Principal, requested: str | None) -> str:
    """确定本次操作的目标租户；非平台管理员不得跨租户。

    为什么单独抽：跨租户读是最容易被忽略的越权，集中一处才能保证每条 SQL
    都拿到"经校验的 tenant_id"，而不是直接信任 query 参数。
    """
    if not requested or requested == principal.tenant_id:
        return principal.tenant_id
    if principal.is_platform_admin:
        return requested
    raise AppError(ErrorCode.TENANT_FORBIDDEN, 403, "不得访问其他租户数据")


def ensure_caps(principal: Principal, caps: Iterable[str]) -> None:
    """非依赖场景（如按候选类型细分确认权）复用同一套判断。"""
    missing = [c for c in caps if c not in principal.caps]
    if missing:
        raise AppError(ErrorCode.ROLE_FORBIDDEN, 403, f"缺少权限: {','.join(missing)}")
