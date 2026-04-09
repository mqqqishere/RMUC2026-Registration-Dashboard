#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHARK 决策面板后端：只基于当前实时填报快照的三志愿预估。

实现原则：
1. 其他学校沿用主看板当前同一份输入：实时提交 > 承办默认 > 就近估算
2. SHARK 分别假设报南部 / 东部 / 北部，各跑一次真实调剂
3. 资格结果与赛区排名复用主看板已有的综合实力榜与资格模型
4. 不做多情景压力测试，输出的是“当前快照下”的机会评估
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import allocator as A  # noqa: E402
import qualification as Q  # noqa: E402

SHARK_SCHOOL = "江南大学霞客湾校区"
SAME_CITY_RIVAL = "南京理工大学江阴校区"
ROSTER_PATH = os.path.join(ROOT, "robomaster_2026_teams.csv")

REGIONS = A.REGIONS
STATUS_CN = {"host": "承办", "volunteer": "志愿", "transfer": "调剂"}

QUALIFICATION_BASE_SCORE = {
    "national": 120.0,
    "revival": 72.0,
    "none": 0.0,
}

MODEL_WEIGHTS = {
    "advance_margin": 12.0,
    "national_margin": 4.0,
    "advance_gap": 2.2,
    "national_gap": 1.1,
    "rank_bonus": 0.7,
    "rank_anchor": 18,
    "volunteer_bonus": 6.0,
    "transfer_bonus": 0.0,
    "gap_clip": 5.0,
}


def load_roster() -> Dict[str, dict]:
    if not os.path.exists(ROSTER_PATH):
        return {}
    return Q.load_roster(ROSTER_PATH)


def _extract_live_volunteers(live_teams) -> Dict[str, str]:
    return Q.extract_live_volunteers(live_teams or [])


def _build_projected_teams(
    roster: Dict[str, dict],
    distances,
    base_volunteers: Dict[str, str],
    shark_override: Optional[str] = None,
) -> Tuple[List[A.Team], Dict[str, str]]:
    live = dict(base_volunteers)
    if shark_override is not None:
        live[SHARK_SCHOOL] = shark_override
    return Q.build_projected_teams(roster, distances, live)


def _school_at(members: List[A.Team], index: int) -> Optional[str]:
    if 0 <= index < len(members):
        return members[index].school
    return None


def _score_gap(
    strength_info: Dict[str, dict],
    school: str,
    boundary_school: Optional[str],
) -> Optional[float]:
    if not boundary_school:
        return None
    return round(
        strength_info[school]["strength_score"]
        - strength_info[boundary_school]["strength_score"],
        4,
    )


def _ff_position(members: List[A.Team], roster: Dict[str, dict]) -> Optional[int]:
    shark_ff = roster[SHARK_SCHOOL]["full_form_ranking"]
    if shark_ff is None:
        return None
    return 1 + sum(
        1
        for team in members
        if roster[team.school]["full_form_ranking"] is not None
        and roster[team.school]["full_form_ranking"] < shark_ff
    )


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _decision_components(option: dict) -> Dict[str, float]:
    advance_gap = _clip(
        float(option["advance_gap_score"] or 0.0),
        -MODEL_WEIGHTS["gap_clip"],
        MODEL_WEIGHTS["gap_clip"],
    )
    national_gap = _clip(
        float(option["national_gap_score"] or 0.0),
        -MODEL_WEIGHTS["gap_clip"],
        MODEL_WEIGHTS["gap_clip"],
    )
    rank_bonus = max(
        0.0,
        (MODEL_WEIGHTS["rank_anchor"] - float(option["strength_rank_region"])) * MODEL_WEIGHTS["rank_bonus"],
    )
    status_bonus = (
        MODEL_WEIGHTS["volunteer_bonus"]
        if option["status"] == "volunteer"
        else MODEL_WEIGHTS["transfer_bonus"]
    )

    components = {
        "qualification_base": QUALIFICATION_BASE_SCORE[option["qualification_status"]],
        "advance_margin": float(option["advance_margin"]) * MODEL_WEIGHTS["advance_margin"],
        "national_margin": float(option["national_margin"]) * MODEL_WEIGHTS["national_margin"],
        "advance_gap": advance_gap * MODEL_WEIGHTS["advance_gap"],
        "national_gap": national_gap * MODEL_WEIGHTS["national_gap"],
        "rank_bonus": rank_bonus,
        "status_bonus": status_bonus,
    }
    components["total"] = round(sum(components.values()), 4)
    return {key: round(value, 4) for key, value in components.items()}


