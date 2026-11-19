# -*- coding: utf-8 -*-
"""分流建议：规则算偏差与候选，证据只能来自知识库/图谱，绝不编造。

定位提醒：这里输出的是"建议"，不是结论；落库与双卡点确认属于后续阶段。
没有证据时置信度必须为 None，而不是给个 0.5 蒙混过去。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..config import Settings

# 告警码 → 限功率类；用集合而不是模糊匹配，避免 "COMM_OK" 被误判
CURTAIL_ALARMS = {
    "CURTAIL", "LIMIT_ACTIVE", "DERATE", "OVERTEMP", "FAN_FAIL",
    "COMM_LOSS", "GRID_LIMIT", "POWER_LIMIT",
}

# 候选 → 需要补的材料；文案直接给到人，减少"再问一轮"
MISSING = {
    "dust": ["上次清洗日期", "清洗前后发电对比", "组件衰减报告"],
    "shading": ["遮挡物照片", "遮挡时段实测功率", "遮挡台账变更记录"],
    "curtailment": ["调度限发指令", "逆变器运行日志", "额定功率参数"],
    "data_quality": ["数据源版本号", "表计校验单", "补采记录"],
    "other": ["现场检测报告", "组串离散度明细"],
}

# 候选 → 图谱里的原因标签，用于挂真实 node_id 作证据
CAUSE_LABEL = {
    "dust": "组件积灰",
    "shading": "局部遮挡",
    "curtailment": "逆变器限功率",
    "data_quality": "数据质量异常",
    "other": "其他原因",
}


def _evidence_ids(conn: sqlite3.Connection, tenant_id: str, candidate: str) -> list[str]:
    """取该候选在图上对应的节点 id；没有就没有，不做兜底编造。"""
    label = CAUSE_LABEL[candidate]
    rows = conn.execute(
        "SELECT node_id FROM wiki_nodes WHERE tenant_id=? AND kind='cause' AND label=?",
        (tenant_id, label),
    ).fetchall()
    return [r["node_id"] for r in rows]


def advise(
    conn: sqlite3.Connection,
    settings: Settings,
    tenant_id: str,
    *,
    unit_code: str,
    actual_kwh: float,
    theoretical_kwh: float,
    irradiance: float,
    alarms: list[str],
    days_since_cleaning: int | None,
    shading_logged: bool,
    data_quality_ok: bool,
) -> dict[str, Any]:
    """按规则产出候选原因、证据、置信度与待补材料。

    出参：{unit_code, deviation_ratio, severity, candidates[], threshold_version, status, note}
    失败行为：不抛异常；证据缺失时对应候选 confidence 为 None 并标 evidence="insufficient"。
    """
    deviation = max(0.0, (theoretical_kwh - actual_kwh) / theoretical_kwh)
    severity = (
        "alert" if deviation >= 0.20 else "warn" if deviation >= 0.12 else "normal"
    )  # 分档仅用于展示，真实阈值以 config 为准

    candidates: list[dict[str, Any]] = []

    def add(kind: str, strength: float, reason: str) -> None:
        """收集候选；置信度只由强度和证据共同决定。"""
        evidence = _evidence_ids(conn, tenant_id, kind)
        # 无证据 → 不出置信度；这是硬规则，不能为了"好看"补一个数字
        confidence = round(min(0.95, strength * (0.7 + 0.3 * min(deviation / 0.2, 1.0))), 2) if evidence else None
        candidates.append(
            {
                "type": kind,
                "reason": reason,
                "evidence_ids": evidence,
                "evidence_status": "ok" if evidence else "insufficient",
                "confidence": confidence,
                "missing": MISSING[kind],
            }
        )

    if not data_quality_ok:
        add("data_quality", 0.85, "数据质量标记异常，先补数据再谈发电偏低")
    if irradiance < 500:
        add("data_quality", 0.6, "辐照低于有效门槛，偏差不可比")

    alarm_hit = [a for a in alarms if a.upper() in CURTAIL_ALARMS]
    if alarm_hit:
        add("curtailment", 0.8, f"命中限功率相关告警: {','.join(alarm_hit)}")
    if shading_logged:
        add("shading", 0.65, "遮挡台账存在本单元记录")
    if days_since_cleaning is None or days_since_cleaning >= 30:
        add("dust", 0.55, "距上次清洗超过 30 天或缺失清洗记录")

    if not candidates and deviation > 0:
        add("other", 0.35, "偏差存在但无规则命中，需现场核实")

    # 同一候选可能被多条规则命中，按类型合并后取最高强度
    merged: dict[str, dict[str, Any]] = {}
    for item in candidates:
        key = item["type"]
        if key in merged and (item["confidence"] or 0) <= (merged[key]["confidence"] or 0):
            merged[key]["reason"] += f"；{item['reason']}"
        else:
            merged[key] = item
    ranked = sorted(merged.values(), key=lambda x: (x["confidence"] is None, -(x["confidence"] or 0)))
    for i, item in enumerate(ranked, 1):
        item["rank"] = i

    return {
        "unit_code": unit_code,
        "deviation_ratio": round(deviation, 4),
        "severity": severity,
        "candidates": ranked,
        "threshold_version": settings.threshold_version,
        "status": "advisory_only",  # 明确：未落库、未确认、不可当结论用
        "note": "本结果仅为人工排查建议，需安全审查与人工确认后方可进入处置流程。",
    }
