#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RMUC 2026 瑞士轮模拟器。

基于当前调剂后的 32 支赛区名单，按 2025 赛制编排复刻抽签、瑞士轮与淘汰赛，
并用 Monte Carlo 近似给出 2026 名额口径下的国赛/复活赛概率。
同时预生成可复现的单次赛程样本池，供前端直接随机抽取展示。
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_ITERATIONS = int(os.environ.get("SWISS_SIM_ITERATIONS", "1400"))
DEFAULT_SAMPLE_POOL = int(os.environ.get("SWISS_SIM_SAMPLE_POOL", "24"))
DEFAULT_SCHOOL = "江南大学霞客湾校区"
MODEL_VERSION = "swiss-sim-v1.1"

REGION_ORDER = {"南部": 0, "东部": 1, "北部": 2}
REGION_SLUG = {"南部": "south", "东部": "east", "北部": "north"}
QUALIFICATION_LABELS = {
    "national": "国赛",
    "revival": "复活赛",
    "out": "未晋级",
}
EARNED_BADGE = {
    "national": {
        "earned_badge": "national",
        "earned_badge_label": "国赛资格",
        "earned_badge_title": "通过本样本比赛获得国赛资格",
    },
    "revival": {
        "earned_badge": "revival",
        "earned_badge_label": "复活资格",
        "earned_badge_title": "通过本样本比赛获得复活资格",
    },
}

TIER1_FIXED_SLOTS = ("A1", "A3", "A5", "A7", "B1", "B3", "B5", "B7")
GROUP_FILL_SLOTS = {
    "A": ("A2", "A4", "A6", "A8", "A9", "A10", "A11", "A12", "A13", "A14", "A15", "A16"),
    "B": ("B2", "B4", "B6", "B8", "B9", "B10", "B11", "B12", "B13", "B14", "B15", "B16"),
}
ROUND1_PAIRINGS = ((1, 9), (2, 10), (11, 3), (12, 4), (5, 13), (6, 14), (15, 7), (16, 8))
ROUND_OF_16_PAIRINGS = (
    ("B1", "A8"),
    ("B5", "A4"),
    ("A7", "B2"),
    ("A3", "B6"),
    ("A6", "B3"),
    ("A2", "B7"),
    ("B4", "A5"),
    ("B8", "A1"),
)
QUARTERFINAL_PAIRINGS = ((0, 1), (3, 2), (4, 5), (7, 6))
PLACEMENT_ROUND1_PAIRINGS = ((0, 1), (3, 2), (4, 5), (7, 6))
PLACEMENT_ROUND2_PAIRINGS = ((0, 2), (1, 3))
QUALIFICATION_CROSS_PAIRINGS = ((0, 2), (1, 3))

RMUL_BOOST_WEIGHT = {
    "champion": 2.8,
    "runner_up": 2.2,
    "third_place": 1.8,
    "fourth_place": 1.5,
}
FULL_FORM_BOOST_SCALE = 0.12
MAX_GAP_CLIP = 18.0
LOGISTIC_SCALE = 8.5

BASE_MARGIN_CENTER = 760.0
BASE_MARGIN_SCALE = 82.0
BASE_MARGIN_NOISE = 210.0
OUTPOST_MARGIN_CENTER = 180.0
OUTPOST_MARGIN_SCALE = 24.0
OUTPOST_MARGIN_NOISE = 65.0
TEAM_DAMAGE_BASE = 3350.0
TEAM_DAMAGE_SCORE_SCALE = 23.0
TEAM_DAMAGE_GAP_SCALE = 30.0
TEAM_DAMAGE_NOISE = 220.0

STAGE_LABELS = {
    "champion": "冠军",
    "runner_up": "亚军",
    "top4": "四强",
    "top8": "八强",
    "national": "国赛线",
    "revival": "复活线",
    "out": "未晋级",
}

RECORD_BUCKET = {
    (3, 0): 0,
    (3, 1): 1,
    (3, 2): 2,
    (2, 3): 3,
    (1, 3): 4,
    (0, 3): 5,
}


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round(value: float, digits: int = 4) -> float:
    return round(value, digits)


def _seeded_order(regions: Iterable[str]) -> List[str]:
    return sorted(regions, key=lambda item: REGION_ORDER.get(item, 9))


@dataclass(frozen=True)
class TeamProfile:
    school: str
    team: str
    city: str
    assigned: str
    team_type: str
    points: Optional[float]
    points_rank: Optional[int]
    full_form_ranking: Optional[int]
    rmul_2026_top4: str
    rmul_2026_top4_cn: str
    detail_2025_cn: str
    strength_score: float
    full_form_score: float
    strength_rank_global: Optional[int]
    strength_rank_region: Optional[int]
    seed_rank_region: int = 0
    seed_tier: str = ""


@dataclass
class TeamState:
    profile: TeamProfile
    group: str
    slot: str
    wins: int = 0
    losses: int = 0
    terminal_round: int = 0
    opponents: List[str] = field(default_factory=list)
    game_wins: int = 0
    game_losses: int = 0
    total_games: int = 0
    base_margin_sum: float = 0.0
    outpost_margin_sum: float = 0.0
    damage_sum: float = 0.0

    @property
    def school(self) -> str:
        return self.profile.school

    @property
    def is_active(self) -> bool:
        return self.wins < 3 and self.losses < 3

    @property
    def record(self) -> Tuple[int, int]:
        return self.wins, self.losses


@dataclass
class TeamAggregate:
    profile: TeamProfile
    stage_counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    national_count: int = 0
    revival_or_better_count: int = 0
    revival_only_count: int = 0
    out_count: int = 0
    top8_count: int = 0
    top4_count: int = 0
    total_rank: int = 0


def _seed_sort_key(profile: TeamProfile) -> Tuple[float, int, int, float, str]:
    return (
        -(profile.points if profile.points is not None else float("-inf")),
        profile.points_rank if profile.points_rank is not None else 10 ** 9,
        profile.full_form_ranking if profile.full_form_ranking is not None else 10 ** 9,
        -profile.strength_score,
        profile.school,
    )


def _stage_from_rank(rank: int, national_spots: int, revival_spots: int) -> str:
    revival_cut = national_spots + revival_spots
    if rank == 1:
        return "champion"
    if rank == 2:
        return "runner_up"
    if rank <= 4:
        return "top4"
    if rank <= 8:
        return "top8"
    if rank <= national_spots:
        return "national"
    if rank <= revival_cut:
        return "revival"
    return "out"


def _qualification_result(rank: int, national_spots: int, revival_spots: int) -> str:
    if rank <= national_spots:
        return "national"
    if rank <= national_spots + revival_spots:
        return "revival"
    return "out"


def _boost_value(profile: TeamProfile) -> float:
    base = profile.full_form_score * FULL_FORM_BOOST_SCALE
    rmul = RMUL_BOOST_WEIGHT.get(profile.rmul_2026_top4, 0.0)
    return _clip(base + rmul, 0.0, profile.full_form_score + 10.0)


