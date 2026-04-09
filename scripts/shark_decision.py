#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
志愿决策面板后端：基于当前实时填报快照，为每所学校评估三志愿去向。

实现原则：
1. 其他学校沿用主看板当前同一份输入：实时提交 > 承办默认 > 就近估算
2. 目标学校分别假设报南部 / 东部 / 北部，各跑一次真实调剂
3. 资格结果与赛区排名复用主看板已有的综合实力榜与资格模型
4. 不做多情景压力测试，输出的是“当前快照下”的机会评估
5. 为控制构建耗时，每个场景只模拟目标学校最终落入的赛区，并缓存相同赛区盘面
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import allocator as A  # noqa: E402
import qualification as Q  # noqa: E402
import swiss_simulation as S  # noqa: E402

SHARK_SCHOOL = "江南大学霞客湾校区"
ROSTER_PATH = os.path.join(ROOT, "robomaster_2026_teams.csv")

REGIONS = A.REGIONS
STATUS_CN = {"host": "承办", "volunteer": "志愿", "transfer": "调剂"}

MODEL_PRIORITIES = {
    "primary": "revival_probability",
    "secondary": "national_probability",
    "tertiary": "average_rank",
    "status_bonus": "volunteer",
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
    target_school: Optional[str] = None,
    volunteer_override: Optional[str] = None,
) -> Tuple[List[A.Team], Dict[str, str]]:
    live = dict(base_volunteers)
    if target_school is not None and volunteer_override is not None:
        live[target_school] = volunteer_override
    return Q.build_projected_teams(roster, distances, live)


def _ff_position(
    target_school: str,
    members: List[A.Team],
    roster: Dict[str, dict],
) -> Optional[int]:
    target_ff = roster[target_school]["full_form_ranking"]
    if target_ff is None:
        return None
    return 1 + sum(
        1
        for team in members
        if roster[team.school]["full_form_ranking"] is not None
        and roster[team.school]["full_form_ranking"] < target_ff
    )


def _probability_status(row: dict) -> str:
    if row.get("national_probability", 0.0) >= max(
        row.get("revival_only_probability", 0.0),
        row.get("out_probability", 0.0),
    ):
        return "national"
    if row.get("revival_only_probability", 0.0) >= row.get("out_probability", 0.0):
        return "revival"
    return "none"


def _probability_decision_components(option: dict) -> Dict[str, float]:
    components = {
        "revival_probability": round(float(option["revival_probability"]) * 100.0, 4),
        "national_probability": round(float(option["national_probability"]) * 40.0, 4),
        "top8_probability": round(float(option["top8_probability"]) * 12.0, 4),
        "top4_probability": round(float(option["top4_probability"]) * 16.0, 4),
        "average_rank_penalty": round(-float(option["average_rank"]) * 0.8, 4),
        "status_bonus": 6.0 if option["status"] == "volunteer" else 0.0,
    }
    components["total"] = round(sum(components.values()), 4)
    return components


def _build_probability_summary(option: dict) -> str:
    return (
        f"报志愿 {option['volunteer']} 后，当前快照下最终落在 {option['final_region']}，"
        f"以{option['status_cn']}身份进入该赛区。"
        f"复活及以上概率 {option['revival_probability'] * 100:.1f}%，"
        f"国赛概率 {option['national_probability'] * 100:.1f}%，"
        f"平均名次 {option['average_rank']:.2f}。"
    )


def _build_target_profile(
    target_school: str,
    roster: Dict[str, dict],
    strength_info: Dict[str, dict],
) -> dict:
    row = roster[target_school]
    return {
        "school": target_school,
        "team": row.get("team", target_school),
        "city": row["city"],
        "team_type": row["team_type"],
        "rank": row["points_rank"],
        "ff": row["full_form_ranking"],
        "points": row.get("points"),
        "detail_2025": row["detail_2025"],
        "detail_2025_cn": row["detail_2025_cn"],
        "rmul_2026_top4": row.get("rmul_2026_top4") or None,
        "rmul_2026_top4_cn": row.get("rmul_2026_top4_cn") or None,
        "strength_score": strength_info[target_school]["strength_score"],
        "strength_rank_global": strength_info[target_school]["strength_rank_global"],
        "dist": {
            "南部": row["distance_to_changsha"],
            "东部": row["distance_to_jinan"],
            "北部": row["distance_to_shenyang"],
        },
    }


