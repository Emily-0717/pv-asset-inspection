# -*- coding: utf-8 -*-
"""请求/响应模型：入参在这里收口，业务层不再做类型判断。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DocIn(BaseModel):
    """知识文档入库参数。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=120)
    content: str = Field(min_length=20, max_length=20000)
    doc_type: Literal["manual", "case", "sop", "faq", "regulation"] = "manual"
    source: str = Field(default="manual", max_length=120)
    version: str = Field(default="v1", max_length=32)
    synthetic: bool = True


class WikiNodeIn(BaseModel):
    """知识图谱节点；kind 限定枚举，避免出现"随手写个类型"。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["cause", "evidence", "action", "metric", "component", "role"] = "cause"
    label: str = Field(min_length=2, max_length=60)
    summary: str = Field(default="", max_length=500)
    props: dict[str, Any] = Field(default_factory=dict)


class WikiEdgeIn(BaseModel):
    """知识图谱边；src/dst 使用 node_id。"""

    model_config = ConfigDict(extra="forbid")

    src_id: str
    dst_id: str
    relation: Literal["suggests", "requires", "mitigates", "caused_by", "measured_by"] = "suggests"
    weight: float = Field(default=1.0, ge=0.0, le=5.0)
    evidence_id: str | None = None


class AskIn(BaseModel):
    """Agent 问答入参。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)
    use_wiki: bool = True


class TriageIn(BaseModel):
    """分流建议入参；只能给建议，不落最终结论。"""

    model_config = ConfigDict(extra="forbid")

    unit_code: str = Field(min_length=1, max_length=40)
    actual_kwh: float = Field(ge=0)
    theoretical_kwh: float = Field(gt=0)
    irradiance: float = Field(ge=0, default=700)
    alarms: list[str] = Field(default_factory=list)
    days_since_cleaning: int | None = Field(default=None, ge=0)
    shading_logged: bool = False
    data_quality_ok: bool = True


class UserIn(BaseModel):
    """创建用户；role 必须是 ROLE_POLICY 中已登记角色。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=40)
    role: str = Field(min_length=3, max_length=40)


class UserPatchIn(BaseModel):
    """调整角色或状态；变更前后都写审计。"""

    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    status: Literal["active", "disabled"] | None = None


class TenantIn(BaseModel):
    """租户创建（仅平台管理员）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=60)
    plan: Literal["trial", "standard", "enterprise"] = "trial"
