# -*- coding: utf-8 -*-
"""Wiki 知识图谱：节点/边 + 邻接查询 + 图数据导出。

为什么用图而不是纯文本：排查链路是"现象→候选原因→所需证据→处置动作"，
用边把这条链显式存下来，Agent 才能按边检索，而不是靠模型自由联想。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..db import new_id, now_iso
from ..errors import AppError, ErrorCode


def _node_row(row: sqlite3.Row) -> dict[str, Any]:
    """行转字典；props 解析失败时返回空对象而不是 500。"""
    try:
        props = json.loads(row["props"])
    except (TypeError, json.JSONDecodeError):
        props = {}
    return {
        "node_id": row["node_id"],
        "kind": row["kind"],
        "label": row["label"],
        "summary": row["summary"],
        "props": props,
    }


def create_node(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    kind: str,
    label: str,
    summary: str,
    props: dict[str, Any],
) -> dict[str, Any]:
    """新增节点；同租户同类型同标签视为重复，返回 409。

    失败行为：重复抛 CONFLICT；外键/唯一约束异常由调用方兜底为 500。
    """
    dup = conn.execute(
        "SELECT node_id FROM wiki_nodes WHERE tenant_id=? AND kind=? AND label=?",
        (tenant_id, kind, label),
    ).fetchone()
    if dup:
        raise AppError(ErrorCode.CONFLICT, 409, f"节点已存在: {label}")

    node_id = new_id("nd")
    conn.execute(
        """INSERT INTO wiki_nodes(node_id, tenant_id, kind, label, summary, props, synthetic, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (node_id, tenant_id, kind, label, summary, json.dumps(props, ensure_ascii=False), 1, now_iso()),
    )
    conn.commit()
    return {"node_id": node_id, "kind": kind, "label": label, "summary": summary, "props": props}


def create_edge(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    src_id: str,
    dst_id: str,
    relation: str,
    weight: float,
    evidence_id: str | None,
) -> dict[str, Any]:
    """新增有向边；两端必须存在且同租户，否则 404/403。

    为什么校验租户：只校验存在性的话，A 租户可以把自己节点指向 B 租户，
    图查询会把别人的知识带出来。
    """
    rows = conn.execute(
        "SELECT node_id, tenant_id FROM wiki_nodes WHERE node_id IN (?,?)",
        (src_id, dst_id),
    ).fetchall()
    found = {r["node_id"]: r["tenant_id"] for r in rows}
    if src_id not in found or dst_id not in found:
        raise AppError(ErrorCode.NOT_FOUND, 404, "边的一端节点不存在")
    if found[src_id] != tenant_id or found[dst_id] != tenant_id:
        raise AppError(ErrorCode.TENANT_FORBIDDEN, 403, "不能跨租户连边")

    edge_id = new_id("eg")
    conn.execute(
        """INSERT INTO wiki_edges(edge_id, tenant_id, src_id, dst_id, relation, weight, evidence_id, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (edge_id, tenant_id, src_id, dst_id, relation, weight, evidence_id, now_iso()),
    )
    conn.commit()
    return {
        "edge_id": edge_id, "src_id": src_id, "dst_id": dst_id,
        "relation": relation, "weight": weight, "evidence_id": evidence_id,
    }


def list_nodes(conn: sqlite3.Connection, tenant_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """按租户列出节点，可按 kind 过滤；按创建时间稳定排序。"""
    if kind:
        rows = conn.execute(
            "SELECT * FROM wiki_nodes WHERE tenant_id=? AND kind=? ORDER BY created_at",
            (tenant_id, kind),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM wiki_nodes WHERE tenant_id=? ORDER BY created_at", (tenant_id,)
        ).fetchall()
    return [_node_row(r) for r in rows]


def lookup(conn: sqlite3.Connection, tenant_id: str, text: str, limit: int = 5) -> list[dict[str, Any]]:
    """按标签模糊匹配；Agent 用它在问答时补一条图上的上下文。"""
    rows = conn.execute(
        """SELECT * FROM wiki_nodes
           WHERE tenant_id=? AND (label LIKE ? OR summary LIKE ?)
           ORDER BY length(label) LIMIT ?""",
        (tenant_id, f"%{text}%", f"%{text}%", limit),
    ).fetchall()
    return [_node_row(r) for r in rows]


def graph(conn: sqlite3.Connection, tenant_id: str) -> dict[str, Any]:
    """导出整张图（节点 + 边 + 计数），供前端或调试使用。"""
    nodes = list_nodes(conn, tenant_id)
    edges = conn.execute(
        "SELECT edge_id, src_id, dst_id, relation, weight, evidence_id FROM wiki_edges WHERE tenant_id=?",
        (tenant_id,),
    ).fetchall()
    return {
        "nodes": nodes,
        "edges": [dict(r) for r in edges],
        "stats": {"nodes": len(nodes), "edges": len(edges)},
    }


def neighbors(conn: sqlite3.Connection, tenant_id: str, node_id: str) -> dict[str, Any]:
    """取一跳邻居；用于"这个原因需要哪些证据"的追问。"""
    edges = conn.execute(
        """SELECT * FROM wiki_edges
           WHERE tenant_id=? AND (src_id=? OR dst_id=?)""",
        (tenant_id, node_id, node_id),
    ).fetchall()
    ids = {node_id}
    for e in edges:
        ids.add(e["src_id"])
        ids.add(e["dst_id"])
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM wiki_nodes WHERE tenant_id=? AND node_id IN ({placeholders})",
        (tenant_id, *ids),
    ).fetchall()
    return {"center": node_id, "nodes": [_node_row(r) for r in rows], "edges": [dict(e) for e in edges]}