def _effective_gap(left: TeamProfile, right: TeamProfile) -> float:
    left_score = left.strength_score
    right_score = right.strength_score
    if left_score < right_score:
        left_score += _boost_value(left)
    elif right_score < left_score:
        right_score += _boost_value(right)
    return _clip(left_score - right_score, -MAX_GAP_CLIP, MAX_GAP_CLIP)


def single_game_probability(left: TeamProfile, right: TeamProfile) -> float:
    gap = _effective_gap(left, right)
    return 1.0 / (1.0 + math.exp(-gap / LOGISTIC_SCALE))


def _margin_sample(center: float, scale: float, noise: float, gap_abs: float, rng: random.Random) -> float:
    sampled = center + gap_abs * scale + rng.gauss(0.0, noise)
    return max(40.0, sampled)


def _team_damage(profile: TeamProfile, gap_abs: float, won_game: bool, rng: random.Random) -> float:
    bias = 180.0 if won_game else -90.0
    value = (
        TEAM_DAMAGE_BASE
        + profile.strength_score * TEAM_DAMAGE_SCORE_SCALE
        + gap_abs * TEAM_DAMAGE_GAP_SCALE
        + bias
        + rng.gauss(0.0, TEAM_DAMAGE_NOISE)
    )
    return max(1200.0, value)


@dataclass(frozen=True)
class SeriesParams:
    game_probability: float
    gap_abs: float
    base_margin_center: float
    outpost_margin_center: float
    left_damage_win_mean: float
    left_damage_loss_mean: float
    right_damage_win_mean: float
    right_damage_loss_mean: float
    bo3_left_2_0_cut: float
    bo3_left_2_1_cut: float
    bo3_right_2_0_cut: float


@lru_cache(maxsize=65536)
def _series_params(left: TeamProfile, right: TeamProfile) -> SeriesParams:
    gap = _effective_gap(left, right)
    game_probability = 1.0 / (1.0 + math.exp(-gap / LOGISTIC_SCALE))
    gap_abs = abs(gap)
    base_margin_center = BASE_MARGIN_CENTER + gap_abs * BASE_MARGIN_SCALE
    outpost_margin_center = OUTPOST_MARGIN_CENTER + gap_abs * OUTPOST_MARGIN_SCALE
    damage_offset = gap_abs * TEAM_DAMAGE_GAP_SCALE
    left_damage_base = TEAM_DAMAGE_BASE + left.strength_score * TEAM_DAMAGE_SCORE_SCALE + damage_offset
    right_damage_base = TEAM_DAMAGE_BASE + right.strength_score * TEAM_DAMAGE_SCORE_SCALE + damage_offset
    q = 1.0 - game_probability
    left_2_0 = game_probability * game_probability
    left_2_1 = left_2_0 + 2.0 * left_2_0 * q
    right_2_0 = left_2_1 + q * q
    return SeriesParams(
        game_probability=game_probability,
        gap_abs=gap_abs,
        base_margin_center=base_margin_center,
        outpost_margin_center=outpost_margin_center,
        left_damage_win_mean=left_damage_base + 180.0,
        left_damage_loss_mean=left_damage_base - 90.0,
        right_damage_win_mean=right_damage_base + 180.0,
        right_damage_loss_mean=right_damage_base - 90.0,
        bo3_left_2_0_cut=left_2_0,
        bo3_left_2_1_cut=left_2_1,
        bo3_right_2_0_cut=right_2_0,
    )


def _sample_best_of_three_score(params: SeriesParams, rng: random.Random) -> Tuple[int, int]:
    draw = rng.random()
    if draw < params.bo3_left_2_0_cut:
        return 2, 0
    if draw < params.bo3_left_2_1_cut:
        return 2, 1
    if draw < params.bo3_right_2_0_cut:
        return 0, 2
    return 1, 2


def _sample_signed_margin_sum(
    center: float,
    noise: float,
    positive_games: int,
    negative_games: int,
    rng: random.Random,
) -> float:
    total = 0.0
    for _ in range(positive_games):
        total += max(40.0, center + rng.gauss(0.0, noise))
    for _ in range(negative_games):
        total -= max(40.0, center + rng.gauss(0.0, noise))
    return total


def _sample_damage_sum(
    win_mean: float,
    loss_mean: float,
    total_games: int,
    won_games: int,
    rng: random.Random,
) -> float:
    if total_games <= 0:
        return 0.0
    mean = won_games * win_mean + (total_games - won_games) * loss_mean
    return rng.gauss(mean, TEAM_DAMAGE_NOISE * math.sqrt(total_games))


def _apply_series_metrics(
    state: TeamState,
    opponent: TeamState,
    match_won: bool,
    game_wins: int,
    game_losses: int,
    base_margin_sum: float,
    outpost_margin_sum: float,
    damage_sum: float,
):
    state.opponents.append(opponent.school)
    state.wins += 1 if match_won else 0
    state.losses += 0 if match_won else 1
    state.game_wins += game_wins
    state.game_losses += game_losses
    state.total_games += game_wins + game_losses
    state.base_margin_sum += base_margin_sum
    state.outpost_margin_sum += outpost_margin_sum
    state.damage_sum += damage_sum


def _run_series_slow(left: TeamState, right: TeamState, best_of: int, rng: random.Random) -> dict:
    wins_needed = best_of // 2 + 1
    left_wins = 0
    right_wins = 0
    left_base_margin_sum = 0.0
    right_base_margin_sum = 0.0
    left_outpost_margin_sum = 0.0
    right_outpost_margin_sum = 0.0
    left_damage_sum = 0.0
    right_damage_sum = 0.0
    game_prob = single_game_probability(left.profile, right.profile)
    gap_abs = abs(_effective_gap(left.profile, right.profile))

    while left_wins < wins_needed and right_wins < wins_needed:
        left_won = rng.random() < game_prob
        base_margin = _margin_sample(
            BASE_MARGIN_CENTER,
            BASE_MARGIN_SCALE,
            BASE_MARGIN_NOISE,
            gap_abs,
            rng,
        )
        outpost_margin = _margin_sample(
            OUTPOST_MARGIN_CENTER,
            OUTPOST_MARGIN_SCALE,
            OUTPOST_MARGIN_NOISE,
            gap_abs,
            rng,
        )
        if left_won:
            left_wins += 1
            left_base_margin_sum += base_margin
            right_base_margin_sum -= base_margin
            left_outpost_margin_sum += outpost_margin
            right_outpost_margin_sum -= outpost_margin
        else:
            right_wins += 1
            left_base_margin_sum -= base_margin
            right_base_margin_sum += base_margin
            left_outpost_margin_sum -= outpost_margin
            right_outpost_margin_sum += outpost_margin
        left_damage_sum += _team_damage(left.profile, gap_abs, left_won, rng)
        right_damage_sum += _team_damage(right.profile, gap_abs, not left_won, rng)

    _apply_series_metrics(
        left,
        right,
        left_wins > right_wins,
        left_wins,
        right_wins,
        left_base_margin_sum,
        left_outpost_margin_sum,
        left_damage_sum,
    )
    _apply_series_metrics(
        right,
        left,
        right_wins > left_wins,
        right_wins,
        left_wins,
        right_base_margin_sum,
        right_outpost_margin_sum,
        right_damage_sum,
    )
    winner = left if left_wins > right_wins else right
    loser = right if winner.school == left.school else left
    return {
        "winner": winner,
        "loser": loser,
        "left_wins": left_wins,
        "right_wins": right_wins,
    }


