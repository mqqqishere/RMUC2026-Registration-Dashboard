#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RMUC 2026 晋级推演模型。

负责：
1. 加载 96 支队伍元数据
2. 把轻流实时志愿覆盖到完整 roster 上
3. 计算综合实力榜
4. 依据公告规则计算全国赛名额，并基于实力模型推演复活赛名额
"""

from __future__ import annotations

import csv
import io
import os
from collections import defaultdict
from statistics import median, pstdev
from typing import Dict, Iterable, List, Optional, Tuple

import allocator as A

HERE = os.path.dirname(os.path.abspath(__file__))
ROSTER_PATH = os.path.join(HERE, "robomaster_2026_teams.csv")
RANKS_PATH = os.path.join(HERE, "ranks.csv")

REGIONS = A.REGIONS

TOP16_DETAILS = {
    "champion",
    "runner_up",
    "third_place",
    "fourth_place",
    "quarter_finalist",
    "top_16",
}

DETAIL_CN = {
    "champion": "冠军",
    "runner_up": "亚军",
    "third_place": "季军",
    "fourth_place": "殿军",
    "quarter_finalist": "八强",
    "top_16": "十六强",
    "top_32": "三十二强",
    "revival": "复活赛",
    "division": "分区赛",
    "not_participated": "未参赛",
    "": "—",
}

RMUL_CN = {
    "champion": "冠军",
    "runner_up": "亚军",
    "third_place": "季军",
    "fourth_place": "殿军",
    "": "—",
}

RMUL_BONUS = {
    "champion": 17.0,
    "runner_up": 15.5,
    "third_place": 14.0,
    "fourth_place": 12.5,
}

HISTORY_BONUS = {
    "champion": 40.0,
    "runner_up": 36.0,
    "third_place": 33.0,
    "fourth_place": 30.0,
    "quarter_finalist": 25.0,
    "top_16": 20.0,
    "top_32": 16.0,
    "revival": 8.0,
}

QUALIFICATION_LABEL = {
    "national": "国赛",
    "revival": "复活赛",
    "none": "未晋级",
}

VOLUNTEER_SOURCE_LABEL = {
    "submitted": "实时提交",
    "nearest_estimate": "就近估算",
    "host_default": "承办默认",
}

BOUNDARY_LABEL = {
    "last_national": "国赛线",
    "first_revival": "复活起点",
    "last_revival": "复活线",
    "first_out": "落选线",
}

REVIVAL_TOTAL = 16
REVIVAL_SOFT_MAX_ADVANCING_PER_REGION = 15
REVIVAL_REGION_WEIGHTS = {
    "avg": 2.4,
    "median": 1.6,
    "top8": 1.2,
    "stability": 0.8,
    "balance": 1.3,
    "saturation": 1.1,
}


def _int_or_none(v: Optional[str]) -> Optional[int]:
    if v in (None, ""):
        return None
    return int(v)


def _float_or_none(v: Optional[str]) -> Optional[float]:
    if v in (None, ""):
        return None
    return float(v)


def _normalize_school_name(name: str) -> str:
    return (
        (name or "")
        .strip()
        .replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
    )


def _round(v: float) -> float:
    return round(v, 4)


def _load_rank_scores(path: str = RANKS_PATH) -> Tuple[Dict[str, float], Dict[int, List[float]]]:
    by_school: Dict[str, float] = {}
    by_rank: Dict[int, List[float]] = defaultdict(list)
    if not os.path.exists(path):
        return by_school, by_rank

    with open(path, encoding="utf-8-sig", newline="") as f:
        for raw in csv.DictReader(f):
            school = _normalize_school_name(raw.get("school") or "")
            rank = _int_or_none(raw.get("rank"))
            score = _float_or_none(raw.get("score"))
            if score is None:
                continue
            if school:
                by_school[school] = score
            if rank is not None:
                by_rank[rank].append(score)
    return by_school, by_rank


def _estimate_points_from_rank(points_rank: Optional[int], scores_by_rank: Dict[int, List[float]]) -> Optional[float]:
    if points_rank is None or not scores_by_rank:
        return None
    if points_rank in scores_by_rank:
        values = scores_by_rank[points_rank]
        return sum(values) / len(values)

    lower = [rank for rank in scores_by_rank if rank < points_rank]
    upper = [rank for rank in scores_by_rank if rank > points_rank]
    lower_rank = max(lower) if lower else None
    upper_rank = min(upper) if upper else None

    if lower_rank is None and upper_rank is None:
        return None
    if lower_rank is None:
        values = scores_by_rank[upper_rank]
        return sum(values) / len(values)
    if upper_rank is None:
        values = scores_by_rank[lower_rank]
        return sum(values) / len(values)

    low_score = sum(scores_by_rank[lower_rank]) / len(scores_by_rank[lower_rank])
    high_score = sum(scores_by_rank[upper_rank]) / len(scores_by_rank[upper_rank])
    span = upper_rank - lower_rank
    if span <= 0:
        return low_score
    weight = (points_rank - lower_rank) / span
    return low_score + (high_score - low_score) * weight


def _backfill_missing_points(roster: Dict[str, dict]):
    school_scores, rank_scores = _load_rank_scores()
    for row in roster.values():
        if row["points"] is not None or row["points_rank"] is None:
            continue
        school_key = _normalize_school_name(row["school"])
        if school_key in school_scores:
            row["points"] = school_scores[school_key]
            continue
        estimated = _estimate_points_from_rank(row["points_rank"], rank_scores)
        if estimated is not None:
            row["points"] = round(estimated, 3)


def _strength_sort_key(team: A.Team, strength_info: Dict[str, dict]):
    entry = strength_info[team.school]
    return (
        -entry["strength_score"],
        entry["full_form_ranking"] if entry["full_form_ranking"] is not None else 10 ** 9,
        entry["points_rank"] if entry["points_rank"] is not None else 10 ** 9,
        team.school,
    )


def _centered_metric(
    values_by_region: Dict[str, float],
    *,
    higher_better: bool = True,
) -> Dict[str, float]:
    values = list(values_by_region.values())
    low = min(values) if values else 0.0
    high = max(values) if values else 0.0
    if high <= low:
        return {region: 0.0 for region in values_by_region}

    out = {}
    for region, value in values_by_region.items():
        scaled = (value - low) / (high - low)
        if not higher_better:
            scaled = 1.0 - scaled
        out[region] = _round((scaled - 0.5) * 2.0)
    return out


def load_roster(path: str = ROSTER_PATH) -> Dict[str, dict]:
    """加载 96 支队伍元数据，跳过注释行。"""
    with open(path, encoding="utf-8-sig") as f:
        lines = [
            ln for ln in f
            if not ln.lstrip().startswith("#") and ln.strip()
        ]

    roster: Dict[str, dict] = {}
    for raw in csv.DictReader(io.StringIO("".join(lines))):
        school = raw["school"].strip()
        row = {
            "school": school,
            "team": raw["team"].strip(),
            "team_type": raw["team_type"].strip(),
            "city": raw["city"].strip(),
            "distance_to_changsha": int(raw["distance_to_changsha"]),
            "distance_to_jinan": int(raw["distance_to_jinan"]),
            "distance_to_shenyang": int(raw["distance_to_shenyang"]),
            "points_rank": _int_or_none(raw.get("points_rank")),
            "points": _float_or_none(raw.get("points")),
            "participated_2025": raw.get("participated_2025") == "True",
            "category_2025": (raw.get("category_2025") or "").strip(),
            "detail_2025": (raw.get("detail_2025") or "").strip(),
            "detail_2025_cn": DETAIL_CN.get((raw.get("detail_2025") or "").strip(), "—"),
            "full_form_ranking": _int_or_none(raw.get("full_form_ranking")),
            "rmul_2026_top4": (raw.get("rmul_2026_top4") or "").strip(),
            "rmul_2026_top4_cn": RMUL_CN.get((raw.get("rmul_2026_top4") or "").strip(), "—"),
        }
        roster[school] = row
    _backfill_missing_points(roster)
    return roster


def nearest_region_for_row(row: dict) -> str:
    distances = {
        "南部": row["distance_to_changsha"],
        "东部": row["distance_to_jinan"],
        "北部": row["distance_to_shenyang"],
    }
    return min(distances, key=distances.get)


def extract_live_volunteers(live_teams: Optional[Iterable[A.Team]]) -> Dict[str, str]:
    if not live_teams:
        return {}
    return {t.school: t.volunteer for t in live_teams}


def build_projected_teams(
    roster: Dict[str, dict],
    distances: Dict[str, A.SchoolInfo],
    live_volunteers: Optional[Dict[str, str]] = None,
) -> Tuple[List[A.Team], Dict[str, str]]:
    """
    用实时志愿覆盖完整 roster，得到完整 96 支队伍的预测输入。

    志愿优先级：
    1. 实时提交
    2. RMUC 承办默认赛区
    3. 就近估算
    """
    live = live_volunteers or {}
    teams: List[A.Team] = []
    volunteer_source: Dict[str, str] = {}

    for school, row in roster.items():
        if school in live:
            volunteer = live[school]
            source = "submitted"
        elif school in A.RMUC_HOSTS:
            volunteer = A.RMUC_HOSTS[school]
            source = "host_default"
        else:
            volunteer = nearest_region_for_row(row)
            source = "nearest_estimate"

        team = A._build_team(school, volunteer, row["points_rank"], distances)
        teams.append(team)
        volunteer_source[school] = source

    return teams, volunteer_source


def compute_top16_counts(region_members: Dict[str, List[A.Team]],
                         roster: Dict[str, dict]) -> Dict[str, int]:
    return {
        region: sum(
            1 for team in region_members.get(region, [])
            if roster[team.school]["detail_2025"] in TOP16_DETAILS
        )
        for region in REGIONS
    }


def compute_national_spots(region_members: Dict[str, List[A.Team]],
                           roster: Dict[str, dict]) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, int]]:
    """
    按公告 1910 精确计算全国赛名额。

    返回：
        top16_count, floating_spots, national_spots
    """
    top16_count = compute_top16_counts(region_members, roster)
    eligible = {r: c for r, c in top16_count.items() if c > 4}

    floating_spots = {r: 0 for r in REGIONS}
    if eligible:
        total = sum(eligible.values())
        exact = {r: eligible[r] / total * 4 for r in eligible}
        floor = {r: int(exact[r]) for r in eligible}
        frac = {r: exact[r] - floor[r] for r in eligible}
        remaining = 4 - sum(floor.values())
        order = sorted(eligible, key=lambda r: (-frac[r], REGIONS.index(r)))
        for region in order[:remaining]:
            floor[region] += 1
        for region, value in floor.items():
            floating_spots[region] = value

    national_spots = {r: 8 + floating_spots[r] for r in REGIONS}
    return top16_count, floating_spots, national_spots


def _full_form_component(rank: Optional[int]) -> float:
    if rank is None:
        return 0.0
    normalized = max(0.0, (96 - rank) / 95)
    return 28.0 * (normalized ** 1.8)


def _points_component(points: Optional[float], points_rank: Optional[int],
                      max_points: float, min_rank: int, max_rank: int) -> float:
    if points is not None and max_points > 0:
        return 9.0 * (points / max_points)
    if points_rank is None or max_rank <= min_rank:
        return 0.0
    normalized = (max_rank - points_rank) / (max_rank - min_rank)
    return max(0.0, 9.0 * normalized)


def _percentile(sorted_values: List[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * ratio
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = pos - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def compute_strength_table(roster: Dict[str, dict]) -> Tuple[Dict[str, dict], List[dict]]:
    """按固定权重计算综合实力分，并返回全局排序。"""
    points_values = [row["points"] for row in roster.values() if row["points"] is not None]
    rank_values = [row["points_rank"] for row in roster.values() if row["points_rank"] is not None]
    max_points = max(points_values) if points_values else 0.0
    min_rank = min(rank_values) if rank_values else 1
    max_rank = max(rank_values) if rank_values else 1

    strength_info: Dict[str, dict] = {}
    for school, row in roster.items():
        rmul_bonus = RMUL_BONUS.get(row["rmul_2026_top4"], 0.0)
        full_form_score = _full_form_component(row["full_form_ranking"])
        history_bonus = HISTORY_BONUS.get(row["detail_2025"], 0.0)
        points_score = _points_component(
            row["points"], row["points_rank"], max_points, min_rank, max_rank
        )
        total = rmul_bonus + full_form_score + history_bonus + points_score

        strength_info[school] = {
            "school": school,
            "team": row["team"],
            "city": row["city"],
            "team_type": row["team_type"],
            "points": row["points"],
            "points_rank": row["points_rank"],
            "full_form_ranking": row["full_form_ranking"],
            "detail_2025": row["detail_2025"],
            "detail_2025_cn": row["detail_2025_cn"],
            "rmul_2026_top4": row["rmul_2026_top4"],
            "rmul_2026_top4_cn": row["rmul_2026_top4_cn"],
            "strength_components": {
                "rmul_bonus": _round(rmul_bonus),
                "full_form_score": _round(full_form_score),
                "history_bonus": _round(history_bonus),
                "points_score": _round(points_score),
            },
            "strength_score": _round(total),
        }

    ranking = sorted(
        strength_info.values(),
        key=lambda item: (
            -item["strength_score"],
            item["full_form_ranking"] if item["full_form_ranking"] is not None else 10 ** 9,
            item["points_rank"] if item["points_rank"] is not None else 10 ** 9,
            item["school"],
        ),
    )
    for idx, item in enumerate(ranking, start=1):
        strength_info[item["school"]]["strength_rank_global"] = idx
    return strength_info, ranking


def compute_region_strength_stats(
    region_rankings: Dict[str, List[A.Team]],
    strength_info: Dict[str, dict],
) -> Dict[str, dict]:
    stats_by_region: Dict[str, dict] = {}

    for region in REGIONS:
        members = region_rankings.get(region, [])
        scores = [float(strength_info[team.school]["strength_score"]) for team in members]
        sorted_scores = sorted(scores)
        top_scores = sorted(scores, reverse=True)[:8]

        if scores:
            q1 = _percentile(sorted_scores, 0.25)
            q3 = _percentile(sorted_scores, 0.75)
            stats_by_region[region] = {
                "team_count": len(scores),
                "avg_strength_score": _round(sum(scores) / len(scores)),
                "median_strength_score": _round(float(median(scores))),
                "stdev_strength_score": _round(float(pstdev(scores))) if len(scores) > 1 else 0.0,
                "top8_avg_strength_score": _round(sum(top_scores) / len(top_scores)),
                "min_strength_score": _round(min(scores)),
                "max_strength_score": _round(max(scores)),
                "q1_strength_score": _round(q1),
                "q3_strength_score": _round(q3),
                "iqr_strength_score": _round(q3 - q1),
            }
        else:
            stats_by_region[region] = {
                "team_count": 0,
                "avg_strength_score": 0.0,
                "median_strength_score": 0.0,
                "stdev_strength_score": 0.0,
                "top8_avg_strength_score": 0.0,
                "min_strength_score": 0.0,
                "max_strength_score": 0.0,
                "q1_strength_score": 0.0,
                "q3_strength_score": 0.0,
                "iqr_strength_score": 0.0,
            }

    return stats_by_region


def _revival_region_context(
    region_rankings: Dict[str, List[A.Team]],
    strength_info: Dict[str, dict],
    national_by_region: Dict[str, int],
) -> Tuple[Dict[str, dict], Dict[str, float], Dict[str, int], float]:
    stats_by_region = compute_region_strength_stats(region_rankings, strength_info)
    avg_signal = _centered_metric(
        {region: stats["avg_strength_score"] for region, stats in stats_by_region.items()}
    )
    median_signal = _centered_metric(
        {region: stats["median_strength_score"] for region, stats in stats_by_region.items()}
    )
    top8_signal = _centered_metric(
        {region: stats["top8_avg_strength_score"] for region, stats in stats_by_region.items()}
    )
    stability_signal = _centered_metric(
        {region: stats["stdev_strength_score"] for region, stats in stats_by_region.items()},
        higher_better=False,
    )

    region_bonus = {}
    for region in REGIONS:
        region_bonus[region] = _round(
            avg_signal[region] * REVIVAL_REGION_WEIGHTS["avg"]
            + median_signal[region] * REVIVAL_REGION_WEIGHTS["median"]
            + top8_signal[region] * REVIVAL_REGION_WEIGHTS["top8"]
            + stability_signal[region] * REVIVAL_REGION_WEIGHTS["stability"]
        )

    soft_cap = {
        region: max(
            0,
            min(
                REVIVAL_TOTAL,
                REVIVAL_SOFT_MAX_ADVANCING_PER_REGION - national_by_region[region],
            ),
        )
        for region in REGIONS
    }
    target_total = (sum(national_by_region.values()) + REVIVAL_TOTAL) / len(REGIONS)
    return stats_by_region, region_bonus, soft_cap, target_total


def _revival_candidate_score(
    team: A.Team,
    strength_info: Dict[str, dict],
    region_bonus: Dict[str, float],
    target_total: float,
    advanced_count: Dict[str, int],
    revival_by_region: Dict[str, int],
) -> float:
    region = team.assigned
    balance_bonus = (
        target_total - advanced_count[region]
    ) * REVIVAL_REGION_WEIGHTS["balance"]
    saturation_penalty = (
        revival_by_region[region] * REVIVAL_REGION_WEIGHTS["saturation"]
    )
    return _round(
        strength_info[team.school]["strength_score"]
        + region_bonus[region]
        + balance_bonus
        - saturation_penalty
    )


def _select_revival_teams(
    region_rankings: Dict[str, List[A.Team]],
    strength_info: Dict[str, dict],
    national_by_region: Dict[str, int],
) -> Tuple[Dict[str, int], List[str], Dict[str, dict], Dict[str, float], Dict[str, int]]:
    stats_by_region, region_bonus, soft_cap, target_total = _revival_region_context(
        region_rankings, strength_info, national_by_region
    )

    remaining_pool = [
        team
        for region in REGIONS
        for team in region_rankings[region][national_by_region[region]:]
    ]
    revival_by_region = {region: 0 for region in REGIONS}
    advanced_count = dict(national_by_region)
    selected_schools: List[str] = []
    selected_school_set = set()

    while len(selected_schools) < REVIVAL_TOTAL:
        candidates = []
        for team in remaining_pool:
            if team.school in selected_school_set:
                continue
            region = team.assigned
            if advanced_count[region] >= 16:
                continue
            if revival_by_region[region] >= soft_cap[region]:
                continue
            candidates.append({
                "team": team,
                "score": _revival_candidate_score(
                    team,
                    strength_info,
                    region_bonus,
                    target_total,
                    advanced_count,
                    revival_by_region,
                ),
            })

        # 若软上限挡住了最后 1 个席位，则退回到绝对 16 上限。
        if not candidates:
            for team in remaining_pool:
                if team.school in selected_school_set:
                    continue
                region = team.assigned
                if advanced_count[region] >= 16:
                    continue
                candidates.append({
                    "team": team,
                    "score": _revival_candidate_score(
                        team,
                        strength_info,
                        region_bonus,
                        target_total,
                        advanced_count,
                        revival_by_region,
                    ),
                })

        if not candidates:
            break

        candidates.sort(
            key=lambda item: (
                -item["score"],
                *_strength_sort_key(item["team"], strength_info),
            )
        )
        best = candidates[0]["team"]
        region = best.assigned
        selected_school_set.add(best.school)
        selected_schools.append(best.school)
        revival_by_region[region] += 1
        advanced_count[region] += 1

    return revival_by_region, selected_schools, stats_by_region, region_bonus, soft_cap


def assign_qualifications(
    result: A.AllocationResult,
    strength_info: Dict[str, dict],
    roster: Dict[str, dict],
) -> dict:
    """
    计算全国赛/复活赛资格，并给出赛区内边界队伍。
    """
    top16_count, floating_spots, national_by_region = compute_national_spots(
        result.regions, roster
    )

    region_rankings: Dict[str, List[A.Team]] = {}
    strength_rank_region: Dict[str, int] = {}
    qualification_status: Dict[str, str] = {}
    boundary_markers: Dict[str, List[str]] = defaultdict(list)

    for region in REGIONS:
        members = sorted(
            result.regions.get(region, []),
            key=lambda team: _strength_sort_key(team, strength_info),
        )
        region_rankings[region] = members
        for idx, team in enumerate(members, start=1):
            strength_rank_region[team.school] = idx
            qualification_status[team.school] = "none"

    for region, members in region_rankings.items():
        national_cut = national_by_region[region]
        for team in members[:national_cut]:
            qualification_status[team.school] = "national"
    revival_by_region, selected_revival_schools, region_strength_stats, _, _ = _select_revival_teams(
        region_rankings,
        strength_info,
        national_by_region,
    )

    for school in selected_revival_schools:
        qualification_status[school] = "revival"

    boundary_teams_by_region = {}
    for region, members in region_rankings.items():
        national_cut = national_by_region[region]
        revival_cut = revival_by_region[region]
        boundary = {}

        if national_cut > 0:
            team = members[national_cut - 1]
            boundary_markers[team.school].append("last_national")
            boundary["last_national"] = team.school

        if revival_cut > 0:
            first_revival = members[national_cut]
            last_revival = members[national_cut + revival_cut - 1]
            boundary_markers[first_revival.school].append("first_revival")
            boundary_markers[last_revival.school].append("last_revival")
            boundary["first_revival"] = first_revival.school
            boundary["last_revival"] = last_revival.school

        if national_cut + revival_cut < len(members):
            team = members[national_cut + revival_cut]
            boundary_markers[team.school].append("first_out")
            boundary["first_out"] = team.school

        boundary_teams_by_region[region] = boundary

    region_summaries = []
    for region in REGIONS:
        region_summaries.append({
            "key": region,
            "name": f"{region}赛区",
            "city": A.REGION_CITY[region],
            "top16_count": top16_count[region],
            "floating_spots": floating_spots[region],
            "national_spots": national_by_region[region],
            "revival_spots": revival_by_region[region],
            "advancing_total": national_by_region[region] + revival_by_region[region],
            "boundary_teams": boundary_teams_by_region[region],
            "strength_stats": region_strength_stats[region],
        })

    return {
        "top16_by_region": top16_count,
        "floating_by_region": floating_spots,
        "national_by_region": national_by_region,
        "revival_by_region": revival_by_region,
        "boundary_teams_by_region": boundary_teams_by_region,
        "region_rankings": region_rankings,
        "region_strength_stats": region_strength_stats,
        "strength_rank_region": strength_rank_region,
        "qualification_status": qualification_status,
        "boundary_markers": dict(boundary_markers),
        "region_summaries": region_summaries,
    }


def qualification_label(status: str) -> str:
    return QUALIFICATION_LABEL.get(status, status)


def volunteer_source_label(source: str) -> str:
    return VOLUNTEER_SOURCE_LABEL.get(source, source)


def boundary_labels(markers: Iterable[str]) -> List[str]:
    return [BOUNDARY_LABEL[m] for m in markers]
