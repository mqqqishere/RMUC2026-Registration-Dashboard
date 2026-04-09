#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHARK 决策面板后端:对 江南大学霞客湾校区 的三种志愿选项做透明的反事实模拟。

核心输出（嵌入 docs/data.json 的 shark_decision 键）:

    target           SHARK 画像 (rank / ff / 距离 / 历史)
    same_city_rival  同城耦合对手 南理工江阴 的当前状态
    observed_state   当前清流看板已提交的初始志愿分布
    options          报志愿 南/东/北 三条路径的完整模拟结果 + 证据链
    sensitivity      5 个博弈情景 × 3 个志愿 的压力测试
    recommendation   基于 worst-case margin 选出的最佳志愿 + 理由

所有模拟都使用 allocator.allocate() 的真实调剂算法；未提交队伍回退到
"就近原则"占位，让面板在窗口期开头也能给出有意义的推荐。
"""

from __future__ import annotations

import csv
import io
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import allocator as A   # noqa: E402

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

SHARK_SCHOOL = "江南大学霞客湾校区"
SAME_CITY_RIVAL = "南京理工大学江阴校区"
ROSTER_PATH = os.path.join(ROOT, "robomaster_2026_teams.csv")

TOP16_DETAILS = {
    "champion", "runner_up", "third_place",
    "fourth_place", "quarter_finalist", "top_16",
}
TOP32_DETAILS = TOP16_DETAILS | {"top_32"}

STATUS_CN = {"host": "承办", "volunteer": "志愿", "transfer": "调剂"}
DETAIL_CN = {
    "champion":        "冠军",
    "runner_up":       "亚军",
    "third_place":     "季军",
    "fourth_place":    "殿军",
    "quarter_finalist": "八强",
    "top_16":          "十六强",
    "top_32":          "三十二强",
    "national":        "全国赛",
    "revival":         "复活赛",
    "division":        "分区赛",
    "not_participated": "未参赛",
    "":                "—",
}

REGIONS = A.REGIONS


# ──────────────────────────────────────────────────────────────────────────────
# 数据加载
# ──────────────────────────────────────────────────────────────────────────────

def load_roster() -> Dict[str, dict]:
    """加载 96 支队伍的元数据表。跳过 # 开头的注释行。"""
    if not os.path.exists(ROSTER_PATH):
        return {}
    with open(ROSTER_PATH, encoding="utf-8-sig") as f:
        lines = [
            ln for ln in f
            if not ln.lstrip().startswith("#") and ln.strip()
        ]
    return {row["school"]: row for row in csv.DictReader(io.StringIO("".join(lines)))}


def _nearest_region(row: dict) -> str:
    d = {
        "南部": int(row["distance_to_changsha"]),
        "东部": int(row["distance_to_jinan"]),
        "北部": int(row["distance_to_shenyang"]),
    }
    return min(d, key=d.get)


def _extract_live_volunteers(live_teams) -> Dict[str, str]:
    """从真实 qingflow 拉取的 Team 列表抽取 {school: volunteer}。"""
    return {t.school: t.volunteer for t in live_teams}


# ──────────────────────────────────────────────────────────────────────────────
# 规则实现:按公告第 2 条算名额
# ──────────────────────────────────────────────────────────────────────────────

def _compute_spots(region_members: Dict[str, list],
                   team_d25: Dict[str, str]):
    """按公告规则计算每个赛区的全国赛名额。

    返回 (top16_count, floating, national)。
    """
    t16 = {
        r: sum(1 for t in region_members[r]
               if team_d25.get(t.school) in TOP16_DETAILS)
        for r in REGIONS
    }
    # 只有 >4 的赛区参与浮动
    elig = {r: c for r, c in t16.items() if c > 4}
    floating = {r: 0 for r in REGIONS}
    if elig:
        total = sum(elig.values())
        exact = {r: c / total * 4 for r, c in elig.items()}
        floor = {r: int(exact[r]) for r in elig}
        frac = {r: exact[r] - floor[r] for r in elig}
        remaining = 4 - sum(floor.values())
        # 最大余数法:小数部分由大到小;并列取 REGIONS 顺序靠前者
        order = sorted(elig, key=lambda r: (-frac[r], REGIONS.index(r)))
        for r in order[:remaining]:
            floor[r] += 1
        for r, v in floor.items():
            floating[r] = v
    national = {r: 8 + floating[r] for r in REGIONS}
    return t16, floating, national


