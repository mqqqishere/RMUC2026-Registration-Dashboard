#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建 docs/data.json 供静态看板页面读取。

数据流：
    qingflow shareView  ──(qingflow.fetch_view)──▶  raw 志愿记录
                                                        │
    distances.csv + 承办院校硬编码 ──▶ allocator.allocate  │
                                                        ▼
                                               docs/data.json

该脚本无参数；仓库根目录执行：
    python scripts/build_dashboard.py
"""

from __future__ import annotations

import csv
import json
import os
import sys
import traceback
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import allocator        # noqa: E402
import qingflow         # noqa: E402
import shark_decision   # noqa: E402

BOARD_URL = "https://qingflow.com/appView/e3bol1op1c02/shareView/e3bol20d1c02"
ANNOUNCEMENT_URL = (
    "https://www.robomaster.com/zh-CN/resource/pages/announcement/1910"
)
OUT_PATH = os.path.join(ROOT, "docs", "data.json")
CSV_PATH = os.path.join(ROOT, "docs", "data.csv")
RANKS_PATH = os.path.join(ROOT, "ranks.csv")   # 可选

STATUS_CN = {"host": "承办", "volunteer": "志愿", "transfer": "调剂"}
REGION_ORDER = {"南部": 0, "东部": 1, "北部": 2}


def _now():
    cst = timezone(timedelta(hours=8))
    utc = datetime.now(timezone.utc)
    return utc, utc.astimezone(cst)


def _empty_regions():
    return [{
        "key":      r,
        "name":     f"{r}赛区",
        "city":     allocator.REGION_CITY[r],
        "count":    0,
        "counts":   {"host": 0, "volunteer": 0, "transfer": 0},
        "members":  [],
    } for r in allocator.REGIONS]


def _member_to_dict(t, region):
    return {
        "school":             t.school,
        "city":               t.city,
        "status":             t.status,            # host / volunteer / transfer
        "original_volunteer": t.volunteer,
        "dist_km":            t.dist[region],
        "rank":               t.rank,
        "is_host":            t.is_host,
    }


def _team_to_flat(t):
    """扁平化 Team，用于 data.json 的 teams 数组和 data.csv。

    保留每支队伍到三个赛区的距离，方便下游脚本做二次分析。
    """
    return {
        "school":     t.school,
        "city":       t.city,
        "volunteer":  t.volunteer,
        "assigned":   t.assigned,
        "status":     t.status,
        "rank":       t.rank,
        "is_host":    t.is_host,
        "dist_south": t.dist.get("南部"),
        "dist_east":  t.dist.get("东部"),
        "dist_north": t.dist.get("北部"),
    }


def _write_csv(flat_teams, path):
    """写 UTF-8 BOM CSV，Excel/Numbers 直接双击可识别中文表头。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    headers = [
        ("学校",         "school"),
        ("城市",         "city"),
        ("原志愿赛区",    "volunteer"),
        ("录取赛区",      "assigned"),
        ("状态",         "status"),
        ("积分榜排名",    "rank"),
        ("距南部(km)",   "dist_south"),
        ("距东部(km)",   "dist_east"),
        ("距北部(km)",   "dist_north"),
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([h for h, _ in headers])
        rows = sorted(
            flat_teams,
            key=lambda t: (REGION_ORDER.get(t.get("assigned"), 9), t["school"]),
        )
        for t in rows:
            out = []
            for _, k in headers:
                v = t.get(k)
                if k == "status":
                    v = STATUS_CN.get(v, v or "")
                out.append("" if v is None else v)
            w.writerow(out)


def build_payload():
    distances = allocator.load_distances()
    ranks = allocator.load_ranks(RANKS_PATH) if os.path.exists(RANKS_PATH) else {}

    try:
        teams = allocator.load_teams_live(
            BOARD_URL, distances, ranks, verbose=False)
        fetch_error = None
    except Exception as e:
        teams = []
        fetch_error = f"{type(e).__name__}: {e}"
        traceback.print_exc()

    utc, cst = _now()
    payload = {
        "updated_at_utc":    utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "updated_at_cst":    cst.strftime("%Y-%m-%d %H:%M:%S"),
        "source": {
            "board":        BOARD_URL,
            "announcement": ANNOUNCEMENT_URL,
        },
        "capacity":          allocator.REGION_CAPACITY,
        "total_teams":       len(teams),
        "ranks_covered":     sum(1 for t in teams if t.rank is not None),
        "hosts_seen":        sum(1 for t in teams if t.is_host),
        "transferred_count": 0,
        "adjustment_order":  [],
        "regions":           _empty_regions(),
        "teams":             [],
        "fetch_error":       fetch_error,
    }

    # SHARK 决策面板始终计算(即使 live 队伍为空,也能用 roster 做预测)
    try:
        sd = shark_decision.build_decision_panel(teams, distances, ranks)
        if sd is not None:
            payload["shark_decision"] = sd
    except Exception as e:
        traceback.print_exc()
        payload["shark_decision_error"] = f"{type(e).__name__}: {e}"

    if not teams:
        return payload

    result = allocator.allocate(teams, verbose=False)
    payload["adjustment_order"] = result.order
    payload["transferred_count"] = sum(
        1 for t in result.teams if t.status == "transfer")
    payload["teams"] = [_team_to_flat(t) for t in result.teams]

    regions_out = []
    for r in allocator.REGIONS:
        members = result.regions.get(r, [])
        counts = {"host": 0, "volunteer": 0, "transfer": 0}
        for t in members:
            counts[t.status] += 1
        members_sorted = sorted(
            members,
            key=lambda t: (
                t.status != "host",
                t.status != "volunteer",
                t.dist[r],
                allocator._rank_key(t),
            ),
        )
        regions_out.append({
            "key":     r,
            "name":    f"{r}赛区",
            "city":    allocator.REGION_CITY[r],
            "count":   len(members),
            "counts":  counts,
            "members": [_member_to_dict(t, r) for t in members_sorted],
        })
    payload["regions"] = regions_out
    return payload


def main():
    payload = build_payload()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _write_csv(payload["teams"], CSV_PATH)

    print(f"wrote {OUT_PATH}")
    print(f"wrote {CSV_PATH}")
    print(f"  total={payload['total_teams']}  "
          f"transfer={payload['transferred_count']}  "
          f"updated={payload['updated_at_cst']}")
    for r in payload["regions"]:
        c = r["counts"]
        print(f"  {r['key']}: {r['count']:>2}/{payload['capacity']}  "
              f"(host {c['host']}, volunteer {c['volunteer']}, "
              f"transfer {c['transfer']})")

    if payload.get("fetch_error"):
        # 让 CI 失败，避免把 docs/data.json 写成空状态后 push
        print(f"ERROR: fetch_error = {payload['fetch_error']}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