def _region_cache_key(
    region: str,
    members: List[A.Team],
    national_spots: int,
    revival_spots: int,
) -> Tuple[str, int, int, Tuple[str, ...]]:
    return (
        region,
        national_spots,
        revival_spots,
        tuple(sorted(team.school for team in members)),
    )


def _region_probability_panel(
    region: str,
    members: List[A.Team],
    roster: Dict[str, dict],
    strength_info: Dict[str, dict],
    national_spots: int,
    revival_spots: int,
    cache: Dict[Tuple[str, int, int, Tuple[str, ...]], dict],
) -> dict:
    key = _region_cache_key(region, members, national_spots, revival_spots)
    cached = cache.get(key)
    if cached is not None:
        return cached

    panel = S.build_swiss_simulation(
        {region: members},
        strength_info,
        roster,
        {region: national_spots},
        {region: revival_spots},
        default_school=members[0].school,
        include_samples=False,
    )
    cached = {
        "region": panel["regions"][0],
        "school_by_name": {row["school"]: row for row in panel["schools"]},
    }
    cache[key] = cached
    return cached


def _simulate_choice(
    target_school: str,
    roster: Dict[str, dict],
    distances,
    strength_info: Dict[str, dict],
    base_volunteers: Dict[str, str],
    volunteer_choice: str,
    region_cache: Dict[Tuple[str, int, int, Tuple[str, ...]], dict],
) -> dict:
    teams, volunteer_source = _build_projected_teams(
        roster,
        distances,
        base_volunteers,
        target_school=target_school,
        volunteer_override=volunteer_choice,
    )
    result = A.allocate(teams, verbose=False)
    qualification_panel = Q.assign_qualifications(result, strength_info, roster)
    by_school = {team.school: team for team in result.teams}
    target_team = by_school[target_school]
    assigned_region = target_team.assigned
    members = qualification_panel["region_rankings"][assigned_region]
    national_spots = qualification_panel["national_by_region"][assigned_region]
    revival_spots = qualification_panel["revival_by_region"][assigned_region]
    swiss_panel = _region_probability_panel(
        assigned_region,
        members,
        roster,
        strength_info,
        national_spots,
        revival_spots,
        region_cache,
    )
    swiss_row = swiss_panel["school_by_name"][target_school]
    swiss_region = swiss_panel["region"]
    qualification_status = _probability_status(swiss_row)

    option = {
        "volunteer": volunteer_choice,
        "volunteer_source": volunteer_source.get(target_school),
        "final_region": assigned_region,
        "status": target_team.status,
        "status_cn": STATUS_CN.get(target_team.status, target_team.status),
        "qualification_status": qualification_status,
        "qualification_label": Q.qualification_label(qualification_status),
        "strength_score": strength_info[target_school]["strength_score"],
        "strength_rank_global": strength_info[target_school]["strength_rank_global"],
        "strength_rank_region": qualification_panel["strength_rank_region"][target_school],
        "ff_position": _ff_position(target_school, members, roster),
        "national_spots": national_spots,
        "revival_spots": revival_spots,
        "national_margin": None,
        "advance_margin": None,
        "national_line_school": swiss_region.get("bubble", {}).get("last_national", {}).get("school"),
        "advance_line_school": swiss_region.get("bubble", {}).get("last_revival", {}).get("school"),
        "first_out_school": swiss_region.get("bubble", {}).get("first_out", {}).get("school"),
        "national_gap_score": None,
        "advance_gap_score": None,
        "region_strength_stats": qualification_panel["region_strength_stats"][assigned_region],
        "national_probability": swiss_row["national_probability"],
        "revival_probability": swiss_row["revival_probability"],
        "revival_only_probability": swiss_row["revival_only_probability"],
        "out_probability": swiss_row["out_probability"],
        "average_rank": swiss_row["average_rank"],
        "top8_probability": swiss_row["top8_probability"],
        "top4_probability": swiss_row["top4_probability"],
        "most_likely_stage": swiss_row["most_likely_stage"],
        "most_likely_stage_label": swiss_row["most_likely_stage_label"],
        "most_likely_stage_probability": swiss_row["most_likely_stage_probability"],
        "swiss_priority_mode": "swiss_probability_total_advance_first",
    }
    option["decision_components"] = _probability_decision_components(option)
    option["decision_score"] = option["decision_components"]["total"]
    option["summary"] = _build_probability_summary(option)
    return option