def _build_summary(option: dict) -> str:
    if option["qualification_status"] == "national":
        line_text = f"当前在国赛线内 {option['national_margin']} 位"
    elif option["qualification_status"] == "revival":
        line_text = (
            f"当前落后国赛线 {abs(option['national_margin'])} 位，"
            f"但仍在复活赛线内 {option['advance_margin']} 位"
        )
    else:
        line_text = f"当前落后总晋级线 {abs(option['advance_margin'])} 位"

    return (
        f"报志愿 {option['shark_volunteer']} 后，当前快照下最终落在 {option['final_region']}，"
        f"以{option['status_cn']}身份进入该赛区，综合实力排赛区第 {option['strength_rank_region']}。"
        f"{line_text}，机会分 {option['decision_score']:.1f}。"
    )


def _simulate_choice(
    roster: Dict[str, dict],
    distances,
    strength_info: Dict[str, dict],
    base_volunteers: Dict[str, str],
    shark_override: str,
) -> dict:
    teams, volunteer_source = _build_projected_teams(
        roster, distances, base_volunteers, shark_override
    )
    result = A.allocate(teams, verbose=False)
    qualification_panel = Q.assign_qualifications(result, strength_info, roster)
    by_school = {team.school: team for team in result.teams}
    shark = by_school[SHARK_SCHOOL]
    assigned_region = shark.assigned
    members = qualification_panel["region_rankings"][assigned_region]

    strength_rank_region = qualification_panel["strength_rank_region"][SHARK_SCHOOL]
    qualification_status = qualification_panel["qualification_status"][SHARK_SCHOOL]
    national_spots = qualification_panel["national_by_region"][assigned_region]
    revival_spots = qualification_panel["revival_by_region"][assigned_region]
    advance_cut = national_spots + revival_spots

    national_line_school = _school_at(members, national_spots - 1) if national_spots else None
    advance_line_school = _school_at(members, advance_cut - 1) if advance_cut else None
    first_out_school = _school_at(members, advance_cut) if advance_cut < len(members) else None

    option = {
        "shark_volunteer": shark_override,
        "volunteer_source": volunteer_source.get(SHARK_SCHOOL),
        "final_region": assigned_region,
        "status": shark.status,
        "status_cn": STATUS_CN.get(shark.status, shark.status),
        "qualification_status": qualification_status,
        "qualification_label": Q.qualification_label(qualification_status),
        "strength_score": strength_info[SHARK_SCHOOL]["strength_score"],
        "strength_rank_global": strength_info[SHARK_SCHOOL]["strength_rank_global"],
        "strength_rank_region": strength_rank_region,
        "ff_position": _ff_position(members, roster),
        "national_spots": national_spots,
        "revival_spots": revival_spots,
        "national_margin": national_spots - strength_rank_region,
        "advance_margin": advance_cut - strength_rank_region,
        "national_line_school": national_line_school,
        "advance_line_school": advance_line_school,
        "first_out_school": first_out_school,
        "national_gap_score": _score_gap(
            strength_info, SHARK_SCHOOL, national_line_school
        ),
        "advance_gap_score": _score_gap(
            strength_info, SHARK_SCHOOL, advance_line_school
        ),
        "region_strength_stats": qualification_panel["region_strength_stats"][assigned_region],
    }
    option["decision_components"] = _decision_components(option)
    option["decision_score"] = option["decision_components"]["total"]
    option["summary"] = _build_summary(option)
    return option


def _current_snapshot_state(
    roster: Dict[str, dict],
    distances,
    strength_info: Dict[str, dict],
    base_volunteers: Dict[str, str],
) -> Tuple[dict, Optional[dict]]:
    teams, _ = _build_projected_teams(roster, distances, base_volunteers)
    result = A.allocate(teams, verbose=False)
    qualification_panel = Q.assign_qualifications(result, strength_info, roster)
    by_school = {team.school: team for team in result.teams}

    shark = by_school[SHARK_SCHOOL]
    rival = by_school.get(SAME_CITY_RIVAL)

    observed = {
        "submitted_count": len(base_volunteers),
        "roster_total": len(roster),
        "submitted_by_region": {
            region: sum(1 for volunteer in base_volunteers.values() if volunteer == region)
            for region in REGIONS
        },
        "projected_by_region": {
            region: len(result.regions[region])
            for region in REGIONS
        },
        "shark_submitted": SHARK_SCHOOL in base_volunteers,
        "shark_live_volunteer": base_volunteers.get(SHARK_SCHOOL),
        "shark_projected_region": shark.assigned,
        "shark_projected_status": shark.status,
        "shark_projected_status_cn": STATUS_CN.get(shark.status, shark.status),
        "shark_projected_qualification": qualification_panel["qualification_status"][SHARK_SCHOOL],
        "shark_projected_qualification_label": Q.qualification_label(
            qualification_panel["qualification_status"][SHARK_SCHOOL]
        ),
    }

    rival_info = None
    if rival is not None and SAME_CITY_RIVAL in roster:
        rival_info = {
            "school": SAME_CITY_RIVAL,
            "team": roster[SAME_CITY_RIVAL]["team"],
            "city": roster[SAME_CITY_RIVAL]["city"],
            "rank": roster[SAME_CITY_RIVAL]["points_rank"],
            "ff": roster[SAME_CITY_RIVAL]["full_form_ranking"],
            "team_type": roster[SAME_CITY_RIVAL]["team_type"],
            "live_volunteer": base_volunteers.get(SAME_CITY_RIVAL),
            "submitted": SAME_CITY_RIVAL in base_volunteers,
            "projected_region": rival.assigned,
            "projected_status": rival.status,
            "projected_status_cn": STATUS_CN.get(rival.status, rival.status),
            "projected_qualification": qualification_panel["qualification_status"][SAME_CITY_RIVAL],
            "projected_qualification_label": Q.qualification_label(
                qualification_panel["qualification_status"][SAME_CITY_RIVAL]
            ),
            "strength_rank_region": qualification_panel["strength_rank_region"][SAME_CITY_RIVAL],
        }

    return observed, rival_info


