# -*- coding: utf-8 -*-
"""一键自检：用临时库跑一遍关键链路，逐项打印 PASS/FAIL 与预期输出。

用法（在 solar_mvp/ 下）：
    python scripts/verify.py
预期（全部通过时最后一行）：
    ALL CHECKS PASSED (9/9)
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from bootstrap import bootstrap  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

checks: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str) -> None:
    """记录一项检查；detail 用于失败时定位。"""
    checks.append((name, passed, detail))
    print(f"{'PASS' if passed else 'FAIL'}  {name}  {detail}")


def main() -> int:
    """建临时库 → 起 TestClient → 逐项断言 → 汇总退出码。"""
    tmp = Path(tempfile.mkdtemp(prefix="solar_mvp_verify_"))
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
    counts = bootstrap(str(cfg))
    app = create_app(settings=load_settings(str(cfg)))
    # 自检只看 PASS/FAIL，把应用日志压到 ERROR，避免 JSON 行淹没结论
    logging.getLogger().setLevel(logging.ERROR)
    logging.getLogger("solar_mvp").setLevel(logging.ERROR)

    with TestClient(app) as client:
        h_view = {"X-API-Key": "sk_viewer_demo"}
        h_engineer = {"X-API-Key": "sk_engineer_demo"}
        h_asset = {"X-API-Key": "sk_asset_demo"}

        health = client.get("/health").json()
        check("health", health["ok"] and health["data"]["model_enabled"] is False, "model_enabled=False")

        unauth = client.get("/auth/me")
        check("默认拒绝", unauth.status_code == 401 and unauth.json()["data"] is None, "401 AUTH_MISSING")

        forbidden = client.post(
            "/knowledge/docs",
            headers=h_view,
            json={"title": "越权测试", "content": "只读角色不应能写入任何知识文档，这条请求应当被拒绝。"},
        )
        check("角色闸门", forbidden.status_code == 403, "403 ROLE_FORBIDDEN")

        search = client.get(
            "/knowledge/search", headers=h_view, params={"q": "积灰怎么排查", "top_k": 3}
        ).json()
        check("RAG 检索", len(search["data"]["items"]) >= 1, f"hits={len(search['data']['items'])}")

        agent = client.post(
            "/agent/ask", headers=h_view, json={"question": "积灰怎么排查", "top_k": 3}
        ).json()
        check(
            "Harness 降级",
            agent["data"]["degraded"] is True and len(agent["data"]["citations"]) >= 1,
            f"citations={len(agent['data']['citations'])}",
        )

        other = client.get(
            "/knowledge/docs", headers={"X-API-Key": "sk_other_demo"}, params={"tenant": "t_solar"}
        )
        check("租户隔离", other.status_code == 403, "403 TENANT_FORBIDDEN")

        triage = client.post(
            "/triage/advise",
            headers=h_asset,
            json={
                "unit_code": "U-03",
                "actual_kwh": 820,
                "theoretical_kwh": 1000,
                "irradiance": 780,
                "alarms": [],
                "days_since_cleaning": 45,
            },
        ).json()
        check(
            "分流建议",
            triage["data"]["status"] == "advisory_only" and triage["data"]["candidates"],
            f"top={triage['data']['candidates'][0]['type']}",
        )

        graph = client.get("/wiki/graph", headers=h_asset).json()
        check("知识图谱", graph["data"]["stats"]["nodes"] >= 5, f"nodes={graph['data']['stats']['nodes']}")

        audit = client.get("/audit", headers={"X-API-Key": "sk_admin_demo"}).json()
        check("审计留痕", len(audit["data"]["items"]) >= 1, f"records={len(audit['data']['items'])}")

    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\nseeded: {counts}")
    print(f"{'ALL CHECKS PASSED' if passed == len(checks) else 'CHECKS FAILED'} ({passed}/{len(checks)})")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
