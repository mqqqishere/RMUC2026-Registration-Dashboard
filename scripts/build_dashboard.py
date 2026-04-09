#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建 RMUC 2026 晋级推演台所需的 docs/data.json 与 docs/data.csv。

数据流：
    qingflow 实时志愿 ─┐
                      ├─▶ qualification.build_projected_teams ─▶ allocator.allocate
    96 队完整 roster ─┘
                                            │
                                            ├─▶ 国赛/复活赛推演
                                            └─▶ 综合实力榜
"""

from __future__ import annotations

import csv
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import allocator         # noqa: E402
import qualification     # noqa: E402

BOARD_URL = "https://qingflow.com/appView/e3bol1op1c02/shareView/e3bol20d1c02"
ANNOUNCEMENT_URL = (
    "https://www.robomaster.com/zh-CN/resource/pages/announcement/1910"
)
OUT_PATH = os.path.join(ROOT, "docs", "data.json")
CSV_PATH = os.path.join(ROOT, "docs", "data.csv")

STATUS_CN = {"host": "承办", "volunteer": "志愿", "transfer": "调剂"}
REGION_ORDER = {"南部": 0, "东部": 1, "北部": 2}


def _now():
    cst = timezone(timedelta(hours=8))
    utc = datetime.now(timezone.utc)
    return utc, utc.astimezone(cst)


def _base_payload(fetch_error: str | None = None) -> dict:
    utc, cst = _now()
    return {
        "updated_at_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_cst": cst.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "board": BOARD_URL,
            "announcement": ANNOUNCEMENT_URL,
        },
        "title": "RMUC 2026 晋级推演台",
        "capacity": allocator.REGION_CAPACITY,
        "total_teams": 0,
        "submitted_teams": 0,
        "ranks_covered": 0,
        "hosts_seen": 0,
        "transferred_count": 0,
        "adjustment_order": [],
        "submission": {
            "submitted_count": 0,
            "roster_total": 0,
            "estimated_count": 0,
        },
        "refresh_meta": {
            "status": "idle",
            "status_label": "尚未刷新",
            "message": "当前页面显示的是本地已生成数据。",
            "source": "local_cache",
            "live_submitted_count": 0,
        },
        "regions": [],
        "teams": [],
        "strength_ranking": [],
        "advancement_panel": {
            "region_summaries": [],
            "region_strength_stats": {region: {} for region in allocator.REGIONS},
            "national_by_region": {region: 0 for region in allocator.REGIONS},
            "revival_by_region": {region: 0 for region in allocator.REGIONS},
            "floating_by_region": {region: 0 for region in allocator.REGIONS},
            "top16_by_region": {region: 0 for region in allocator.REGIONS},
            "boundary_teams_by_region": {region: {} for region in allocator.REGIONS},
            "model_note": (
                "全国赛名额按公告精确计算；复活赛名额为基于公告公开因素和当前综合实力模型的推演结果。"
            ),
        },
        "fetch_error": fetch_error,
    }


def _member_to_dict(team, region: str, roster_row: dict, strength_entry: dict,
                    qualification_status: str, volunteer_source: str,
                    markers: List[str]):
    return {
        "school": team.school,
        "team": roster_row["team"],
        "city": team.city,
        "status": team.status,
        "status_cn": STATUS_CN.get(team.status, team.status or ""),
        "original_volunteer": team.volunteer,
        "assigned": team.assigned,
        "dist_km": team.dist[region],
        "rank": team.rank,
        "points": roster_row["points"],
        "team_type": roster_row["team_type"],
        "is_host": team.is_host,
        "full_form_ranking": roster_row["full_form_ranking"],
        "detail_2025": roster_row["detail_2025"],
        "detail_2025_cn": roster_row["detail_2025_cn"],
        "rmul_2026_top4": roster_row["rmul_2026_top4"],
        "rmul_2026_top4_cn": roster_row["rmul_2026_top4_cn"],
        "strength_score": strength_entry["strength_score"],
        "strength_components": strength_entry["strength_components"],
        "strength_rank_global": strength_entry["strength_rank_global"],
        "strength_rank_region": strength_entry["strength_rank_region"],
        "qualification_status": qualification_status,
        "qualification_label": qualification.qualification_label(qualification_status),
        "volunteer_source": volunteer_source,
        "volunteer_source_label": qualification.volunteer_source_label(volunteer_source),
        "is_submitted": volunteer_source == "submitted",
        "boundary_markers": markers,
        "boundary_labels": qualification.boundary_labels(markers),
    }


def _team_to_flat(team, region: str, roster_row: dict, strength_entry: dict,
                  qualification_status: str, volunteer_source: str,
                  markers: List[str], panel: dict):
    return {
        "school": team.school,
        "team": roster_row["team"],
        "city": team.city,
        "team_type": roster_row["team_type"],
        "volunteer": team.volunteer,
        "assigned": region,
        "status": team.status,
        "status_cn": STATUS_CN.get(team.status, team.status or ""),
        "rank": team.rank,
        "points": roster_row["points"],
        "full_form_ranking": roster_row["full_form_ranking"],
        "detail_2025": roster_row["detail_2025"],
        "detail_2025_cn": roster_row["detail_2025_cn"],
        "rmul_2026_top4": roster_row["rmul_2026_top4"],
        "rmul_2026_top4_cn": roster_row["rmul_2026_top4_cn"],
        "is_host": team.is_host,
        "dist_south": team.dist.get("南部"),
        "dist_east": team.dist.get("东部"),
        "dist_north": team.dist.get("北部"),
        "dist_to_assigned": team.dist.get(region),
        "strength_score": strength_entry["strength_score"],
        "strength_components": strength_entry["strength_components"],
        "strength_rank_global": strength_entry["strength_rank_global"],
        "strength_rank_region": strength_entry["strength_rank_region"],
        "qualification_status": qualification_status,
        "qualification_label": qualification.qualification_label(qualification_status),
        "volunteer_source": volunteer_source,
        "volunteer_source_label": qualification.volunteer_source_label(volunteer_source),
        "is_submitted": volunteer_source == "submitted",
        "boundary_markers": markers,
        "boundary_labels": qualification.boundary_labels(markers),
        "national_spots_region": panel["national_by_region"][region],
        "revival_spots_region": panel["revival_by_region"][region],
    }


def _write_csv(flat_teams: List[dict], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    headers = [
        ("综合实力总排", "strength_rank_global"),
        ("学校", "school"),
        ("队名", "team"),
        ("城市", "city"),
        ("队伍类型", "team_type"),
        ("原志愿赛区", "volunteer"),
        ("录取赛区", "assigned"),
        ("录取状态", "status_cn"),
        ("资格状态", "qualification_label"),
        ("志愿来源", "volunteer_source_label"),
        ("综合实力分", "strength_score"),
        ("赛区内实力排名", "strength_rank_region"),
        ("积分榜排名", "rank"),
        ("积分", "points"),
        ("完整形态排名", "full_form_ranking"),
        ("2025 战绩", "detail_2025_cn"),
        ("2026 RMUL", "rmul_2026_top4_cn"),
        ("赛区国赛名额", "national_spots_region"),
        ("赛区复活赛名额", "revival_spots_region"),
        ("距录取赛区(km)", "dist_to_assigned"),
        ("距南部(km)", "dist_south"),
        ("距东部(km)", "dist_east"),
        ("距北部(km)", "dist_north"),
        ("边界标签", "boundary_labels_text"),
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([label for label, _ in headers])
        rows = sorted(
            flat_teams,
            key=lambda t: (
                t["strength_rank_global"],
                REGION_ORDER.get(t["assigned"], 9),
                t["school"],
            ),
        )
        for row in rows:
            row = dict(row)
            row["boundary_labels_text"] = " / ".join(row.get("boundary_labels", []))
            writer.writerow([
                "" if row.get(key) is None else row.get(key)
                for _, key in headers
            ])


def write_outputs(payload: dict, out_path: str = OUT_PATH, csv_path: str = CSV_PATH):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _write_csv(payload["teams"], csv_path)


def build_payload() -> dict:
    distances = allocator.load_distances()
    roster = qualification.load_roster()

    try:
        live_teams = allocator.load_teams_live(
            BOARD_URL, distances, ranks={}, verbose=False
        )
        fetch_error = None
    except Exception as exc:
        traceback.print_exc()
        payload = _base_payload(fetch_error=f"{type(exc).__name__}: {exc}")
        payload["submission"]["roster_total"] = len(roster)
        payload["total_teams"] = len(roster)
        payload["hosts_seen"] = sum(
            1 for school in roster if allocator.is_protected_host(school)
        )
        payload["ranks_covered"] = sum(
            1 for row in roster.values() if row["points_rank"] is not None
        )
        payload["refresh_meta"] = {
            "status": "error",
            "status_label": "刷新失败",
            "message": f"轻流抓取失败：{type(exc).__name__}: {exc}",
            "source": "qingflow_live",
            "live_submitted_count": 0,
        }
        return payload

    live_volunteers = qualification.extract_live_volunteers(live_teams)
    projected_teams, volunteer_source = qualification.build_projected_teams(
        roster, distances, live_volunteers
    )
    result = allocator.allocate(projected_teams, verbose=False)
    strength_info, ranking = qualification.compute_strength_table(roster)
    panel = qualification.assign_qualifications(result, strength_info, roster)

    payload = _base_payload()
    payload["total_teams"] = len(projected_teams)
    payload["submitted_teams"] = len(live_volunteers)
    payload["ranks_covered"] = sum(
        1 for row in roster.values() if row["points_rank"] is not None
    )
    payload["hosts_seen"] = sum(1 for team in projected_teams if team.is_host)
    payload["transferred_count"] = sum(
        1 for team in result.teams if team.status == "transfer"
    )
    payload["adjustment_order"] = result.order
    payload["submission"] = {
        "submitted_count": len(live_volunteers),
        "roster_total": len(projected_teams),
        "estimated_count": len(projected_teams) - len(live_volunteers),
    }
    payload["refresh_meta"] = {
        "status": "success",
        "status_label": "实时抓取成功",
        "message": f"本页展示的是 {payload['updated_at_cst']} 的最新轻流刷新结果，当前抓到 {len(live_volunteers)} 支已填报学校。",
        "source": "qingflow_live",
        "live_submitted_count": len(live_volunteers),
    }

    flat_teams = []
    region_outputs = []
    by_school_final = {team.school: team for team in result.teams}

    for region in allocator.REGIONS:
        members = panel["region_rankings"][region]
        counts = {"host": 0, "volunteer": 0, "transfer": 0}
        visible_member_dicts = []
        hidden_member_dicts = []
        for team in members:
            counts[team.status] += 1
            roster_row = roster[team.school]
            strength_entry = dict(strength_info[team.school])
            strength_entry["strength_rank_region"] = panel["strength_rank_region"][team.school]
            qualification_status = panel["qualification_status"][team.school]
            markers = panel["boundary_markers"].get(team.school, [])
            member = _member_to_dict(
                team,
                region,
                roster_row,
                strength_entry,
                qualification_status,
                volunteer_source[team.school],
                markers,
            )
            if member["is_submitted"]:
                visible_member_dicts.append(member)
            else:
                hidden_member_dicts.append(member)

        region_outputs.append({
            "key": region,
            "name": f"{region}赛区",
            "city": allocator.REGION_CITY[region],
            "count": len(members),
            "visible_count": len(visible_member_dicts),
            "hidden_count": len(hidden_member_dicts),
            "counts": counts,
            "national_spots": panel["national_by_region"][region],
            "revival_spots": panel["revival_by_region"][region],
            "top16_count": panel["top16_by_region"][region],
            "floating_spots": panel["floating_by_region"][region],
            "advancing_total": (
                panel["national_by_region"][region] + panel["revival_by_region"][region]
            ),
            "boundary_teams": panel["boundary_teams_by_region"][region],
            "strength_stats": panel["region_strength_stats"][region],
            "members": visible_member_dicts,
            "hidden_members": hidden_member_dicts,
        })

    for item in ranking:
        team = by_school_final[item["school"]]
        roster_row = roster[item["school"]]
        strength_entry = dict(item)
        strength_entry["strength_rank_region"] = panel["strength_rank_region"][item["school"]]
        qualification_status = panel["qualification_status"][item["school"]]
        markers = panel["boundary_markers"].get(item["school"], [])
        flat = _team_to_flat(
            team,
            team.assigned,
            roster_row,
            strength_entry,
            qualification_status,
            volunteer_source[item["school"]],
            markers,
            panel,
        )
        flat_teams.append(flat)

    payload["regions"] = region_outputs
    payload["teams"] = flat_teams
    payload["strength_ranking"] = flat_teams
    payload["advancement_panel"] = {
        "region_summaries": panel["region_summaries"],
        "region_strength_stats": panel["region_strength_stats"],
        "national_by_region": panel["national_by_region"],
        "revival_by_region": panel["revival_by_region"],
        "floating_by_region": panel["floating_by_region"],
        "top16_by_region": panel["top16_by_region"],
        "boundary_teams_by_region": panel["boundary_teams_by_region"],
        "model_note": (
            "全国赛名额按公告精确计算；复活赛名额为基于全国赛名额、上赛季成绩、RMU 积分榜和当前综合实力模型的推演结果。未提交志愿的学校会按当前预测结果显示在主看板中；赛区强度面板按当前录取结果统计均分、中位数、标准差与头部均分。"
        ),
    }
    return payload


def main():
    payload = build_payload()
    if payload.get("fetch_error"):
        print(f"ERROR: fetch_error = {payload['fetch_error']}", file=sys.stderr)
        sys.exit(2)

    write_outputs(payload)

    print(f"wrote {OUT_PATH}")
    print(f"wrote {CSV_PATH}")
    print(
        f"  roster={payload['submission']['roster_total']}  "
        f"submitted={payload['submission']['submitted_count']}  "
        f"estimated={payload['submission']['estimated_count']}  "
        f"transfer={payload['transferred_count']}  "
        f"updated={payload['updated_at_cst']}"
    )
    for region in payload["regions"]:
        print(
            f"  {region['key']}: {region['count']:>2}/{payload['capacity']}  "
            f"国赛 {region['national_spots']}  "
            f"复活 {region['revival_spots']}  "
            f"调入 {region['counts']['transfer']}"
        )


if __name__ == "__main__":
    main()
