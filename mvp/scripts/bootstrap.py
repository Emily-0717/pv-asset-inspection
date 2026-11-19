# -*- coding: utf-8 -*-
"""初始化脚本：建表 + 写入合成演示数据（可重复执行，不覆盖已有数据）。

为什么用脚本而不是应用启动时自动灌数据：示例账号与样例知识只应存在于
演示环境，生产启动不应"顺手"创建一批人。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # solar_mvp/
sys.path.insert(0, str(ROOT))  # 允许直接以脚本方式运行时导入 app 包

from app.services import rag  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import connect, init_schema, new_id, now_iso  # noqa: E402
from app.security import hash_key  # noqa: E402

# 演示 Key：仅用于本地与测试；真实部署必须由管理员创建并立即轮换
DEMO_USERS = [
    ("u_platform", "t_platform", "平台管理员·演示", "platform_admin", "sk_platform_demo"),
    ("u_admin", "t_solar", "租户管理员·演示", "tenant_admin", "sk_admin_demo"),
    ("u_asset", "t_solar", "资产经理·顾明岚", "asset_manager", "sk_asset_demo"),
    ("u_safety", "t_solar", "安全专员·傅景", "safety_officer", "sk_safety_demo"),
    ("u_clean", "t_solar", "清洗调度·唐映秋", "cleaning_dispatcher", "sk_clean_demo"),
    ("u_engineer", "t_solar", "数据工程·秦芮", "data_engineer", "sk_engineer_demo"),
    ("u_inspector", "t_solar", "巡检员·林亦舟", "inspector", "sk_inspector_demo"),
    ("u_finance", "t_solar", "财务·叶书", "finance_manager", "sk_finance_demo"),
    ("u_viewer", "t_solar", "只读访客·演示", "viewer", "sk_viewer_demo"),
    ("u_other", "t_other", "他站只读·演示", "viewer", "sk_other_demo"),
]

DOCS = [
    (
        "组件积灰排查手册",
        "manual",
        "组件积灰的典型特征是连续多日同向偏低、无逆变器告警、清洗后发电回升。\n"
        "排查顺序：先看辐照是否达到有效门槛，再看清洗记录距今天数，最后对比同朝向组串。\n"
        "若距上次清洗超过 30 天且无明显告警，应优先安排现场核查，再由清洗调度排期。\n"
        "注意：不得仅凭发电量下降就判定积灰，也不得给出增发比例或收益承诺。",
    ),
    (
        "局部遮挡识别与核查",
        "sop",
        "局部遮挡表现为固定时段的功率凹陷，阴影移动会使凹陷时段随季节漂移。\n"
        "核查证据包括：遮挡物照片、遮挡时段实测功率、遮挡台账变更记录。\n"
        "树障与建筑阴影的处理路径不同：树障走绿化协调，建筑阴影走设计复核。\n"
        "若遮挡台账已有记录，应先核对台账版本，避免重复出车。",
    ),
    (
        "逆变器限功率与告警处置",
        "manual",
        "限功率常见原因包括调度限发、设备降额、过温保护、通讯中断。\n"
        "告警码 CURTAIL、DERATE、OVERTEMP、COMM_LOSS 需要分别核对限发指令、设备温度与通讯质量。\n"
        "在确认限功率前，必须取得调度指令或逆变器日志，不得用发电曲线反推。\n"
        "过温降额属于设备问题，应转设备检修，不要排入清洗工单。",
    ),
    (
        "晴天发电偏低排查 SOP",
        "sop",
        "第一步做数据质量门：缺测、冻结值、采样错位一律先补数据，不得进入原因分流。\n"
        "第二步算偏差率：以理论发电量或同组基准为分母，偏差超过阈值才进入分流。\n"
        "第三步按候选原因收集证据：积灰看清洗记录，遮挡看台账，限功率看告警。\n"
        "第四步人工确认：由对应角色确认，安全审查与业务确认不得由同一人兼任。",
    ),
]

NODES = [
    ("cause", "组件积灰", "表面污染导致透光率下降，发电量同向偏低"),
    ("cause", "局部遮挡", "树障或建筑阴影造成固定时段功率凹陷"),
    ("cause", "逆变器限功率", "调度限发、降额、过温或通讯异常导致出力受限"),
    ("cause", "数据质量异常", "缺测、冻结值、口径变更导致读数偏低"),
    ("cause", "其他原因", "组件故障、接线异常等未归类的现场问题"),
    ("evidence", "发电曲线", "实测与理论发电量、等效小时"),
    ("evidence", "逆变器告警", "告警码与状态机记录"),
    ("evidence", "组串电流", "组串电流电压离散度"),
    ("evidence", "巡检记录", "现场照片与巡检结论"),
    ("evidence", "清洗记录", "上次清洗日期与清洗效果"),
    ("evidence", "辐照数据", "气象站辐照与有效时长"),
    ("evidence", "遮挡台账", "遮挡物位置与变更记录"),
    ("action", "清洗排期", "由清洗调度确认后排期，不自动执行"),
    ("action", "现场核查", "巡检员到场核对并回传证据"),
    ("action", "数据补采", "数据工程师补采并标注版本"),
]

# (源标签, 目标标签, 关系)：原因 -> 需要证据 / 建议动作
EDGES = [
    ("组件积灰", "清洗记录", "requires"),
    ("组件积灰", "发电曲线", "requires"),
    ("组件积灰", "清洗排期", "suggests"),
    ("局部遮挡", "遮挡台账", "requires"),
    ("局部遮挡", "组串电流", "requires"),
    ("局部遮挡", "现场核查", "suggests"),
    ("逆变器限功率", "逆变器告警", "requires"),
    ("逆变器限功率", "发电曲线", "requires"),
    ("数据质量异常", "辐照数据", "requires"),
    ("数据质量异常", "数据补采", "suggests"),
]


def seed_users(conn) -> None:
    """写入租户与用户；已存在的 user_id 跳过，保证可重复执行。"""
    conn.execute(
        "INSERT OR IGNORE INTO tenants(tenant_id, name, plan, status, created_at) VALUES(?,?,?,?,?)",
        ("t_platform", "SaaS 平台", "enterprise", "active", now_iso()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO tenants(tenant_id, name, plan, status, created_at) VALUES(?,?,?,?,?)",
        ("t_solar", "晨曦光伏（合成）", "standard", "active", now_iso()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO tenants(tenant_id, name, plan, status, created_at) VALUES(?,?,?,?,?)",
        ("t_other", "对照租户（隔离测试）", "trial", "active", now_iso()),
    )
    for user_id, tenant_id, name, role, key in DEMO_USERS:
        conn.execute(
            """INSERT OR IGNORE INTO users(user_id, tenant_id, name, role, tier, api_key_hash, status, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user_id, tenant_id, name, role, "L1", hash_key(key), "active", now_iso()),
        )
    conn.commit()


