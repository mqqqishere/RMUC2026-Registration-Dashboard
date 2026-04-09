#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RoboMaster 2026 区域赛志愿调剂模拟器

根据官方《RoboMaster 2026 区域赛参赛反馈公告》
(https://www.robomaster.com/zh-CN/resource/pages/announcement/1910)
的"志愿优先录取 + 地理就近 + 积分榜排名"规则实现。

【规则原文要点】
1. 三个赛区：南部(长沙) / 东部(济南) / 北部(沈阳)，各 32 支
2. 组委会统筹调剂按赛区志愿数由少至多的顺序 (A < B < C)：
   ① 志愿优先录取：已选择 A 赛区队伍全部录取至 A 赛区；当赛季 RMUC&RMUL
      承办院校全部按志愿录取
   ② 地理就近调剂：A 赛区剩余需调剂数 = 32 − 已录取志愿数；按学校所在
      城市与赛区所在城市的直线距离"由近到远"排序（按官方附表距离计算），
      从 B、C 两赛区的志愿队伍中（承办院校等"优先录取权益队伍"除外）
      选取相应数量调入 A 赛区
   ③ 若两支参赛队伍学校所在城市一致，优先按积分榜排名，"排名靠后者"
      将优先被调剂至该赛区
   ④ A 调剂结束后，比较 B、C 剩余志愿数，继续从志愿数较少的一方开始，
      直至 B、C 分别满足 32 支容量
3. RMUC 2026 区域赛承办院校（优先录取权益）：
      长沙理工大学 → 南部；齐鲁工业大学 → 东部；东北大学 → 北部

【数据来源】
- 距离：distances.csv —— 直接解析自官方公告 HTML 的附表（96 所学校）
- 志愿：qingflow.py 从
        https://qingflow.com/appView/e3bol1op1c02/shareView/e3bol20d1c02
        的只读分享接口抓取
- 积分榜：可通过 --ranks ranks.csv 传入（可选；为空时同城按学校名稳定排序）

【用法】
# 1. 从线上看板抓取志愿并直接分配
python3 allocator.py --url https://qingflow.com/appView/e3bol1op1c02/shareView/e3bol20d1c02

# 2. 手动提供 CSV（字段: school,volunteer[,rank]）
python3 allocator.py --teams teams.csv

# 3. 同时指定积分榜
python3 allocator.py --url <...> --ranks ranks.csv --output result.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qingflow  # noqa: E402

# ──────────────────────────────────────────────────────────────────────────────
# 赛区 & 承办院校
# ──────────────────────────────────────────────────────────────────────────────

REGIONS = ("南部", "东部", "北部")
REGION_CAPACITY = 32
REGION_CITY: Dict[str, str] = {"南部": "长沙市", "东部": "济南市", "北部": "沈阳市"}
DIST_COL: Dict[str, str] = {
    "南部": "dist_south",
    "东部": "dist_east",
    "北部": "dist_north",
}

# ── 志愿优先录取权益队伍 ───────────────────────────────────────────────
# 规则原文："本赛季 RMUL 2026 各站点承办院校、RMUC 2026 各赛区承办院校拥有
# 志愿优先录取权益；其中 RMUC 2026 区域赛承办院校默认在所承办赛区参赛，
# 如选择其它赛区将被优先录取"

# RMUC 2026 区域赛承办院校 → 承办赛区
# 来源：announcement/1910 赛区设置表
RMUC_HOSTS: Dict[str, str] = {
    "长沙理工大学": "南部",     # 南部赛区承办
    "齐鲁工业大学": "东部",     # 东部赛区承办
    "东北大学":     "北部",     # 北部赛区承办
}

# RMUL 2026 9 大站点承办院校（无 RMUC 默认赛区，仅享受志愿保护）
# 来源：announcement/1903 高校联盟赛参赛反馈须知
# 注：重庆大学 是 RMUL 重庆站 承办院校，但不在 RMUC 区域赛的 96 队参赛名单中，
#     因此不会出现在 RMUC 分配逻辑里，此处仍完整列出以备查。
RMUL_HOSTS: set = {
    "中国科学技术大学",          # 安徽站
    "北京理工大学（珠海）",      # 华南站
    "齐鲁工业大学",              # 山东站 (同时也是 RMUC 东部 承办)
    "重庆大学",                  # 重庆站 (不在 RMUC 96 队中)
    "沈阳理工大学",              # 东北站
    "南京理工大学",              # 江苏站
    "内蒙古科技大学",            # 华北站
    "上海工程技术大学",          # 上海站
    "成都大学",                  # 四川站
}

def is_protected_host(school: str) -> bool:
    """是否为志愿优先录取权益队伍（RMUC 或 RMUL 承办院校）。"""
    return school in RMUC_HOSTS or school in RMUL_HOSTS

# 看板"赛区选择"列里的选项文本 → 赛区缩写
LANE_NAME_TO_REGION: Dict[str, str] = {
    "南部赛区": "南部",
    "东部赛区": "东部",
    "北部赛区": "北部",
    "南部": "南部", "东部": "东部", "北部": "北部",
}


# ──────────────────────────────────────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SchoolInfo:
    """来自官方距离附表的单校条目。"""
    school: str
    city: str
    dist: Dict[str, int]   # {"南部": km, "东部": km, "北部": km}


@dataclass
class Team:
    school: str
    city: str
    volunteer: str                    # 南部 / 东部 / 北部
    rank: Optional[int] = None        # 积分榜排名（数字越小越靠前），未知为 None
    host_region: Optional[str] = None # RMUC 承办院校所承办赛区（RMUL 承办为 None）
    is_protected: bool = False        # True = RMUC 或 RMUL 承办，志愿不可被调剂

    dist: Dict[str, int] = field(default_factory=dict)
    assigned: Optional[str] = None
    status: str = ""                  # volunteer / host / transfer

    @property
    def is_host(self) -> bool:
        """向后兼容：所有享有志愿优先录取权益的队伍 (RMUC+RMUL 承办)。"""
        return self.is_protected


# ──────────────────────────────────────────────────────────────────────────────
# 距离表
# ──────────────────────────────────────────────────────────────────────────────

def load_distances(path: str = None) -> Dict[str, SchoolInfo]:
    """加载官方距离附表 (CSV)。键为学校名。"""
    if path is None:
        path = os.path.join(HERE, "distances.csv")
    out: Dict[str, SchoolInfo] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out[row["school"].strip()] = SchoolInfo(
                school=row["school"].strip(),
                city=row["city"].strip(),
                dist={
                    "南部": int(row["dist_south"]),
                    "东部": int(row["dist_east"]),
                    "北部": int(row["dist_north"]),
                },
            )
    return out


def load_ranks(path: Optional[str]) -> Dict[str, int]:
    """加载积分榜 CSV（字段: school,rank）。排名越小越靠前。"""
    if not path:
        return {}
    out: Dict[str, int] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            out[row["school"].strip()] = int(row["rank"])
    return out


# ──────────────────────────────────────────────────────────────────────────────
# 输入加载：CSV 或线上看板
# ──────────────────────────────────────────────────────────────────────────────

def _build_team(school: str, volunteer: str, rank: Optional[int],
                distances: Dict[str, SchoolInfo]) -> Team:
    info = distances.get(school)
    if info is None:
        raise SystemExit(
            f"[错误] 学校 {school!r} 不在官方距离表中。请核对名称或在 distances.csv 补充。"
        )
    region = LANE_NAME_TO_REGION.get(volunteer.strip())
    if region not in REGIONS:
        raise SystemExit(f"[错误] 学校 {school!r} 的志愿 {volunteer!r} 非法")
    return Team(
        school=school,
        city=info.city,
        volunteer=region,
        rank=rank,
        host_region=RMUC_HOSTS.get(school),        # 仅 RMUC 承办有默认赛区
        is_protected=is_protected_host(school),    # RMUC+RMUL 承办都享权益
        dist=dict(info.dist),
    )


def load_teams_csv(path: str, distances, ranks: Dict[str, int]) -> List[Team]:
    teams: List[Team] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            school = row["school"].strip()
            volunteer = row["volunteer"].strip()
            rank = row.get("rank")
            rank_val = int(rank) if rank and rank.strip() else ranks.get(school)
            teams.append(_build_team(school, volunteer, rank_val, distances))
    return teams


def load_teams_live(url_or_key: str, distances, ranks: Dict[str, int],
                    verbose: bool) -> List[Team]:
    """从 qingflow 分享看板抓取志愿。"""
    if url_or_key.startswith("http"):
        _, view_key = qingflow.parse_share_url(url_or_key)
    else:
        view_key = url_or_key
    view = qingflow.fetch_view(view_key, verbose=verbose)
    field_map = qingflow.guess_field_map(view["questions"])
    if "school" not in field_map or "volunteer" not in field_map:
        raise SystemExit("[错误] 看板未识别出'申请学校'或'赛区选择'列")

    school_qid = field_map["school"]
    vol_qid = field_map["volunteer"]
    lane_name_by_id = {ln.lane_id: ln.name for ln in view["lanes"]}

    # 按学校聚合：一支队伍可能多次提交，取最后一次（最新）
    latest_by_school: Dict[str, str] = {}
    for rec in view["records"]:
        school = rec.get(school_qid).strip()
        if not school:
            continue
        vol_text = rec.get(vol_qid).strip()
        # 若 answers 抽不到 vol_text，回退用泳道名
        if not vol_text and rec.lane_id:
            vol_text = lane_name_by_id.get(rec.lane_id, "")
        if not vol_text:
            continue
        latest_by_school[school] = vol_text

    teams: List[Team] = []
    missing: List[str] = []
    for school, vol in latest_by_school.items():
        if school not in distances:
            missing.append(school)
            continue
        teams.append(_build_team(school, vol, ranks.get(school), distances))
    if missing:
        print(f"[警告] 以下 {len(missing)} 所学校不在官方距离表中，已跳过：")
        for s in missing:
            print(f"   · {s}")
    return teams


# ──────────────────────────────────────────────────────────────────────────────
# 调剂算法（严格按公告第 2 条）
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class AllocationResult:
    teams: List[Team]
    regions: Dict[str, List[Team]]
    order: List[str]                  # 调剂处理顺序

    def summary(self) -> str:
        lines = []
        for r in REGIONS:
            members = self.regions.get(r, [])
            host = sum(1 for t in members if t.status == "host")
            vol = sum(1 for t in members if t.status == "volunteer")
            tr = sum(1 for t in members if t.status == "transfer")
            lines.append(
                f"  {r}赛区({REGION_CITY[r]})  总计 {len(members):>2} | "
                f"承办 {host} | 志愿 {vol:>2} | 调入 {tr:>2}"
            )
        moved = sum(1 for t in self.teams if t.status == "transfer")
        return "\n".join(lines) + f"\n  ─── 共 {moved} 支队伍被调剂"


def _rank_key(t: Team) -> int:
    """积分排名缺失时用一个很大的值，保证其"靠后"。"""
    return t.rank if t.rank is not None else 10**9


def allocate(teams: List[Team], verbose: bool = False) -> AllocationResult:
    """
    严格按 announcement/1910 第 2 条实现。关键不变量：

    · 一个赛区一旦作为"目标"被调剂过一次，即锁定，不再被后续轮次抽调；
      这等价于规则 "已选择A赛区队伍全部录取至A赛区" 的闭合解读 ——
      一旦 A 填满，就不能从 A 再抽队伍去填 B/C。
    · RMUC + RMUL 承办院校永远按其志愿录取，不进入调剂池。
    · 调剂顺序：第一轮选原始志愿数最少者；其后每轮选"剩余志愿数"最少者。
    """
    # ── 阶段 1：志愿入位 ──
    assigned: Dict[str, List[Team]] = {r: [] for r in REGIONS}
    for t in teams:
        assigned[t.volunteer].append(t)
        t.assigned = t.volunteer
        t.status = "host" if t.is_protected else "volunteer"

    vcnt = {r: len(assigned[r]) for r in REGIONS}
    if verbose:
        print("\n【阶段1 志愿入位】")
        for r in REGIONS:
            print(f"  {r}: 志愿 {vcnt[r]} 支 "
                  f"(权益 {sum(1 for t in assigned[r] if t.is_protected)})")

    # ── 阶段 2：按由少至多顺序调剂 ──
    # 每轮从"尚未处理过"的赛区里选当前数量最少的作为目标，
    # 从同样"尚未处理过"的非目标赛区里（非权益队伍）按距离就近抽取。

    # 若无赛区超过容量，全部志愿直录，无需调剂。
    # （规则前提："队伍自主动态选择结束后"总数 96，才可能出现溢出。
    #  提交期间 < 96 且无溢出时跳过，避免错误抽调。）
    if all(vcnt[r] <= REGION_CAPACITY for r in REGIONS):
        return AllocationResult(teams=teams, regions=assigned, order=[])

    process_order: List[str] = []
    processed: set = set()                # 已作为目标完成的赛区（锁定）
    pending = sorted(REGIONS, key=lambda r: vcnt[r])
    if verbose:
        print(f"\n【阶段2 调剂顺序】初始：" +
              " < ".join(f"{r}({vcnt[r]})" for r in pending))

    while pending:
        # 在剩余 pending 中选当前已录人数最少的
        pending.sort(key=lambda r: len(assigned[r]))
        target = pending.pop(0)
        process_order.append(target)
        need = REGION_CAPACITY - len(assigned[target])
        if need <= 0:
            if verbose:
                print(f"  → 跳过 {target}：已有 {len(assigned[target])} 支（无需调剂）")
            processed.add(target)
            continue

        # 候选池：未被处理过 且 非目标 的赛区中的 非权益 队伍
        pool: List[Team] = []
        for r in REGIONS:
            if r == target or r in processed:
                continue
            for t in assigned[r]:
                if t.is_protected:
                    continue                          # 权益队伍不参与调剂
                pool.append(t)

        # 全局排序键（严格符合规则）：
        #   1) 距目标赛区直线距离升序（由近到远）
        #   2) 城市名做 grouping key
        #   3) 积分榜排名降序（rank 大 = 排名靠后 = 优先调剂），仅同城生效
        #   4) 学校名做稳定兜底
        pool.sort(key=lambda t: (t.dist[target], t.city,
                                 -_rank_key(t), t.school))

        take = pool[:need]
        if verbose:
            print(f"  → 调剂 {target}：需 {need} 支，池 {len(pool)} 支，"
                  f"取 {len(take)} 支")
        for t in take:
            assigned[t.volunteer].remove(t)
            assigned[target].append(t)
            t.assigned = target
            t.status = "transfer"
        processed.add(target)

    return AllocationResult(teams=teams, regions=assigned, order=process_order)


# ──────────────────────────────────────────────────────────────────────────────
# 输出
# ──────────────────────────────────────────────────────────────────────────────

def dump_csv(path: str, result: AllocationResult) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "school", "city", "volunteer", "assigned", "status",
            "rank", "dist_to_assigned_km", "is_host",
        ])
        for t in sorted(result.teams, key=lambda t: (t.assigned, t.status != "host",
                                                     t.status != "volunteer",
                                                     _rank_key(t))):
            w.writerow([
                t.school, t.city, t.volunteer, t.assigned, t.status,
                t.rank if t.rank is not None else "",
                t.dist[t.assigned] if t.assigned else "",
                "Y" if t.is_host else "",
            ])