def _run_series(left: TeamState, right: TeamState, best_of: int, rng: random.Random) -> dict:
    if best_of != 3:
        return _run_series_slow(left, right, best_of, rng)

    params = _series_params(left.profile, right.profile)
    left_wins, right_wins = _sample_best_of_three_score(params, rng)
    total_games = left_wins + right_wins
    left_base_margin_sum = _sample_signed_margin_sum(
        params.base_margin_center,
        BASE_MARGIN_NOISE,
        left_wins,
        right_wins,
        rng,
    )
    left_outpost_margin_sum = _sample_signed_margin_sum(
        params.outpost_margin_center,
        OUTPOST_MARGIN_NOISE,
        left_wins,
        right_wins,
        rng,
    )
    left_damage_sum = _sample_damage_sum(
        params.left_damage_win_mean,
        params.left_damage_loss_mean,
        total_games,
        left_wins,
        rng,
    )
    right_damage_sum = _sample_damage_sum(
        params.right_damage_win_mean,
        params.right_damage_loss_mean,
        total_games,
        right_wins,
        rng,
    )

    _apply_series_metrics(
        left,
        right,
        left_wins > right_wins,
        left_wins,
        right_wins,
        left_base_margin_sum,
        left_outpost_margin_sum,
        left_damage_sum,
    )
    _apply_series_metrics(
        right,
        left,
        right_wins > left_wins,
        right_wins,
        left_wins,
        -left_base_margin_sum,
        -left_outpost_margin_sum,
        right_damage_sum,
    )
    winner = left if left_wins > right_wins else right
    loser = right if winner.school == left.school else left
    return {
        "winner": winner,
        "loser": loser,
        "left_wins": left_wins,
        "right_wins": right_wins,
    }


def simulate_series(left: TeamState, right: TeamState, best_of: int, rng: random.Random) -> TeamState:
    return _run_series(left, right, best_of, rng)["winner"]


def _opponent_sos(state: TeamState, state_map: Dict[str, TeamState]) -> int:
    return sum(
        state_map[school].game_wins - state_map[school].game_losses
        for school in state.opponents
    )