def seed_docs(conn) -> None:
    """写入演示文档并建索引；按标题去重，避免重复灌库。"""
    for title, doc_type, content in DOCS:
        exists = conn.execute(
            "SELECT doc_id FROM knowledge_docs WHERE tenant_id='t_solar' AND title=?", (title,)
        ).fetchone()
        if exists:
            continue
        doc_id = new_id("doc")
        conn.execute(
            """INSERT INTO knowledge_docs(doc_id, tenant_id, title, doc_type, source, version,
                                          synthetic, status, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (doc_id, "t_solar", title, doc_type, "seed", "v1", 1, "active", "u_engineer", now_iso()),
        )
        for seq, chunk in enumerate(rag.chunk_text(content, 380, 60)):
            chunk_id = f"{doc_id}-{seq}"
            conn.execute(
                """INSERT INTO knowledge_chunks(chunk_id, doc_id, tenant_id, seq, content, embedding, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (chunk_id, doc_id, "t_solar", seq, chunk, None, now_iso()),
            )
            conn.execute(
                "INSERT INTO knowledge_fts(chunk_id, tenant_id, title, body) VALUES(?,?,?,?)",
                (chunk_id, "t_solar", rag.segment(title), rag.segment(chunk)),
            )
    conn.commit()


def seed_wiki(conn) -> None:
    """写入知识图谱节点与边；节点存在则复用其 id，边按三元组去重。"""
    ids: dict[str, str] = {}
    for kind, label, summary in NODES:
        row = conn.execute(
            "SELECT node_id FROM wiki_nodes WHERE tenant_id='t_solar' AND kind=? AND label=?",
            (kind, label),
        ).fetchone()
        if row:
            ids[label] = row["node_id"]
            continue
        node_id = new_id("nd")
        conn.execute(
            """INSERT INTO wiki_nodes(node_id, tenant_id, kind, label, summary, props, synthetic, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (node_id, "t_solar", kind, label, summary, "{}", 1, now_iso()),
        )
        ids[label] = node_id

    for src, dst, relation in EDGES:
        dup = conn.execute(
            "SELECT edge_id FROM wiki_edges WHERE tenant_id='t_solar' AND src_id=? AND dst_id=? AND relation=?",
            (ids[src], ids[dst], relation),
        ).fetchone()
        if dup:
            continue
        conn.execute(
            """INSERT INTO wiki_edges(edge_id, tenant_id, src_id, dst_id, relation, weight, evidence_id, created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (new_id("eg"), "t_solar", ids[src], ids[dst], relation, 1.0, None, now_iso()),
        )
    conn.commit()


def bootstrap(config_path: str | None = None) -> dict[str, int]:
    """建表 + 灌样例；返回各表计数，便于初始化脚本自检。"""
    settings = load_settings(config_path)
    init_schema(settings)
    conn = connect(settings.storage.db_path)
    try:
        seed_users(conn)
        seed_docs(conn)
        seed_wiki(conn)
        counts = {
            "tenants": conn.execute("SELECT COUNT(*) AS c FROM tenants").fetchone()["c"],
            "users": conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"],
            "docs": conn.execute("SELECT COUNT(*) AS c FROM knowledge_docs").fetchone()["c"],
            "chunks": conn.execute("SELECT COUNT(*) AS c FROM knowledge_chunks").fetchone()["c"],
            "nodes": conn.execute("SELECT COUNT(*) AS c FROM wiki_nodes").fetchone()["c"],
            "edges": conn.execute("SELECT COUNT(*) AS c FROM wiki_edges").fetchone()["c"],
        }
    finally:
        conn.close()
    return counts


if __name__ == "__main__":
    result = bootstrap(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(result, ensure_ascii=False))