def print_result(result: AllocationResult) -> None:
    print("\n════════ 最终录取结果 ════════")
    for region in REGIONS:
        members = result.regions.get(region, [])
        print(f"\n● {region}赛区（{REGION_CITY[region]}） — 共 {len(members)} 支")
        members_sorted = sorted(
            members,
            key=lambda t: (t.status != "host", t.status != "volunteer",
                           t.dist[region], _rank_key(t)),
        )
        for t in members_sorted:
            tag = {"host": "承办", "volunteer": "志愿", "transfer": "调剂"}[t.status]
            mark = "" if t.volunteer == region else f"  ← 原志愿 {t.volunteer}"
            rankstr = f"rank={t.rank}" if t.rank is not None else "rank=－"
            print(f"  [{tag}] {t.school}（{t.city}）  "
                  f"dist={t.dist[region]}km  {rankstr}{mark}")
    print("\n──── 汇总 ────")
    print(result.summary())


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="RoboMaster 2026 区域赛志愿调剂模拟器")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="qingflow 分享看板 URL（或 viewKey）")
    src.add_argument("--teams", help="本地 CSV，字段: school,volunteer[,rank]")
    ap.add_argument("--ranks", help="可选的积分榜 CSV（字段: school,rank）")
    ap.add_argument("--distances", help="距离表路径（默认 distances.csv）")
    ap.add_argument("--output", "-o", help="输出结果 CSV")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    distances = load_distances(args.distances)
    ranks = load_ranks(args.ranks)

    if args.url:
        teams = load_teams_live(args.url, distances, ranks, verbose=args.verbose)
    else:
        teams = load_teams_csv(args.teams, distances, ranks)

    if args.verbose:
        print(f"\n[加载完成] {len(teams)} 支队伍  "
              f"(积分榜覆盖 {sum(1 for t in teams if t.rank is not None)} 支，"
              f"承办院校 {sum(1 for t in teams if t.is_host)} 所)")

    if not teams:
        print("[提示] 看板当前没有可分配的志愿数据 "
              "（首次提交窗口 2026-04-09 15:00 起）")
        return 0

    result = allocate(teams, verbose=args.verbose)
    print_result(result)

    if args.output:
        dump_csv(args.output, result)
        print(f"\n已写入 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