# ──────────────────────────────────────────────────────────────────────────────
# 情景构建 & 模拟
# ──────────────────────────────────────────────────────────────────────────────

def _build_teams_for_scenario(
    roster: Dict[str, dict],
    distances,
    base_volunteers: Dict[str, str],
    shark_override: Optional[str] = None,
    extra_overrides: Optional[Dict[str, str]] = None,
) -> list:
    """构造完整 96 队列表,按优先级决定每队的志愿。

    优先级: shark_override > extra_overrides > RMUC 承办守区 >
            base_volunteers (观察到的) > 就近(fallback)。
    """
    extra = extra_overrides or {}
    teams = []
    for school, row in roster.items():
        if school == SHARK_SCHOOL and shark_override is not None:
            vol = shark_override
        elif school in extra:
            vol = extra[school]
        elif school in A.RMUC_HOSTS:
            vol = A.RMUC_HOSTS[school]
        elif school in base_volunteers:
            vol = base_volunteers[school]
        else:
            vol = _nearest_region(row)
        rank = int(row["points_rank"]) if row["points_rank"] else None
        teams.append(A._build_team(school, vol, rank, distances))
    return teams


def _run_scenario(
    roster: Dict[str, dict],
    distances,
    base_volunteers: Dict[str, str],
    shark_override: str,
    extra_overrides: Optional[Dict[str, str]] = None,
) -> dict:
    """跑一次完整调剂,返回 SHARK 的命运 + 名额/对手证据。"""
    teams = _build_teams_for_scenario(
        roster, distances, base_volunteers, shark_override, extra_overrides
    )
    initial = {r: sum(1 for t in teams if t.volunteer == r) for r in REGIONS}
    result = A.allocate(teams, verbose=False)

    team_d25 = {s: row["detail_2025"] for s, row in roster.items()}
    team_ff = {
        s: int(row["full_form_ranking"]) if row["full_form_ranking"] else 999
        for s, row in roster.items()
    }
    team_type = {s: row["team_type"] for s, row in roster.items()}

    t16, floating, national = _compute_spots(result.regions, team_d25)

    shark = next(t for t in teams if t.school == SHARK_SCHOOL)
    shark_ff = team_ff[SHARK_SCHOOL]

    final_members = result.regions[shark.assigned]
    ff_scores = [team_ff[t.school] for t in final_members if team_ff[t.school] < 999]
    ff_position = sum(1 for x in ff_scores if x < shark_ff) + 1
    spots = national[shark.assigned]
    margin = spots - ff_position

    stronger = sorted(
        [
            {
                "school": t.school,
                "city": t.city,
                "ff": team_ff[t.school],
                "rank": t.rank,
                "detail_2025": team_d25[t.school],
                "team_type": team_type[t.school],
                "is_protected": t.is_protected,
            }
            for t in final_members
            if team_ff[t.school] < shark_ff and team_ff[t.school] < 999
        ],
        key=lambda x: x["ff"],
    )

    # 同城无锡所有队伍
    wuxi_same = [
        {
            "school": t.school,
            "rank": t.rank,
            "volunteer": t.volunteer,
            "assigned": t.assigned,
            "status": t.status,
            "ff": team_ff.get(t.school),
        }
        for t in teams if t.city == "无锡市"
    ]

    return {
        "shark_volunteer":      shark_override,
        "initial_counts":       initial,
        "final_region":         shark.assigned,
        "status":               shark.status,
        "ff_position":          ff_position,
        "spots_national":       spots,
        "spots_total_max":      16,
        "margin":               margin,
        "top16_by_region":      t16,
        "floating_by_region":   floating,
        "national_by_region":   national,
        "adjustment_order":     result.order,
        "stronger_in_region":   stronger,
        "wuxi_same_city":       wuxi_same,
        "region_size":          len(final_members),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 压力测试情景
# ──────────────────────────────────────────────────────────────────────────────

_HUABEI_FLEE = [
    "首都师范大学", "北方工业大学", "北京理工大学",
    "中国石油大学（北京）", "北京信息科技大学",
    "天津大学",
    "华北理工大学", "华北科技学院", "河北科技大学",
    "太原理工大学", "太原工业学院", "太原科技大学", "中北大学",
    "青岛大学", "山东科技大学", "中国石油大学（华东）",
]
_CHANGSANJIAO_FLEE = [
    "复旦大学", "上海大学", "华东理工大学", "西交利物浦大学",
    "江苏大学", "常州大学", "南京理工大学江阴校区", "杭州电子科技大学",
]
_HUANAN_FLEE = [
    "华南师范大学", "华南师范大学（佛山校区）", "广州航海学院",
    "仲恺农业工程学院", "香港科技大学（广州）", "香港大学",
    "广西大学", "厦门大学", "厦门理工学院",
]
_XIBEI_XINAN_TO_EAST = [
    "西安交通大学", "西北工业大学", "西安电子科技大学", "长安大学",
    "四川大学", "西南交通大学", "成都大学", "成都信息工程大学",
    "重庆理工大学",
]


def _scenarios() -> List[dict]:
    return [
        {
            "key": "baseline",
            "name": "基线",
            "description": "未提交者按就近原则补报,无战略流动",
            "extra": {},
        },
        {
            "key": "huabei_flee",
            "name": "华北 16 队涌北",
            "description": "京/津/冀/晋/鲁的 16 支队战略性报北部,抢 12 名额",
            "extra": {s: "北部" for s in _HUABEI_FLEE},
        },
        {
            "key": "changsanjiao_flee",
            "name": "长三角 8 弱队涌北",
            "description": "上海/江苏/杭州历史战绩弱的 8 支队战略性报北部",
            "extra": {s: "北部" for s in _CHANGSANJIAO_FLEE},
        },
        {
            "key": "huanan_flee",
            "name": "华南 9 队涌北",
            "description": "华南 9 支队赌名额涌北(距离极远)",
            "extra": {s: "北部" for s in _HUANAN_FLEE},
        },
        {
            "key": "grand_equilibrium",
            "name": "综合博弈均衡",
            "description": "三股涌北 + 西北/西南走东部,最激烈的博弈状态",
            "extra": {
                **{s: "北部" for s in _HUABEI_FLEE + _CHANGSANJIAO_FLEE + _HUANAN_FLEE},
                **{s: "东部" for s in _XIBEI_XINAN_TO_EAST},
            },
        },
    ]


# ──────────────────────────────────────────────────────────────────────────────
# 对外主函数
# ──────────────────────────────────────────────────────────────────────────────

def build_decision_panel(live_teams, distances, ranks) -> Optional[dict]:
    """构建 SHARK 决策面板 payload。

    参数:
        live_teams   allocator.load_teams_live 拿到的 Team 列表(可为空)
        distances    allocator.load_distances() 结果
        ranks        allocator.load_ranks() 结果(本函数目前不直接用,传入备用)
    返回:
        dict  嵌入 docs/data.json['shark_decision'];若 roster 缺失返回 None
    """
    roster = load_roster()
    if not roster or SHARK_SCHOOL not in roster:
        return None

    shark_row = roster[SHARK_SCHOOL]
    rival_row = roster.get(SAME_CITY_RIVAL)
    base_volunteers = _extract_live_volunteers(live_teams or [])

    # Step 1: 基线下的三种志愿选择
    options = [
        _run_scenario(roster, distances, base_volunteers, choice)
        for choice in REGIONS
    ]

    # Step 2: 5 个博弈情景 × 3 个志愿选择的压力测试
    scenarios = _scenarios()
    sensitivity = []
    for sc in scenarios:
        row = {
            "key":         sc["key"],
            "name":        sc["name"],
            "description": sc["description"],
            "results":     [],
        }
        for choice in REGIONS:
            r = _run_scenario(
                roster, distances, base_volunteers,
                choice, extra_overrides=sc["extra"],
            )
            row["results"].append({
                "shark_volunteer":    choice,
                "final_region":       r["final_region"],
                "status":             r["status"],
                "ff_position":        r["ff_position"],
                "spots_national":     r["spots_national"],
                "margin":             r["margin"],
                "initial_counts":     r["initial_counts"],
                "national_by_region": r["national_by_region"],
                "top16_by_region":    r["top16_by_region"],
            })
        sensitivity.append(row)

    # Step 3: 按 worst-case margin 选最佳志愿
    worst_case: Dict[str, int] = {}
    for i, choice in enumerate(REGIONS):
        margins = [sc["results"][i]["margin"] for sc in sensitivity]
        worst_case[choice] = min(margins)

    # 先按 worst-case margin,再按"志愿身份优先于调剂",再按基线 margin
    def _score(i):
        choice = REGIONS[i]
        opt = options[i]
        return (
            worst_case[choice],
            1 if opt["status"] == "volunteer" else 0,
            opt["margin"],
        )
    best_idx = max(range(len(REGIONS)), key=_score)
    best_choice = REGIONS[best_idx]
    best_option = options[best_idx]

    # Step 4: 组装可读的 target/rival/observed/recommendation
    def _int_or_none(v):
        return int(v) if v not in (None, "") else None

    def _float_or_none(v):
        return float(v) if v not in (None, "") else None

    target = {
        "school":            SHARK_SCHOOL,
        "team":              shark_row.get("team", "SHARK"),
        "city":              shark_row["city"],
        "team_type":         shark_row["team_type"],
        "rank":              _int_or_none(shark_row["points_rank"]),
        "ff":                _int_or_none(shark_row["full_form_ranking"]),
        "points":            _float_or_none(shark_row.get("points")),
        "participated_2025": shark_row["participated_2025"] == "True",
        "detail_2025":       shark_row["detail_2025"],
        "detail_2025_cn":    DETAIL_CN.get(shark_row["detail_2025"], shark_row["detail_2025"]),
        "rmul_2026_top4":    shark_row.get("rmul_2026_top4") or None,
        "rmul_2026_top4_cn": DETAIL_CN.get(shark_row.get("rmul_2026_top4", ""), shark_row.get("rmul_2026_top4") or None),
        "dist": {
            "南部": int(shark_row["distance_to_changsha"]),
            "东部": int(shark_row["distance_to_jinan"]),
            "北部": int(shark_row["distance_to_shenyang"]),
        },
    }

    rival_info = None
    if rival_row:
        rival_info = {
            "school":         SAME_CITY_RIVAL,
            "team":           rival_row.get("team"),
            "city":           rival_row["city"],
            "rank":           _int_or_none(rival_row["points_rank"]),
            "ff":             _int_or_none(rival_row["full_form_ranking"]),
            "team_type":      rival_row["team_type"],
            "live_volunteer": base_volunteers.get(SAME_CITY_RIVAL),
            "submitted":      SAME_CITY_RIVAL in base_volunteers,
        }

    observed = {
        "submitted_count":    len(base_volunteers),
        "roster_total":       len(roster),
        "submitted_by_region": {
            r: sum(1 for v in base_volunteers.values() if v == r)
            for r in REGIONS
        },
        "shark_submitted":    SHARK_SCHOOL in base_volunteers,
        "shark_live_volunteer": base_volunteers.get(SHARK_SCHOOL),
    }

    recommendation = {
        "volunteer":         best_choice,
        "final_region":      best_option["final_region"],
        "status":            best_option["status"],
        "status_cn":         STATUS_CN[best_option["status"]],
        "ff_position":       best_option["ff_position"],
        "spots_national":    best_option["spots_national"],
        "baseline_margin":   best_option["margin"],
        "worst_case_margin": worst_case[best_choice],
        "all_worst_case":    worst_case,
        "summary": (
            f"报志愿 {best_choice} → 最终 {best_option['final_region']}"
            f"({STATUS_CN[best_option['status']]}),"
            f"ff 第 {best_option['ff_position']} 位,"
            f"全国赛 {best_option['spots_national']} 名额,"
            f"基线余裕 {best_option['margin']} 位,"
            f"博弈最坏余裕 {worst_case[best_choice]} 位。"
        ),
    }

    return {
        "target":          target,
        "same_city_rival": rival_info,
        "observed_state":  observed,
        "options":         options,
        "sensitivity":     sensitivity,
        "recommendation":  recommendation,
    }