def _current_snapshot_context(
    roster: Dict[str, dict],
    distances,
    strength_info: Dict[str, dict],
    base_volunteers: Dict[str, str],
) -> dict:
    teams, volunteer_source = _build_projected_teams(roster, distances, base_volunteers)
    result = A.allocate(teams, verbose=False)
    qualification_panel = Q.assign_qualifications(result, strength_info, roster)
    by_school = {team.school: team for team in result.teams}
    city_to_schools: Dict[str, List[str]] = defaultdict(list)
    for school, row in roster.items():
        city_to_schools[row["city"]].append(school)
    return {
        "result": result,
        "qualification_panel": qualification_panel,
        "by_school": by_school,
        "volunteer_source": volunteer_source,
        "city_to_schools": city_to_schools,
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
    }


def _observed_state(
    target_school: str,
    base_volunteers: Dict[str, str],
    snapshot: dict,
) -> dict:
    target_team = snapshot["by_school"][target_school]
    qualification_panel = snapshot["qualification_panel"]
    return {
        "submitted_count": snapshot["submitted_count"],
        "roster_total": snapshot["roster_total"],
        "submitted_by_region": snapshot["submitted_by_region"],
        "projected_by_region": snapshot["projected_by_region"],
        "school_submitted": target_school in base_volunteers,
        "live_volunteer": base_volunteers.get(target_school),
        "projected_region": target_team.assigned,
        "projected_status": target_team.status,
        "projected_status_cn": STATUS_CN.get(target_team.status, target_team.status),
        "projected_qualification": qualification_panel["qualification_status"][target_school],
        "projected_qualification_label": Q.qualification_label(
            qualification_panel["qualification_status"][target_school]
        ),
    }


def _same_city_rival(
    target_school: str,
    roster: Dict[str, dict],
    base_volunteers: Dict[str, str],
    snapshot: dict,
) -> Optional[dict]:
    city = roster[target_school]["city"]
    candidates = [
        school
        for school in snapshot["city_to_schools"].get(city, [])
        if school != target_school
    ]
    if len(candidates) != 1:
        return None

    rival_school = candidates[0]
    rival = snapshot["by_school"].get(rival_school)
    if rival is None:
        return None

    qualification_panel = snapshot["qualification_panel"]
    return {
        "school": rival_school,
        "team": roster[rival_school]["team"],
        "city": roster[rival_school]["city"],
        "rank": roster[rival_school]["points_rank"],
        "ff": roster[rival_school]["full_form_ranking"],
        "team_type": roster[rival_school]["team_type"],
        "live_volunteer": base_volunteers.get(rival_school),
        "submitted": rival_school in base_volunteers,
        "projected_region": rival.assigned,
        "projected_status": rival.status,
        "projected_status_cn": STATUS_CN.get(rival.status, rival.status),
        "projected_qualification": qualification_panel["qualification_status"][rival_school],
        "projected_qualification_label": Q.qualification_label(
            qualification_panel["qualification_status"][rival_school]
        ),
        "strength_rank_region": qualification_panel["strength_rank_region"][rival_school],
    }


def _recommendation_key(option: dict):
    return (
        option["revival_probability"],
        option["national_probability"],
        -option["average_rank"],
        1 if option["status"] == "volunteer" else 0,
        -REGIONS.index(option["volunteer"]),
    )