def _recommendation_key(option: dict):
    return (
        option["decision_score"],
        QUALIFICATION_BASE_SCORE[option["qualification_status"]],
        option["advance_margin"],
        option["national_margin"],
        1 if option["status"] == "volunteer" else 0,
    )


def select_recommendation_option(options: List[dict]) -> dict:
    return max(options, key=_recommendation_key)


def _build_recommendation(option: dict) -> dict:
    return {
        "priority_mode": "current_snapshot_score",
        "volunteer": option["shark_volunteer"],
        "final_region": option["final_region"],
        "status": option["status"],
        "status_cn": option["status_cn"],
        "qualification_status": option["qualification_status"],
        "qualification_label": option["qualification_label"],
        "strength_rank_region": option["strength_rank_region"],
        "decision_score": option["decision_score"],
        "national_margin": option["national_margin"],
        "advance_margin": option["advance_margin"],
        "national_gap_score": option["national_gap_score"],
        "advance_gap_score": option["advance_gap_score"],
        "summary": (
            f"推荐报志愿 {option['shark_volunteer']}。在当前实时填报快照下，"
            f"SHARK 最终会落在 {option['final_region']}，当前预估结果为"
            f"{option['qualification_label']}，赛区排名第 {option['strength_rank_region']}，"
            f"机会分 {option['decision_score']:.1f}。"
        ),
    }


def build_decision_panel(
    live_teams,
    distances,
    roster: Optional[Dict[str, dict]] = None,
    strength_info: Optional[Dict[str, dict]] = None,
) -> Optional[dict]:
    roster = roster or load_roster()
    if not roster or SHARK_SCHOOL not in roster:
        return None

    if strength_info is None:
        strength_info, _ = Q.compute_strength_table(roster)

    base_volunteers = _extract_live_volunteers(live_teams)
    observed_state, same_city_rival = _current_snapshot_state(
        roster, distances, strength_info, base_volunteers
    )
    options = [
        _simulate_choice(roster, distances, strength_info, base_volunteers, choice)
        for choice in REGIONS
    ]
    recommendation = _build_recommendation(select_recommendation_option(options))

    shark_row = roster[SHARK_SCHOOL]
    target = {
        "school": SHARK_SCHOOL,
        "team": shark_row.get("team", "SHARK"),
        "city": shark_row["city"],
        "team_type": shark_row["team_type"],
        "rank": shark_row["points_rank"],
        "ff": shark_row["full_form_ranking"],
        "points": shark_row.get("points"),
        "detail_2025": shark_row["detail_2025"],
        "detail_2025_cn": shark_row["detail_2025_cn"],
        "rmul_2026_top4": shark_row.get("rmul_2026_top4") or None,
        "rmul_2026_top4_cn": shark_row.get("rmul_2026_top4_cn") or None,
        "strength_score": strength_info[SHARK_SCHOOL]["strength_score"],
        "strength_rank_global": strength_info[SHARK_SCHOOL]["strength_rank_global"],
        "dist": {
            "南部": shark_row["distance_to_changsha"],
            "东部": shark_row["distance_to_jinan"],
            "北部": shark_row["distance_to_shenyang"],
        },
    }

    return {
        "model_note": (
            "当前不做多情景压力测试，只基于当前实时填报快照推演。"
            "机会分 = 当前资格档位 + 距国赛/总晋级线余裕 + 与边界队伍的实力分差 + "
            "赛区内排名修正 + 志愿直录修正。"
        ),
        "model_weights": dict(MODEL_WEIGHTS),
        "target": target,
        "same_city_rival": same_city_rival,
        "observed_state": observed_state,
        "options": options,
        "recommendation": recommendation,
    }