def _avg(value: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return value / count


def _current_key(state: TeamState, state_map: Dict[str, TeamState]) -> Tuple[int, int, float, float, float, str]:
    return (
        -state.wins,
        state.losses,
        -_opponent_sos(state, state_map),
        -_avg(state.base_margin_sum, state.total_games),
        -_avg(state.outpost_margin_sum, state.total_games),
        -_avg(state.damage_sum, state.total_games),
        state.profile.school,
    )


def _final_key(state: TeamState, state_map: Dict[str, TeamState]) -> Tuple[int, int, float, float, float, str]:
    terminal_priority = state.terminal_round if state.wins == 3 else 10 + state.terminal_round
    return (
        RECORD_BUCKET[state.record],
        terminal_priority,
        -_opponent_sos(state, state_map),
        -_avg(state.base_margin_sum, state.total_games),
        -_avg(state.outpost_margin_sum, state.total_games),
        -_avg(state.damage_sum, state.total_games),
        state.profile.school,
    )


def _resolve_exact_ties(states: Sequence[TeamState], rng: random.Random) -> List[TeamState]:
    if len(states) <= 1:
        return list(states)

    playoff_points = {state.school: 0 for state in states}
    for left_index in range(len(states)):
        for right_index in range(left_index + 1, len(states)):
            left = states[left_index]
            right = states[right_index]
            if rng.random() < single_game_probability(left.profile, right.profile):
                playoff_points[left.school] += 1
            else:
                playoff_points[right.school] += 1

    return sorted(
        states,
        key=lambda state: (
            -playoff_points[state.school],
            -state.profile.strength_score,
            state.profile.school,
        ),
    )


def _sort_states(
    states: Sequence[TeamState],
    key_func,
    state_map: Dict[str, TeamState],
    rng: random.Random,
) -> List[TeamState]:
    buckets: Dict[Tuple[object, ...], List[TeamState]] = defaultdict(list)
    for state in states:
        buckets[key_func(state, state_map)].append(state)

    ordered: List[TeamState] = []
    for key in sorted(buckets):
        tied = buckets[key]
        if len(tied) == 1:
            ordered.extend(tied)
        else:
            ordered.extend(_resolve_exact_ties(tied, rng))
    return ordered


def _serialize_team_ref(state: TeamState) -> dict:
    return {
        "school": state.school,
        "team": state.profile.team,
        "seed_rank_region": state.profile.seed_rank_region,
        "seed_tier": state.profile.seed_tier,
        "slot": state.slot,
        "group": state.group,
    }


def _serialize_standing(
    state: TeamState,
    rank: int,
    state_map: Dict[str, TeamState],
    qualification_result: Optional[str] = None,
) -> dict:
    payload = {
        "rank": rank,
        "school": state.school,
        "team": state.profile.team,
        "seed_rank_region": state.profile.seed_rank_region,
        "seed_tier": state.profile.seed_tier,
        "slot": state.slot,
        "group": state.group,
        "record": {"wins": state.wins, "losses": state.losses},
        "terminal_round": state.terminal_round,
        "sos": _opponent_sos(state, state_map),
        "avg_base_margin": _round(_avg(state.base_margin_sum, state.total_games), 2),
        "avg_outpost_margin": _round(_avg(state.outpost_margin_sum, state.total_games), 2),
        "avg_team_damage": _round(_avg(state.damage_sum, state.total_games), 2),
    }
    if qualification_result:
        payload["qualification_result"] = qualification_result
        payload["qualification_label"] = QUALIFICATION_LABELS[qualification_result]
        payload.update(EARNED_BADGE.get(qualification_result, {
            "earned_badge": None,
            "earned_badge_label": None,
            "earned_badge_title": None,
        }))
    return payload


def _swiss_status(state: TeamState) -> str:
    if state.wins >= 3:
        return "advance"
    if state.losses >= 3:
        return "eliminated"
    return "continue"


def _branch_sort_key(state: TeamState, swiss_overall_rank: Dict[str, int]) -> Tuple[int, str]:
    return (
        swiss_overall_rank[state.school],
        state.school,
    )


def _build_match_card(
    stage: str,
    match_id: str,
    left: TeamState,
    right: TeamState,
    best_of: int,
    left_score: int,
    right_score: int,
    left_pre: Tuple[int, int],
    right_pre: Tuple[int, int],
    left_status: str,
    right_status: str,
) -> dict:
    return {
        "stage": stage,
        "match_id": match_id,
        "best_of": best_of,
        "left": _serialize_team_ref(left),
        "right": _serialize_team_ref(right),
        "score": {
            "left": left_score,
            "right": right_score,
            "text": f"{left_score}:{right_score}",
        },
        "winner_school": left.school if left_score > right_score else right.school,
        "pre_record": {
            "left": {"wins": left_pre[0], "losses": left_pre[1]},
            "right": {"wins": right_pre[0], "losses": right_pre[1]},
        },
        "post_record": {
            "left": {"wins": left.wins, "losses": left.losses},
            "right": {"wins": right.wins, "losses": right.losses},
        },
        "status_after_match": {
            "left": left_status,
            "right": right_status,
        },
    }


def _play_traced_series(
    left: TeamState,
    right: TeamState,
    best_of: int,
    rng: random.Random,
    *,
    stage: str,
    match_id: str,
    left_status: str,
    right_status: str,
) -> Tuple[TeamState, TeamState, dict]:
    left_pre = left.record
    right_pre = right.record
    result = _run_series(left, right, best_of, rng)
    card = _build_match_card(
        stage,
        match_id,
        left,
        right,
        best_of,
        result["left_wins"],
        result["right_wins"],
        left_pre,
        right_pre,
        left_status,
        right_status,
    )
    return result["winner"], result["loser"], card


def _round_snapshot(states: Sequence[TeamState], state_map: Dict[str, TeamState], rng: random.Random) -> List[dict]:
    ordered = _sort_states(states, _current_key, state_map, rng)
    return [
        _serialize_standing(state, rank, state_map)
        for rank, state in enumerate(ordered, start=1)
    ]


def _seeded_profiles_for_region(
    members: Sequence[object],
    roster: Dict[str, dict],
    strength_info: Dict[str, dict],
) -> List[TeamProfile]:
    profiles: List[TeamProfile] = []
    strength_sorted = sorted(
        members,
        key=lambda team: (
            -float(strength_info[team.school]["strength_score"]),
            roster[team.school]["full_form_ranking"]
            if roster[team.school]["full_form_ranking"] is not None
            else 10 ** 9,
            roster[team.school]["points_rank"]
            if roster[team.school]["points_rank"] is not None
            else 10 ** 9,
            team.school,
        ),
    )
    strength_rank_region = {team.school: index for index, team in enumerate(strength_sorted, start=1)}

    for team in members:
        row = roster[team.school]
        entry = strength_info[team.school]
        profiles.append(
            TeamProfile(
                school=team.school,
                team=row["team"],
                city=row["city"],
                assigned=team.assigned,
                team_type=row["team_type"],
                points=row["points"],
                points_rank=row["points_rank"],
                full_form_ranking=row["full_form_ranking"],
                rmul_2026_top4=row["rmul_2026_top4"],
                rmul_2026_top4_cn=row["rmul_2026_top4_cn"],
                detail_2025_cn=row["detail_2025_cn"],
                strength_score=float(entry["strength_score"]),
                full_form_score=float(entry["strength_components"]["full_form_score"]),
                strength_rank_global=entry.get("strength_rank_global"),
                strength_rank_region=strength_rank_region[team.school],
            )
        )

    seeded = sorted(profiles, key=_seed_sort_key)
    out: List[TeamProfile] = []
    for index, profile in enumerate(seeded, start=1):
        tier = "Tier1" if index <= 8 else "Tier2" if index <= 16 else "Remaining"
        out.append(
            TeamProfile(
                **{
                    **profile.__dict__,
                    "seed_rank_region": index,
                    "seed_tier": tier,
                }
            )
        )
    return out


def draw_groups(profiles: Sequence[TeamProfile], rng: random.Random) -> Dict[str, TeamState]:
    tier1 = list(profiles[:8])
    tier2 = list(profiles[8:16])
    remaining = list(profiles[16:])

    slot_profiles: Dict[str, TeamProfile] = {}
    for slot, profile in zip(TIER1_FIXED_SLOTS, tier1):
        slot_profiles[slot] = profile

    rng.shuffle(tier2)
    rng.shuffle(remaining)
    tier2_sub1, tier2_sub2 = tier2[:4], tier2[4:]
    remain_sub1, remain_sub2 = remaining[:8], remaining[8:]
    box_a = tier2_sub1 + remain_sub1
    box_b = tier2_sub2 + remain_sub2
    rng.shuffle(box_a)
    rng.shuffle(box_b)

    for slot, profile in zip(GROUP_FILL_SLOTS["A"], box_a):
        slot_profiles[slot] = profile
    for slot, profile in zip(GROUP_FILL_SLOTS["B"], box_b):
        slot_profiles[slot] = profile

    states: Dict[str, TeamState] = {}
    for slot, profile in slot_profiles.items():
        states[profile.school] = TeamState(profile=profile, group=slot[0], slot=slot)
    return states


def _serialize_draw(states: Dict[str, TeamState]) -> dict:
    output = {"A": [], "B": []}
    for group_key in ("A", "B"):
        group_states = sorted(
            [state for state in states.values() if state.group == group_key],
            key=lambda state: int(state.slot[1:]),
        )
        output[group_key] = [
            {
                "slot": state.slot,
                **_serialize_team_ref(state),
            }
            for state in group_states
        ]
    return output


def _simulate_group(
    group_key: str,
    states: Dict[str, TeamState],
    rng: random.Random,
    *,
    collect_trace: bool = False,
) -> Tuple[List[TeamState], Optional[List[dict]]]:
    state_map = {state.school: state for state in states.values()}
    group_states = [state for state in states.values() if state.group == group_key]
    by_slot = {state.slot: state for state in group_states}
    rounds_trace: List[dict] = []

    for round_index in range(1, 6):
        matches: List[dict] = []
        if round_index == 1:
            pairs = [
                (
                    by_slot[f"{group_key}{left_index}"],
                    by_slot[f"{group_key}{right_index}"],
                )
                for left_index, right_index in ROUND1_PAIRINGS
            ]
        else:
            active = [
                state
                for state in _sort_states(group_states, _current_key, state_map, rng)
                if state.is_active
            ]
            pairs = [(active[index], active[index + 1]) for index in range(0, len(active), 2)]

        for match_number, (left, right) in enumerate(pairs, start=1):
            if not left.is_active or not right.is_active:
                continue
            if collect_trace:
                _, _, card = _play_traced_series(
                    left,
                    right,
                    3,
                    rng,
                    stage=f"{group_key}_swiss_round_{round_index}",
                    match_id=f"{group_key}-R{round_index}-{match_number}",
                    left_status="continue",
                    right_status="continue",
                )
                card["status_after_match"] = {
                    "left": _swiss_status(left),
                    "right": _swiss_status(right),
                }
                matches.append(card)
            else:
                result = _run_series(left, right, 3, rng)
                _ = result

            if left.wins >= 3 and left.terminal_round == 0:
                left.terminal_round = round_index
            if right.wins >= 3 and right.terminal_round == 0:
                right.terminal_round = round_index
            if left.losses >= 3 and left.terminal_round == 0:
                left.terminal_round = round_index
            if right.losses >= 3 and right.terminal_round == 0:
                right.terminal_round = round_index

        if collect_trace:
            rounds_trace.append(
                {
                    "round": round_index,
                    "matches": matches,
                    "standings_after_round": _round_snapshot(group_states, state_map, rng),
                }
            )

    final_ranking = _sort_states(group_states, _final_key, state_map, rng)
    return final_ranking, rounds_trace if collect_trace else None


def _overall_swiss_ranking(states: Sequence[TeamState], rng: random.Random) -> List[TeamState]:
    state_map = {state.school: state for state in states}
    return _sort_states(states, _final_key, state_map, rng)


def _qualification_trace_entry(
    title: str,
    record: str,
    *,
    matches: Optional[List[dict]] = None,
    standings: Optional[List[dict]] = None,
):
    return {
        "title": title,
        "record": record,
        "matches": matches or [],
        "standings": standings or [],
    }


def _decision_payload(title: str, qualification_result: str) -> dict:
    return {
        "qualification_decided_title": title,
        "qualification_decided_result": qualification_result,
        "qualification_decided_text": (
            f"在「{title}」确定{QUALIFICATION_LABELS[qualification_result]}资格"
        ),
    }


def _serialize_decision_standing(
    state: TeamState,
    rank: int,
    state_map: Dict[str, TeamState],
    qualification_result: str,
    decided_title: str,
) -> dict:
    payload = _serialize_standing(state, rank, state_map, qualification_result)
    payload.update(_decision_payload(decided_title, qualification_result))
    return payload


def _simulate_qualification_branch(
    round_of_16_losers: List[TeamState],
    swiss_overall_rank: Dict[str, int],
    state_map: Dict[str, TeamState],
    rng: random.Random,
    *,
    collect_trace: bool,
    national_spots: int,
    revival_spots: int,
) -> dict:
    extra_national = max(0, national_spots - 8)
    extra_revival = revival_spots
    qualification = {"format": "", "columns": []}

    round_0_0_winners: List[TeamState] = []
    round_0_0_losers: List[TeamState] = []
    round_0_0_matches: List[dict] = []
    for match_index, (left_index, right_index) in enumerate(PLACEMENT_ROUND1_PAIRINGS, start=1):
        left = round_of_16_losers[left_index]
        right = round_of_16_losers[right_index]
        if collect_trace:
            winner, loser, card = _play_traced_series(
                left,
                right,
                3,
                rng,
                stage="qualification_0_0",
                match_id=f"Q00-{match_index}",
                left_status="continue",
                right_status="continue",
            )
            card["status_after_match"] = {
                "left": "continue",
                "right": "continue",
            }
            round_0_0_matches.append(card)
        else:
            winner = simulate_series(left, right, 3, rng)
            loser = right if winner.school == left.school else left
        round_0_0_winners.append(winner)
        round_0_0_losers.append(loser)

    round_1_0_winners: List[TeamState] = []
    round_1_0_losers: List[TeamState] = []
    round_1_0_matches: List[dict] = []
    for match_index, (left_index, right_index) in enumerate(QUALIFICATION_CROSS_PAIRINGS, start=1):
        left = round_0_0_winners[left_index]
        right = round_0_0_winners[right_index]
        if collect_trace:
            winner, loser, card = _play_traced_series(
                left,
                right,
                3,
                rng,
                stage="qualification_1_0",
                match_id=f"Q10-{match_index}",
                left_status="continue",
                right_status="continue",
            )
            card["status_after_match"] = {
                "left": "continue",
                "right": "continue",
            }
            round_1_0_matches.append(card)
        else:
            winner = simulate_series(left, right, 3, rng)
            loser = right if winner.school == left.school else left
        round_1_0_winners.append(winner)
        round_1_0_losers.append(loser)

    round_0_1_winners: List[TeamState] = []
    round_0_1_losers: List[TeamState] = []
    round_0_1_matches: List[dict] = []
    for match_index, (left_index, right_index) in enumerate(QUALIFICATION_CROSS_PAIRINGS, start=1):
        left = round_0_0_losers[left_index]
        right = round_0_0_losers[right_index]
        if collect_trace:
            winner, loser, card = _play_traced_series(
                left,
                right,
                3,
                rng,
                stage="qualification_0_1",
                match_id=f"Q01-{match_index}",
                left_status="continue",
                right_status="continue",
            )
            card["status_after_match"] = {
                "left": "continue",
                "right": "continue",
            }
            round_0_1_matches.append(card)
        else:
            winner = simulate_series(left, right, 3, rng)
            loser = right if winner.school == left.school else left
        round_0_1_winners.append(winner)
        round_0_1_losers.append(loser)

    if extra_national == 2 and extra_revival == 4:
        qualification["format"] = "south_10_4"
        national_qualifiers = sorted(
            round_1_0_winners,
            key=lambda team: _branch_sort_key(team, swiss_overall_rank),
        )
        revival_qualifiers = sorted(
            round_1_0_losers + round_0_1_winners,
            key=lambda team: _branch_sort_key(team, swiss_overall_rank),
        )
        eliminated = sorted(
            round_0_1_losers,
            key=lambda team: _branch_sort_key(team, swiss_overall_rank),
        )
        decision_by_school = {
            team.school: _decision_payload("晋级全国赛 2-0", "national")
            for team in national_qualifiers
        }
        decision_by_school.update({
            team.school: _decision_payload("晋级复活赛 1-1", "revival")
            for team in revival_qualifiers
        })

        if collect_trace:
            qualification["columns"] = [
                _qualification_trace_entry("全国赛名额竞争 0-0", "0-0", matches=round_0_0_matches),
                _qualification_trace_entry("全国赛名额竞争 1-0", "1-0", matches=round_1_0_matches),
                _qualification_trace_entry("复活赛名额竞争 0-1", "0-1", matches=round_0_1_matches),
                _qualification_trace_entry(
                    "晋级全国赛 2-0",
                    "2-0",
                    standings=[
                        _serialize_decision_standing(team, 9 + idx, state_map, "national", "晋级全国赛 2-0")
                        for idx, team in enumerate(national_qualifiers)
                    ],
                ),
                _qualification_trace_entry(
                    "晋级复活赛 1-1",
                    "1-1",
                    standings=[
                        _serialize_decision_standing(team, 11 + idx, state_map, "revival", "晋级复活赛 1-1")
                        for idx, team in enumerate(revival_qualifiers)
                    ],
                ),
                _qualification_trace_entry(
                    "淘汰 0-2",
                    "0-2",
                    standings=[
                        _serialize_standing(team, 15 + idx, state_map, "out")
                        for idx, team in enumerate(eliminated)
                    ],
                ),
            ]

        return {
            "qualification": qualification,
            "ranking": national_qualifiers + revival_qualifiers + eliminated,
            "decision_by_school": decision_by_school,
        }

    qualification["format"] = "east_north_9_6"
    round_2_0_matches: List[dict] = []
    if collect_trace:
        national_winner, national_loser, national_card = _play_traced_series(
            round_1_0_winners[0],
            round_1_0_winners[1],
            3,
            rng,
            stage="qualification_2_0",
            match_id="Q20-1",
            left_status="advance",
            right_status="continue",
        )
        national_card["status_after_match"] = {
            "left": "advance" if national_winner.school == round_1_0_winners[0].school else "continue",
            "right": "advance" if national_winner.school == round_1_0_winners[1].school else "continue",
        }
        round_2_0_matches.append(national_card)
    else:
        national_winner = simulate_series(round_1_0_winners[0], round_1_0_winners[1], 3, rng)
        national_loser = round_1_0_winners[1] if national_winner.school == round_1_0_winners[0].school else round_1_0_winners[0]

    direct_revival = sorted(
        round_1_0_losers + round_0_1_winners,
        key=lambda team: _branch_sort_key(team, swiss_overall_rank),
    )
    decision_by_school = {
        team.school: _decision_payload("晋级复活赛 1-1", "revival")
        for team in direct_revival
    }

    round_0_2_matches: List[dict] = []
    if collect_trace:
        last_revival, eliminated_last, round_0_2_card = _play_traced_series(
            round_0_1_losers[0],
            round_0_1_losers[1],
            3,
            rng,
            stage="qualification_0_2",
            match_id="Q02-1",
            left_status="advance",
            right_status="eliminated",
        )
        round_0_2_card["status_after_match"] = {
            "left": "advance" if last_revival.school == round_0_1_losers[0].school else "eliminated",
            "right": "advance" if last_revival.school == round_0_1_losers[1].school else "eliminated",
        }
        round_0_2_matches.append(round_0_2_card)
    else:
        last_revival = simulate_series(round_0_1_losers[0], round_0_1_losers[1], 3, rng)
        eliminated_last = round_0_1_losers[1] if last_revival.school == round_0_1_losers[0].school else round_0_1_losers[0]
    decision_by_school[national_winner.school] = _decision_payload("晋级全国赛 3-0", "national")
    decision_by_school[national_loser.school] = _decision_payload("晋级复活赛 2-1", "revival")
    decision_by_school[last_revival.school] = _decision_payload("晋级复活赛 1-2", "revival")

    if collect_trace:
        qualification["columns"] = [
            _qualification_trace_entry("全国赛名额竞争 0-0", "0-0", matches=round_0_0_matches),
            _qualification_trace_entry("全国赛名额竞争 1-0", "1-0", matches=round_1_0_matches),
            _qualification_trace_entry("复活赛名额竞争 0-1", "0-1", matches=round_0_1_matches),
            _qualification_trace_entry("全国赛名额竞争 2-0", "2-0", matches=round_2_0_matches),
            _qualification_trace_entry(
                "晋级复活赛 1-1",
                "1-1",
                standings=[
                    _serialize_decision_standing(team, 11 + idx, state_map, "revival", "晋级复活赛 1-1")
                    for idx, team in enumerate(direct_revival)
                ],
            ),
            _qualification_trace_entry("复活赛名额竞争 0-2", "0-2", matches=round_0_2_matches),
            _qualification_trace_entry(
                "晋级全国赛 3-0",
                "3-0",
                standings=[_serialize_decision_standing(national_winner, 9, state_map, "national", "晋级全国赛 3-0")],
            ),
            _qualification_trace_entry(
                "晋级复活赛 2-1",
                "2-1",
                standings=[_serialize_decision_standing(national_loser, 10, state_map, "revival", "晋级复活赛 2-1")],
            ),
            _qualification_trace_entry(
                "晋级复活赛 1-2",
                "1-2",
                standings=[_serialize_decision_standing(last_revival, 15, state_map, "revival", "晋级复活赛 1-2")],
            ),
            _qualification_trace_entry(
                "淘汰 0-3",
                "0-3",
                standings=[_serialize_standing(eliminated_last, 16, state_map, "out")],
            ),
        ]

    return {
        "qualification": qualification,
        "ranking": [national_winner, national_loser] + direct_revival + [last_revival, eliminated_last],
        "decision_by_school": decision_by_school,
    }


def _sample_seed(base_seed: int, region_index: int, sample_index: int) -> int:
    return (
        base_seed
        ^ ((region_index + 1) * 0x9E3779B97F4A7C15)
        ^ ((sample_index + 1) * 0xD1B54A32D192ED03)
    )


def _simulate_region_once(
    profiles: Sequence[TeamProfile],
    rng: random.Random,
    *,
    collect_trace: bool = False,
    national_spots: int = 0,
    revival_spots: int = 0,
    sample_id: str = "",
    sample_seed: str = "",
) -> dict:
    states = draw_groups(profiles, rng)
    state_map = {state.school: state for state in states.values()}
    draw = _serialize_draw(states) if collect_trace else None

    group_a, group_a_rounds = _simulate_group("A", states, rng, collect_trace=collect_trace)
    group_b, group_b_rounds = _simulate_group("B", states, rng, collect_trace=collect_trace)

    qualified_slots = {f"A{index}": team for index, team in enumerate(group_a[:8], start=1)}
    qualified_slots.update({f"B{index}": team for index, team in enumerate(group_b[:8], start=1)})
    swiss_overall = _overall_swiss_ranking(group_a + group_b, rng)
    swiss_overall_rank = {team.school: rank for rank, team in enumerate(swiss_overall, start=1)}

    knockout = {
        "round_of_16": [],
        "quarterfinal": [],
        "semifinal": [],
        "third_place": None,
        "championship": None,
        "qualification": {
            "format": "",
            "columns": [],
        },
    }

    round_of_16_losers: List[TeamState] = []
    quarterfinal_winners: List[TeamState] = []
    for match_index, (left_slot, right_slot) in enumerate(ROUND_OF_16_PAIRINGS, start=1):
        left = qualified_slots[left_slot]
        right = qualified_slots[right_slot]
        if collect_trace:
            winner, loser, card = _play_traced_series(
                left,
                right,
                3,
                rng,
                stage="round_of_16",
                match_id=f"R16-{match_index}",
                left_status="continue",
                right_status="continue",
            )
            card["status_after_match"] = {
                "left": "advance" if winner.school == left.school else "continue",
                "right": "advance" if winner.school == right.school else "continue",
            }
            knockout["round_of_16"].append(card)
        else:
            winner = simulate_series(left, right, 3, rng)
            loser = right if winner.school == left.school else left
        quarterfinal_winners.append(winner)
        round_of_16_losers.append(loser)

    quarterfinal_losers: List[TeamState] = []
    semifinalists: List[TeamState] = []
    for match_index, (left_index, right_index) in enumerate(QUARTERFINAL_PAIRINGS, start=1):
        left = quarterfinal_winners[left_index]
        right = quarterfinal_winners[right_index]
        if collect_trace:
            winner, loser, card = _play_traced_series(
                left,
                right,
                3,
                rng,
                stage="quarterfinal",
                match_id=f"QF-{match_index}",
                left_status="continue",
                right_status="continue",
            )
            card["status_after_match"] = {
                "left": "advance" if winner.school == left.school else "eliminated",
                "right": "advance" if winner.school == right.school else "eliminated",
            }
            knockout["quarterfinal"].append(card)
        else:
            winner = simulate_series(left, right, 3, rng)
            loser = right if winner.school == left.school else left
        semifinalists.append(winner)
        quarterfinal_losers.append(loser)

    semifinal_losers: List[TeamState] = []
    finalists: List[TeamState] = []
    for match_index, (left, right) in enumerate(((semifinalists[0], semifinalists[2]), (semifinalists[1], semifinalists[3])), start=1):
        if collect_trace:
            winner, loser, card = _play_traced_series(
                left,
                right,
                3,
                rng,
                stage="semifinal",
                match_id=f"SF-{match_index}",
                left_status="continue",
                right_status="continue",
            )
            card["status_after_match"] = {
                "left": "advance" if winner.school == left.school else "continue",
                "right": "advance" if winner.school == right.school else "continue",
            }
            knockout["semifinal"].append(card)
        else:
            winner = simulate_series(left, right, 3, rng)
            loser = right if winner.school == left.school else left
        finalists.append(winner)
        semifinal_losers.append(loser)

    if collect_trace:
        third_winner, fourth_place, card = _play_traced_series(
            semifinal_losers[0],
            semifinal_losers[1],
            5,
            rng,
            stage="third_place",
            match_id="TP-1",
            left_status="advance",
            right_status="eliminated",
        )
        card["status_after_match"] = {
            "left": "advance" if third_winner.school == semifinal_losers[0].school else "eliminated",
            "right": "advance" if third_winner.school == semifinal_losers[1].school else "eliminated",
        }
        knockout["third_place"] = card
    else:
        third_winner = simulate_series(semifinal_losers[0], semifinal_losers[1], 5, rng)
        fourth_place = semifinal_losers[1] if third_winner.school == semifinal_losers[0].school else semifinal_losers[0]
    if not collect_trace:
        fourth_place = semifinal_losers[1] if third_winner.school == semifinal_losers[0].school else semifinal_losers[0]

    if collect_trace:
        champion, runner_up, card = _play_traced_series(
            finalists[0],
            finalists[1],
            5,
            rng,
            stage="championship",
            match_id="F-1",
            left_status="advance",
            right_status="eliminated",
        )
        card["status_after_match"] = {
            "left": "advance" if champion.school == finalists[0].school else "eliminated",
            "right": "advance" if champion.school == finalists[1].school else "eliminated",
        }
        knockout["championship"] = card
    else:
        champion = simulate_series(finalists[0], finalists[1], 5, rng)
        runner_up = finalists[1] if champion.school == finalists[0].school else finalists[0]
    if not collect_trace:
        runner_up = finalists[1] if champion.school == finalists[0].school else finalists[0]

    decision_by_school = {
        winner.school: _decision_payload("16强晋级全国赛", "national")
        for winner in quarterfinal_winners
    }

    qualification_branch = _simulate_qualification_branch(
        round_of_16_losers,
        swiss_overall_rank,
        state_map,
        rng,
        collect_trace=collect_trace,
        national_spots=national_spots,
        revival_spots=revival_spots,
    )
    knockout["qualification"] = qualification_branch["qualification"]
    qualification_ranking = qualification_branch["ranking"]
    decision_by_school.update(qualification_branch["decision_by_school"])

    final_order: List[TeamState] = [champion, runner_up, third_winner, fourth_place]
    final_order.extend(sorted(quarterfinal_losers, key=lambda team: swiss_overall_rank[team.school]))
    final_order.extend(qualification_ranking)

    qualified_school_set = {team.school for team in group_a[:8] + group_b[:8]}
    remaining = [team for team in swiss_overall if team.school not in qualified_school_set]
    final_order.extend(remaining)
    final_ranks = {team.school: rank for rank, team in enumerate(final_order, start=1)}

    if not collect_trace:
        return {"final_ranks": final_ranks}

    group_final = {
        "A": [_serialize_standing(team, rank, state_map) for rank, team in enumerate(group_a, start=1)],
        "B": [_serialize_standing(team, rank, state_map) for rank, team in enumerate(group_b, start=1)],
    }
    final_ranking = []
    for rank, team in enumerate(final_order, start=1):
        qualification_result = _qualification_result(rank, national_spots, revival_spots)
        payload = _serialize_standing(team, rank, state_map, qualification_result)
        if team.school in decision_by_school:
            payload.update(decision_by_school[team.school])
        final_ranking.append(payload)

    return {
        "sample_id": sample_id,
        "sample_seed": sample_seed,
        "region": profiles[0].assigned if profiles else "",
        "draw": draw,
        "swiss": {
            "group_a_rounds": group_a_rounds or [],
            "group_b_rounds": group_b_rounds or [],
        },
        "group_final": group_final,
        "knockout": knockout,
        "final_ranking": final_ranking,
        "final_ranks": final_ranks,
    }


def _seed_basis(region_profiles: Dict[str, List[TeamProfile]]) -> str:
    chunks = []
    for region in _seeded_order(region_profiles):
        profiles = sorted(region_profiles[region], key=lambda profile: profile.school)
        for profile in profiles:
            chunks.append(
                "|".join(
                    [
                        region,
                        profile.school,
                        str(profile.points),
                        str(profile.points_rank),
                        str(profile.full_form_ranking),
                        f"{profile.strength_score:.4f}",
                    ]
                )
            )
    return "\n".join(chunks)


def _summarize_region(
    region: str,
    schools: Sequence[dict],
    national_spots: int,
    revival_spots: int,
    default_school: str,
) -> dict:
    ordered = sorted(
        schools,
        key=lambda item: (
            -item["national_probability"],
            -item["revival_probability"],
            item["average_rank"],
            item["seed_rank_region"],
            item["school"],
        ),
    )
    revival_cut = national_spots + revival_spots

    def _pick(index: int) -> Optional[dict]:
        if 0 <= index < len(ordered):
            entry = ordered[index]
            return {
                "school": entry["school"],
                "team": entry["team"],
                "seed_rank_region": entry["seed_rank_region"],
                "seed_tier": entry["seed_tier"],
                "national_probability": entry["national_probability"],
                "revival_probability": entry["revival_probability"],
                "average_rank": entry["average_rank"],
            }
        return None

    return {
        "region": region,
        "school_count": len(ordered),
        "national_spots": national_spots,
        "revival_spots": revival_spots,
        "default_school": default_school if any(item["school"] == default_school for item in ordered) else ordered[0]["school"],
        "bubble": {
            "last_national": _pick(national_spots - 1),
            "first_revival": _pick(national_spots),
            "last_revival": _pick(revival_cut - 1),
            "first_out": _pick(revival_cut),
        },
    }


def build_swiss_simulation(
    region_members: Dict[str, Sequence[object]],
    strength_info: Dict[str, dict],
    roster: Dict[str, dict],
    national_by_region: Dict[str, int],
    revival_by_region: Dict[str, int],
    *,
    updated_at_utc: str = "",
    updated_at_cst: str = "",
    default_school: str = DEFAULT_SCHOOL,
    iterations: Optional[int] = None,
    include_samples: bool = True,
    sample_pool_size: Optional[int] = None,
) -> dict:
    run_count = max(200, iterations if iterations is not None else DEFAULT_ITERATIONS)
    sample_count = max(1, sample_pool_size if sample_pool_size is not None else DEFAULT_SAMPLE_POOL) if include_samples else 0
    region_profiles = {
        region: _seeded_profiles_for_region(region_members[region], roster, strength_info)
        for region in _seeded_order(region_members)
    }

    seed_basis = _seed_basis(region_profiles)
    seed_hash = hashlib.sha256(seed_basis.encode("utf-8")).hexdigest()
    base_seed = int(seed_hash[:16], 16)

    aggregates: Dict[str, TeamAggregate] = {}
    schools_by_region: Dict[str, List[str]] = defaultdict(list)
    for region in _seeded_order(region_profiles):
        for profile in region_profiles[region]:
            aggregates[profile.school] = TeamAggregate(profile=profile)
            schools_by_region[region].append(profile.school)

    for region_index, region in enumerate(_seeded_order(region_profiles)):
        region_rng = random.Random(base_seed ^ ((region_index + 1) * 0x9E3779B97F4A7C15))
        for _ in range(run_count):
            result = _simulate_region_once(region_profiles[region], region_rng)
            national_cut = national_by_region[region]
            revival_cut = national_cut + revival_by_region[region]
            for school, final_rank in result["final_ranks"].items():
                aggregate = aggregates[school]
                aggregate.total_rank += final_rank
                if final_rank <= national_cut:
                    aggregate.national_count += 1
                    aggregate.revival_or_better_count += 1
                elif final_rank <= revival_cut:
                    aggregate.revival_or_better_count += 1
                    aggregate.revival_only_count += 1
                else:
                    aggregate.out_count += 1
                if final_rank <= 8:
                    aggregate.top8_count += 1
                if final_rank <= 4:
                    aggregate.top4_count += 1
                aggregate.stage_counts[_stage_from_rank(final_rank, national_cut, revival_by_region[region])] += 1

    school_rows: List[dict] = []
    for region in _seeded_order(region_profiles):
        for school in schools_by_region[region]:
            aggregate = aggregates[school]
            stage_probabilities = {
                stage: _round(count / run_count, 4)
                for stage, count in aggregate.stage_counts.items()
            }
            for stage in STAGE_LABELS:
                stage_probabilities.setdefault(stage, 0.0)
            most_likely_stage = max(
                STAGE_LABELS,
                key=lambda stage: (stage_probabilities[stage], STAGE_LABELS[stage]),
            )
            school_rows.append(
                {
                    "school": aggregate.profile.school,
                    "team": aggregate.profile.team,
                    "city": aggregate.profile.city,
                    "team_type": aggregate.profile.team_type,
                    "assigned": aggregate.profile.assigned,
                    "points": aggregate.profile.points,
                    "rank": aggregate.profile.points_rank,
                    "full_form_ranking": aggregate.profile.full_form_ranking,
                    "rmul_2026_top4": aggregate.profile.rmul_2026_top4,
                    "rmul_2026_top4_cn": aggregate.profile.rmul_2026_top4_cn,
                    "detail_2025_cn": aggregate.profile.detail_2025_cn,
                    "strength_score": _round(aggregate.profile.strength_score),
                    "strength_rank_global": aggregate.profile.strength_rank_global,
                    "strength_rank_region": aggregate.profile.strength_rank_region,
                    "seed_rank_region": aggregate.profile.seed_rank_region,
                    "seed_tier": aggregate.profile.seed_tier,
                    "national_probability": _round(aggregate.national_count / run_count, 4),
                    "revival_probability": _round(aggregate.revival_or_better_count / run_count, 4),
                    "revival_only_probability": _round(aggregate.revival_only_count / run_count, 4),
                    "out_probability": _round(aggregate.out_count / run_count, 4),
                    "top8_probability": _round(aggregate.top8_count / run_count, 4),
                    "top4_probability": _round(aggregate.top4_count / run_count, 4),
                    "average_rank": _round(aggregate.total_rank / run_count, 3),
                    "most_likely_stage": most_likely_stage,
                    "most_likely_stage_label": STAGE_LABELS[most_likely_stage],
                    "most_likely_stage_probability": stage_probabilities[most_likely_stage],
                    "stage_probabilities": stage_probabilities,
                }
            )

    school_rows.sort(
        key=lambda item: (
            REGION_ORDER.get(item["assigned"], 9),
            item["average_rank"],
            item["seed_rank_region"],
            item["school"],
        )
    )

    regions = [
        _summarize_region(
            region,
            [row for row in school_rows if row["assigned"] == region],
            national_by_region[region],
            revival_by_region[region],
            default_school,
        )
        for region in _seeded_order(region_profiles)
    ]
    default_region = next(
        (row["assigned"] for row in school_rows if row["school"] == default_school),
        school_rows[0]["assigned"],
    )
    default_school_by_region = {item["region"]: item["default_school"] for item in regions}

    sample_regions: List[dict] = []
    if include_samples:
        for region_index, region in enumerate(_seeded_order(region_profiles)):
            samples: List[dict] = []
            for sample_index in range(sample_count):
                seed_value = _sample_seed(base_seed, region_index, sample_index)
                sample = _simulate_region_once(
                    region_profiles[region],
                    random.Random(seed_value),
                    collect_trace=True,
                    national_spots=national_by_region[region],
                    revival_spots=revival_by_region[region],
                    sample_id=f"{REGION_SLUG[region]}-{sample_index + 1:02d}",
                    sample_seed=f"{seed_value & ((1 << 64) - 1):016x}",
                )
                samples.append(sample)
            sample_regions.append(
                {
                    "region": region,
                    "default_school": default_school_by_region[region],
                    "samples": samples,
                }
            )

    return {
        "default_school": default_school if default_school in aggregates else school_rows[0]["school"],
        "default_region": default_region,
        "model_note": (
            "输入为最新调剂后的 32 支赛区名单，抽签与 2025 场序对齐；"
            "基础分取当前综合实力分，弱者仅在面对更强对手时获得轻微 boost。"
        ),
        "meta": {
            "model_version": MODEL_VERSION,
            "updated_at_utc": updated_at_utc,
            "updated_at_cst": updated_at_cst,
            "iterations": run_count,
            "sample_pool_size": sample_count,
            "quota_mode": "rmuc_2026_current_region_quota",
            "seed_rule": "points desc -> points_rank asc -> full_form_ranking asc -> strength_score desc -> school",
            "draw_rule": "2025_region_swiss_layout",
            "boost_rule": {
                "full_form_scale": FULL_FORM_BOOST_SCALE,
                "rmul_weight": RMUL_BOOST_WEIGHT,
                "cap": "full_form_score + 10",
            },
            "single_game_model": {
                "gap_clip": MAX_GAP_CLIP,
                "logistic_scale": LOGISTIC_SCALE,
            },
            "seed_basis_hash": seed_hash,
            "seed_basis_label": "assigned_region + seeded team profile snapshot",
            "sample_seed_label": "seed_basis_hash + region index + sample index",
        },
        "regions": regions,
        "schools": school_rows,
        "sample_regions": sample_regions,
    }