def select_recommendation_option(options: List[dict]) -> dict:
    return max(options, key=_recommendation_key)


def _build_recommendation(target_school: str, option: dict) -> dict:
    return {
        "priority_mode": "swiss_probability_total_advance_first",
        "volunteer": option["volunteer"],
        "final_region": option["final_region"],
        "status": option["status"],
        "status_cn": option["status_cn"],
        "qualification_status": option["qualification_status"],
        "qualification_label": option["qualification_label"],
        "strength_rank_region": option["strength_rank_region"],
        "decision_score": option["decision_score"],
        "national_margin": option.get("national_margin"),
        "advance_margin": option.get("advance_margin"),
        "national_gap_score": option.get("national_gap_score"),
        "advance_gap_score": option.get("advance_gap_score"),
        "national_probability": option["national_probability"],
        "revival_probability": option["revival_probability"],
        "revival_only_probability": option["revival_only_probability"],
        "out_probability": option["out_probability"],
        "average_rank": option["average_rank"],
        "top8_probability": option["top8_probability"],
        "top4_probability": option["top4_probability"],
        "summary": (
            f"推荐报志愿 {option['volunteer']}。在当前实时填报快照下，"
            f"{target_school} 最终会落在 {option['final_region']}，当前预估结果为"
            f"{option['qualification_label']}。复活及以上概率 "
            f"{option['revival_probability'] * 100:.1f}%，"
            f"国赛概率 {option['national_probability'] * 100:.1f}%，"
            f"平均名次 {option['average_rank']:.2f}。"
        ),
    }


def _school_order(snapshot: dict, strength_info: Dict[str, dict]) -> List[str]:
    return sorted(
        snapshot["by_school"],
        key=lambda school: (
            A.REGIONS.index(snapshot["by_school"][school].assigned),
            snapshot["qualification_panel"]["strength_rank_region"].get(school, 10 ** 9),
            strength_info[school]["strength_rank_global"],
            school,
        ),
    )


def build_decision_panel(
    live_teams,
    distances,
    roster: Optional[Dict[str, dict]] = None,
    strength_info: Optional[Dict[str, dict]] = None,
    *,
    default_school: str = SHARK_SCHOOL,
) -> Optional[dict]:
    roster = roster or load_roster()
    if not roster:
        return None

    if strength_info is None:
        strength_info, _ = Q.compute_strength_table(roster)

    base_volunteers = _extract_live_volunteers(live_teams)
    snapshot = _current_snapshot_context(roster, distances, strength_info, base_volunteers)
    ordered_schools = _school_order(snapshot, strength_info)
    region_cache: Dict[Tuple[str, int, int, Tuple[str, ...]], dict] = {}
    schools: Dict[str, dict] = {}

    for target_school in ordered_schools:
        options = [
            _simulate_choice(
                target_school,
                roster,
                distances,
                strength_info,
                base_volunteers,
                choice,
                region_cache,
            )
            for choice in REGIONS
        ]
        recommendation = _build_recommendation(
            target_school,
            select_recommendation_option(options),
        )
        schools[target_school] = {
            "target": _build_target_profile(target_school, roster, strength_info),
            "same_city_rival": _same_city_rival(
                target_school,
                roster,
                base_volunteers,
                snapshot,
            ),
            "observed_state": _observed_state(target_school, base_volunteers, snapshot),
            "options": options,
            "recommendation": recommendation,
        }

    actual_default = default_school if default_school in schools else ordered_schools[0]
    return {
        "default_school": actual_default,
        "school_count": len(schools),
        "model_note": (
            "当前不做多情景压力测试，只基于当前实时填报快照推演。"
            "每个志愿选项都会先按真实调剂落位，再只对目标学校最终所在赛区"
            f"进行 {S.DEFAULT_ITERATIONS} 次瑞士轮概率模拟；最终推荐按总晋级率优先。"
        ),
        "model_weights": dict(MODEL_PRIORITIES),
        "schools": schools,
    }
