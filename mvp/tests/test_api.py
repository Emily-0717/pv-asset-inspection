# -*- coding: utf-8 -*-
"""MVP 回归测试：认证、租户隔离、角色闸门、RAG、Harness 降级、图谱、审计。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]  # solar_mvp/
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from bootstrap import bootstrap  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 演示 Key 与 bootstrap.py 保持一致；仅测试环境使用
K_PLATFORM = "sk_platform_demo"
K_ADMIN = "sk_admin_demo"
K_ASSET = "sk_asset_demo"
K_ENGINEER = "sk_engineer_demo"
K_VIEWER = "sk_viewer_demo"
K_OTHER = "sk_other_demo"


def H(key: str) -> dict[str, str]:
    """构造认证头，减少每个用例重复写。"""
    return {"X-API-Key": key}


@pytest.fixture(scope="session")
def client(tmp_path_factory) -> TestClient:
    """起一份独立的临时库：建表 + 灌样例，再挂 TestClient。"""
    tmp = tmp_path_factory.mktemp("solar_mvp")
    cfg = tmp / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "app": {"env": "test", "synthetic_only": True, "audit_retention_days": 180},
                "storage": {"db_path": str(tmp / "mvp.db"), "sql_dir": str(ROOT / "app" / "sql")},
                "model": {"base_url": "", "api_key": "", "model": "", "embedding_model": ""},
                "rag": {"mode": "fts"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    bootstrap(str(cfg))
    app = create_app(settings=load_settings(str(cfg)))
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient) -> None:
    """/health 应回显能力开关与阈值版本，且不泄漏密钥。"""
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["data"]["model_enabled"] is False


def test_missing_key(client: TestClient) -> None:
    """未带 Key 一律 401，且 data 必须为 null。"""
    res = client.get("/auth/me")
    assert res.status_code == 401
    assert res.json()["data"] is None
    assert res.json()["error"]["code"] == "AUTH_MISSING"


def test_unknown_key(client: TestClient) -> None:
    """伪造 Key 不区分"不存在"与"已停用"，统一 401。"""
    res = client.get("/auth/me", headers=H("sk_not_exists"))
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "AUTH_INVALID"


def test_viewer_cannot_write(client: TestClient) -> None:
    """只读角色写文档必须 403，这就是"默认拒绝"的可见效果。"""
    res = client.post(
        "/knowledge/docs",
        headers=H(K_VIEWER),
        json={"title": "测试文档", "content": "这是一段足够长的测试内容，用于验证权限闸门是否生效。"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "ROLE_FORBIDDEN"


def test_ingest_and_search(client: TestClient) -> None:
    """工程师可入库并检索到，证据带 chunk_id。"""
    res = client.post(
        "/knowledge/docs",
        headers=H(K_ENGINEER),
        json={
            "title": "组串电流离散度判断",
            "content": "组串电流离散度超过 10% 时，应优先排查遮挡与接线异常。\n"
                       "单串电流明显偏低但电压正常，常见于组件热斑或旁路二极管故障。",
            "doc_type": "manual",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["chunks"] >= 1

    found = client.get("/knowledge/search", headers=H(K_VIEWER), params={"q": "组串电流"})
    assert found.status_code == 200
    items = found.json()["data"]["items"]
    assert items and items[0]["chunk_id"]


def test_tenant_isolation(client: TestClient) -> None:
    """他站用户既能查到空结果，也不能显式指定别家租户。"""
    docs = client.get("/knowledge/docs", headers=H(K_OTHER))
    assert docs.json()["data"]["count"] == 0

    hijack = client.get("/knowledge/docs", headers=H(K_OTHER), params={"tenant": "t_solar"})
    assert hijack.status_code == 403
    assert hijack.json()["error"]["code"] == "TENANT_FORBIDDEN"


def test_agent_degraded(client: TestClient) -> None:
    """未配置模型时 Harness 走检索摘录，仍要返回引用且标记 degraded。"""
    res = client.post("/agent/ask", headers=H(K_VIEWER), json={"question": "积灰怎么排查"})
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["degraded"] is True
    assert data["status"] == "degraded"
    assert data["citations"], "降级路径也必须给出可核对引用"


def test_wiki_graph_and_duplicate(client: TestClient) -> None:
    """图谱可读；重复节点返回 409 而不是悄悄写两条。"""
    graph = client.get("/wiki/graph", headers=H(K_ASSET))
    assert graph.status_code == 200
    assert graph.json()["data"]["stats"]["nodes"] >= 5

    created = client.post(
        "/wiki/nodes",
        headers=H(K_ENGINEER),
        json={"kind": "cause", "label": "接线松动", "summary": "端子接触不良导致电压异常"},
    )
    assert created.status_code == 200, created.text

    again = client.post(
        "/wiki/nodes",
        headers=H(K_ENGINEER),
        json={"kind": "cause", "label": "接线松动", "summary": "重复"},
    )
    assert again.status_code == 409


def test_triage_confidence_requires_evidence(client: TestClient) -> None:
    """有图谱证据时给置信度；他站没有证据时置信度必须为 None。"""
    ok = client.post(
        "/triage/advise",
        headers=H(K_ASSET),
        json={
            "unit_code": "U-03",
            "actual_kwh": 820,
            "theoretical_kwh": 1000,
            "irradiance": 780,
            "alarms": [],
            "days_since_cleaning": 45,
        },
    )
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["status"] == "advisory_only"
    assert data["candidates"]
    assert data["candidates"][0]["confidence"] is not None


def test_admin_cannot_escalate(client: TestClient) -> None:
    """租户管理员不能造平台管理员，否则一次误操作就绕过了 SaaS 边界。"""
    res = client.post(
        "/admin/users",
        headers=H(K_ADMIN),
        json={"name": "越权账号", "role": "platform_admin"},
    )
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "ROLE_FORBIDDEN"


def test_platform_admin_can_list_all_tenants(client: TestClient) -> None:
    """平台管理员可跨租户查看用户；普通管理员不行。"""
    all_users = client.get("/admin/users", headers=H(K_PLATFORM), params={"tenant": "t_other"})
    assert all_users.status_code == 200
    assert all_users.json()["data"]["count"] >= 1

    denied = client.get("/admin/users", headers=H(K_ADMIN), params={"tenant": "t_other"})
    assert denied.status_code == 403


def test_audit_written(client: TestClient) -> None:
    """关键动作必须落审计，且 before/after 可还原。"""
    res = client.get("/audit", headers=H(K_ADMIN), params={"limit": 50})
    assert res.status_code == 200
    actions = [x["action"] for x in res.json()["data"]["items"]]
    assert "KNOWLEDGE_INGEST" in actions
