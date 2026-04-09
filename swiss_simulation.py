#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RMUC 2026 瑞士轮模拟器。

基于当前调剂后的 32 支赛区名单，按 2025 赛制编排复刻抽签、瑞士轮与淘汰赛，
并用 Monte Carlo 近似给出 2026 名额口径下的国赛/复活赛概率。
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


DEFAULT_ITERATIONS = int(os.environ.get("SWISS_SIM_ITERATIONS", "1400"))
DEFAULT_SCHOOL = "江南大学霞客湾校区"
MODEL_VERSION = "swiss-sim-v1.0"
REGION_ORDER = {"南部": 0, "东部": 1, "北部": 2}

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


def _apply_series_metrics(
    state: TeamState,
    opponent: TeamState,
    match_won: bool,
    game_wins: int,
    game_losses: int,
    base_margins: Iterable[float],
    outpost_margins: Iterable[float],
    damages: Iterable[float],
):
    state.opponents.append(opponent.school)
    state.wins += 1 if match_won else 0
    state.losses += 0 if match_won else 1
    state.game_wins += game_wins
    state.game_losses += game_losses
    game_count = game_wins + game_losses
    state.total_games += game_count
    state.base_margin_sum += sum(base_margins)
    state.outpost_margin_sum += sum(outpost_margins)
    state.damage_sum += sum(damages)


def simulate_series(left: TeamState, right: TeamState, best_of: int, rng: random.Random) -> TeamState:
    wins_needed = best_of // 2 + 1
    left_wins = 0
    right_wins = 0
    left_base_margins: List[float] = []
    right_base_margins: List[float] = []
    left_outpost_margins: List[float] = []
    right_outpost_margins: List[float] = []
    left_damages: List[float] = []
    right_damages: List[float] = []
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
            left_base_margins.append(base_margin)
            right_base_margins.append(-base_margin)
            left_outpost_margins.append(outpost_margin)
            right_outpost_margins.append(-outpost_margin)
        else:
            right_wins += 1
            left_base_margins.append(-base_margin)
            right_base_margins.append(base_margin)
            left_outpost_margins.append(-outpost_margin)
            right_outpost_margins.append(outpost_margin)

        left_damages.append(_team_damage(left.profile, gap_abs, left_won, rng))
        right_damages.append(_team_damage(right.profile, gap_abs, not left_won, rng))

    _apply_series_metrics(
        left,
        right,
        left_wins > right_wins,
        left_wins,
        right_wins,
        left_base_margins,
        left_outpost_margins,
        left_damages,
    )
    _apply_series_metrics(
        right,
        left,
        right_wins > left_wins,
        right_wins,
        left_wins,
        right_base_margins,
        right_outpost_margins,
        right_damages,
    )
    return left if left_wins > right_wins else right


def _opponent_sos(state: TeamState, state_map: Dict[str, TeamState]) -> int:
    return sum(
        state_map[school].game_wins - state_map[school].game_losses
        for school in state.opponents
    )


def _avg(value: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return value / count


def _active_key(state: TeamState, state_map: Dict[str, TeamState]) -> Tuple[int, int, float, float, float]:
    return (
        -state.wins,
        state.losses,
        -_opponent_sos(state, state_map),
        -_avg(state.base_margin_sum, state.total_games),
        -_avg(state.outpost_margin_sum, state.total_games),
        -_avg(state.damage_sum, state.total_games),
    )


def _final_key(state: TeamState, state_map: Dict[str, TeamState]) -> Tuple[int, int, float, float, float]:
    return (
        RECORD_BUCKET[state.record],
        state.terminal_round if state.wins == 3 else 0,
        -_opponent_sos(state, state_map),
        -_avg(state.base_margin_sum, state.total_games),
        -_avg(state.outpost_margin_sum, state.total_games),
        -_avg(state.damage_sum, state.total_games),
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
    buckets: Dict[Tuple[int, ...], List[TeamState]] = defaultdict(list)
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


def _group_states_for_round(
    states: Sequence[TeamState],
    state_map: Dict[str, TeamState],
    rng: random.Random,
) -> List[TeamState]:
    active = [state for state in states if state.is_active]
    return _sort_states(active, _active_key, state_map, rng)


def _group_final_ranking(
    states: Sequence[TeamState],
    state_map: Dict[str, TeamState],
    rng: random.Random,
) -> List[TeamState]:
    return _sort_states(states, _final_key, state_map, rng)


def _slot_number(slot: str) -> int:
    return int(slot[1:])


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
    final_profiles: List[TeamProfile] = []
    for index, profile in enumerate(seeded, start=1):
        if index <= 8:
            tier = "Tier1"
        elif index <= 16:
            tier = "Tier2"
        else:
            tier = "Remaining"
        final_profiles.append(
            TeamProfile(
                **{
                    **profile.__dict__,
                    "seed_rank_region": index,
                    "seed_tier": tier,
                }
            )
        )
    return final_profiles


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


def _simulate_group(group_key: str, states: Dict[str, TeamState], rng: random.Random) -> List[TeamState]:
    state_map = {state.school: state for state in states.values()}
    group_states = [state for state in states.values() if state.group == group_key]
    by_slot = {state.slot: state for state in group_states}

    for round_index in range(1, 6):
        if round_index == 1:
            pairs = [
                (
                    by_slot[f"{group_key}{left_index}"],
                    by_slot[f"{group_key}{right_index}"],
                )
                for left_index, right_index in ROUND1_PAIRINGS
            ]
        else:
            active = _group_states_for_round(group_states, state_map, rng)
            pairs = [
                (active[index], active[index + 1])
                for index in range(0, len(active), 2)
            ]

        for left, right in pairs:
            if not left.is_active or not right.is_active:
                continue
            winner = simulate_series(left, right, 3, rng)
            loser = right if winner.school == left.school else left
            if winner.wins >= 3 and winner.terminal_round == 0:
                winner.terminal_round = round_index
            if loser.losses >= 3 and loser.terminal_round == 0:
                loser.terminal_round = round_index

        if not any(state.is_active for state in group_states):
            break

    return _group_final_ranking(group_states, state_map, rng)


def _overall_swiss_ranking(states: Sequence[TeamState], rng: random.Random) -> List[TeamState]:
    state_map = {state.school: state for state in states}
    return _sort_states(states, _final_key, state_map, rng)


def _simulate_region_once(profiles: Sequence[TeamProfile], rng: random.Random) -> Dict[str, int]:
    states = draw_groups(profiles, rng)
    group_a = _simulate_group("A", states, rng)
    group_b = _simulate_group("B", states, rng)

    qualified_slots = {f"A{index}": team for index, team in enumerate(group_a[:8], start=1)}
    qualified_slots.update({f"B{index}": team for index, team in enumerate(group_b[:8], start=1)})
    swiss_overall = _overall_swiss_ranking(group_a + group_b, rng)
    swiss_overall_rank = {team.school: rank for rank, team in enumerate(swiss_overall, start=1)}

    round_of_16_losers: List[TeamState] = []
    quarterfinal_winners: List[TeamState] = []
    for left_slot, right_slot in ROUND_OF_16_PAIRINGS:
        left = qualified_slots[left_slot]
        right = qualified_slots[right_slot]
        winner = simulate_series(left, right, 3, rng)
        loser = right if winner.school == left.school else left
        quarterfinal_winners.append(winner)
        round_of_16_losers.append(loser)

    quarterfinal_losers: List[TeamState] = []
    semifinalists: List[TeamState] = []
    for left_index, right_index in QUARTERFINAL_PAIRINGS:
        left = quarterfinal_winners[left_index]
        right = quarterfinal_winners[right_index]
        winner = simulate_series(left, right, 3, rng)
        loser = right if winner.school == left.school else left
        semifinalists.append(winner)
        quarterfinal_losers.append(loser)

    semifinal_losers: List[TeamState] = []
    finalists: List[TeamState] = []
    for left, right in ((semifinalists[0], semifinalists[2]), (semifinalists[1], semifinalists[3])):
        winner = simulate_series(left, right, 3, rng)
        loser = right if winner.school == left.school else left
        finalists.append(winner)
        semifinal_losers.append(loser)

    third_winner = simulate_series(semifinal_losers[0], semifinal_losers[1], 5, rng)
    fourth_place = semifinal_losers[1] if third_winner.school == semifinal_losers[0].school else semifinal_losers[0]
    champion = simulate_series(finalists[0], finalists[1], 5, rng)
    runner_up = finalists[1] if champion.school == finalists[0].school else finalists[0]

    placement_round1_losers: List[TeamState] = []
    placement_round2_candidates: List[TeamState] = []
    for left_index, right_index in PLACEMENT_ROUND1_PAIRINGS:
        left = round_of_16_losers[left_index]
        right = round_of_16_losers[right_index]
        winner = simulate_series(left, right, 3, rng)
        loser = right if winner.school == left.school else left
        placement_round2_candidates.append(winner)
        placement_round1_losers.append(loser)

    placement_round2_winners: List[TeamState] = []
    placement_round2_losers: List[TeamState] = []
    for left_index, right_index in PLACEMENT_ROUND2_PAIRINGS:
        left = placement_round2_candidates[left_index]
        right = placement_round2_candidates[right_index]
        winner = simulate_series(left, right, 3, rng)
        loser = right if winner.school == left.school else left
        placement_round2_winners.append(winner)
        placement_round2_losers.append(loser)

    final_order: List[TeamState] = [champion, runner_up, third_winner, fourth_place]
    final_order.extend(sorted(quarterfinal_losers, key=lambda team: swiss_overall_rank[team.school]))
    final_order.extend(sorted(placement_round2_winners, key=lambda team: swiss_overall_rank[team.school]))
    final_order.extend(sorted(placement_round2_losers, key=lambda team: swiss_overall_rank[team.school]))
    final_order.extend(sorted(placement_round1_losers, key=lambda team: swiss_overall_rank[team.school]))

    qualified_school_set = {team.school for team in group_a[:8] + group_b[:8]}
    remaining = [team for team in swiss_overall if team.school not in qualified_school_set]
    final_order.extend(remaining)

    return {team.school: rank for rank, team in enumerate(final_order, start=1)}


def _seed_basis(region_profiles: Dict[str, List[TeamProfile]]) -> str:
    chunks = []
    for region in sorted(region_profiles, key=lambda item: REGION_ORDER.get(item, 9)):
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
) -> dict:
    run_count = max(200, iterations if iterations is not None else DEFAULT_ITERATIONS)
    region_profiles = {
        region: _seeded_profiles_for_region(region_members[region], roster, strength_info)
        for region in sorted(region_members, key=lambda item: REGION_ORDER.get(item, 9))
    }
    seed_basis = _seed_basis(region_profiles)
    seed_hash = hashlib.sha256(seed_basis.encode("utf-8")).hexdigest()
    base_seed = int(seed_hash[:16], 16)

    aggregates: Dict[str, TeamAggregate] = {}
    schools_by_region: Dict[str, List[str]] = defaultdict(list)
    for region in sorted(region_profiles, key=lambda item: REGION_ORDER.get(item, 9)):
        for profile in region_profiles[region]:
            aggregates[profile.school] = TeamAggregate(profile=profile)
            schools_by_region[region].append(profile.school)

    for region_index, region in enumerate(sorted(region_profiles, key=lambda item: REGION_ORDER.get(item, 9))):
        region_rng = random.Random(base_seed ^ ((region_index + 1) * 0x9E3779B97F4A7C15))
        for _ in range(run_count):
            result = _simulate_region_once(region_profiles[region], region_rng)
            national_cut = national_by_region[region]
            revival_cut = national_cut + revival_by_region[region]
            for school, final_rank in result.items():
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
    for region in sorted(region_profiles, key=lambda item: REGION_ORDER.get(item, 9)):
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
        for region in sorted(region_profiles, key=lambda item: REGION_ORDER.get(item, 9))
    ]

    return {
        "default_school": default_school if default_school in aggregates else school_rows[0]["school"],
        "model_note": (
            "输入为最新调剂后的 32 支赛区名单，抽签与 2025 场序对齐；"
            "基础分取当前综合实力分，弱者仅在面对更强对手时获得轻微 boost。"
        ),
        "meta": {
            "model_version": MODEL_VERSION,
            "updated_at_utc": updated_at_utc,
            "updated_at_cst": updated_at_cst,
            "iterations": run_count,
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
        },
        "regions": regions,
        "schools": school_rows,
    }
