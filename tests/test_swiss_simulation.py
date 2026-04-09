#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import random
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import allocator
import qualification
import swiss_simulation


def _profile(index: int) -> swiss_simulation.TeamProfile:
    return swiss_simulation.TeamProfile(
        school=f"s{index}",
        team=f"T{index}",
        city="A",
        assigned="南部",
        team_type="甲级",
        points=100.0 - index,
        points_rank=index,
        full_form_ranking=index,
        rmul_2026_top4="",
        rmul_2026_top4_cn="—",
        detail_2025_cn="—",
        strength_score=80.0 - index,
        full_form_score=max(0.0, 15.0 - index * 0.1),
        strength_rank_global=index,
        strength_rank_region=index,
        seed_rank_region=index,
        seed_tier="Tier1" if index <= 8 else "Tier2" if index <= 16 else "Remaining",
    )


class SwissSimulationRulesTest(unittest.TestCase):
    def test_seed_sorting_prefers_points_then_rank_then_full_form(self):
        members = [
            SimpleNamespace(school="alpha", assigned="南部"),
            SimpleNamespace(school="beta", assigned="南部"),
            SimpleNamespace(school="gamma", assigned="南部"),
        ]
        roster = {
            "alpha": {
                "team": "A",
                "city": "A",
                "team_type": "甲级",
                "points": 12.0,
                "points_rank": 40,
                "full_form_ranking": 30,
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
                "detail_2025_cn": "—",
            },
            "beta": {
                "team": "B",
                "city": "A",
                "team_type": "甲级",
                "points": 12.0,
                "points_rank": 35,
                "full_form_ranking": 80,
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
                "detail_2025_cn": "—",
            },
            "gamma": {
                "team": "C",
                "city": "A",
                "team_type": "甲级",
                "points": 11.5,
                "points_rank": 1,
                "full_form_ranking": 1,
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
                "detail_2025_cn": "—",
            },
        }
        strength_info = {
            "alpha": {
                "strength_score": 20.0,
                "strength_components": {"full_form_score": 10.0},
                "strength_rank_global": 1,
            },
            "beta": {
                "strength_score": 18.0,
                "strength_components": {"full_form_score": 5.0},
                "strength_rank_global": 2,
            },
            "gamma": {
                "strength_score": 25.0,
                "strength_components": {"full_form_score": 12.0},
                "strength_rank_global": 3,
            },
        }

        seeded = swiss_simulation._seeded_profiles_for_region(members, roster, strength_info)
        self.assertEqual([profile.school for profile in seeded], ["beta", "alpha", "gamma"])
        self.assertEqual([profile.seed_rank_region for profile in seeded], [1, 2, 3])

    def test_draw_keeps_tier1_in_fixed_slots(self):
        profiles = [_profile(index) for index in range(1, 33)]
        states = swiss_simulation.draw_groups(profiles, random.Random(7))
        by_slot = {state.slot: state.school for state in states.values()}
        self.assertEqual([by_slot[slot] for slot in swiss_simulation.TIER1_FIXED_SLOTS], [f"s{i}" for i in range(1, 9)])
        self.assertEqual(sum(1 for state in states.values() if state.group == "A"), 16)
        self.assertEqual(sum(1 for state in states.values() if state.group == "B"), 16)

    def test_pairing_constants_match_2025_layout(self):
        self.assertEqual(
            swiss_simulation.ROUND1_PAIRINGS,
            ((1, 9), (2, 10), (11, 3), (12, 4), (5, 13), (6, 14), (15, 7), (16, 8)),
        )
        self.assertEqual(
            swiss_simulation.ROUND_OF_16_PAIRINGS,
            (("B1", "A8"), ("B5", "A4"), ("A7", "B2"), ("A3", "B6"), ("A6", "B3"), ("A2", "B7"), ("B4", "A5"), ("B8", "A1")),
        )

    def test_stage_cutoffs_follow_2026_rank_bands(self):
        self.assertEqual(swiss_simulation._stage_from_rank(1, 10, 4), "champion")
        self.assertEqual(swiss_simulation._stage_from_rank(2, 10, 4), "runner_up")
        self.assertEqual(swiss_simulation._stage_from_rank(4, 10, 4), "top4")
        self.assertEqual(swiss_simulation._stage_from_rank(8, 10, 4), "top8")
        self.assertEqual(swiss_simulation._stage_from_rank(10, 10, 4), "national")
        self.assertEqual(swiss_simulation._stage_from_rank(14, 10, 4), "revival")
        self.assertEqual(swiss_simulation._stage_from_rank(15, 10, 4), "out")


class SwissSimulationBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.roster = qualification.load_roster()
        cls.distances = allocator.load_distances()
        cls.teams, _ = qualification.build_projected_teams(cls.roster, cls.distances, {})
        cls.result = allocator.allocate(cls.teams, verbose=False)
        cls.strength_info, _ = qualification.compute_strength_table(cls.roster)
        cls.panel = qualification.assign_qualifications(cls.result, cls.strength_info, cls.roster)
        cls.current_national = {"南部": 10, "东部": 9, "北部": 9}
        cls.current_revival = {"南部": 4, "东部": 6, "北部": 6}

    def test_build_is_deterministic_and_probabilities_are_valid(self):
        with mock.patch.object(swiss_simulation, "DEFAULT_SAMPLE_POOL", 4):
            first = swiss_simulation.build_swiss_simulation(
                self.result.regions,
                self.strength_info,
                self.roster,
                self.current_national,
                self.current_revival,
                iterations=240,
            )
            second = swiss_simulation.build_swiss_simulation(
                self.result.regions,
                self.strength_info,
                self.roster,
                self.current_national,
                self.current_revival,
                iterations=240,
            )

        self.assertEqual(first["meta"]["seed_basis_hash"], second["meta"]["seed_basis_hash"])
        self.assertEqual(first["schools"], second["schools"])
        self.assertEqual(first["sample_regions"], second["sample_regions"])
        self.assertEqual(first["meta"]["sample_pool_size"], 4)

        for row in first["schools"]:
            self.assertLessEqual(row["national_probability"], row["revival_probability"] + 1e-9)
            self.assertAlmostEqual(
                row["national_probability"] + row["revival_only_probability"] + row["out_probability"],
                1.0,
                places=3,
            )

    def test_probability_only_mode_matches_school_probabilities_and_skips_samples(self):
        with mock.patch.object(swiss_simulation, "DEFAULT_SAMPLE_POOL", 4):
            full = swiss_simulation.build_swiss_simulation(
                self.result.regions,
                self.strength_info,
                self.roster,
                self.current_national,
                self.current_revival,
                iterations=180,
            )
            lite = swiss_simulation.build_swiss_simulation(
                self.result.regions,
                self.strength_info,
                self.roster,
                self.current_national,
                self.current_revival,
                iterations=180,
                include_samples=False,
            )

        self.assertEqual(full["schools"], lite["schools"])
        self.assertEqual(lite["sample_regions"], [])
        self.assertEqual(lite["meta"]["sample_pool_size"], 0)

    def test_clear_strong_team_has_higher_national_probability_than_clear_weak_team(self):
        with mock.patch.object(swiss_simulation, "DEFAULT_SAMPLE_POOL", 4):
            simulation = swiss_simulation.build_swiss_simulation(
                self.result.regions,
                self.strength_info,
                self.roster,
                self.current_national,
                self.current_revival,
                iterations=320,
            )
        north = [row for row in simulation["schools"] if row["assigned"] == "北部"]
        north.sort(key=lambda row: row["seed_rank_region"])
        self.assertGreaterEqual(north[0]["national_probability"], north[-1]["national_probability"])

    def test_sample_pool_contains_complete_and_unique_brackets(self):
        with mock.patch.object(swiss_simulation, "DEFAULT_SAMPLE_POOL", 3):
            simulation = swiss_simulation.build_swiss_simulation(
                self.result.regions,
                self.strength_info,
                self.roster,
                self.current_national,
                self.current_revival,
                iterations=220,
            )

        self.assertEqual(simulation["default_region"], "北部")
        self.assertEqual(len(simulation["sample_regions"]), 3)
        for region_entry in simulation["sample_regions"]:
            self.assertEqual(len(region_entry["samples"]), 3)
            for sample in region_entry["samples"]:
                draw_a = sample["draw"]["A"]
                draw_b = sample["draw"]["B"]
                all_slots = {entry["slot"] for entry in draw_a + draw_b}
                all_schools = {entry["school"] for entry in draw_a + draw_b}
                self.assertEqual(len(draw_a), 16)
                self.assertEqual(len(draw_b), 16)
                self.assertEqual(len(all_slots), 32)
                self.assertEqual(len(all_schools), 32)
                self.assertEqual(
                    {entry["slot"] for entry in draw_a},
                    {f"A{i}" for i in range(1, 17)},
                )
                self.assertEqual(
                    {entry["slot"] for entry in draw_b},
                    {f"B{i}" for i in range(1, 17)},
                )
                self.assertEqual(
                    [entry["slot"] for entry in draw_a if entry["seed_tier"] == "Tier1"][:4],
                    ["A1", "A3", "A5", "A7"],
                )
                self.assertEqual(
                    [entry["slot"] for entry in draw_b if entry["seed_tier"] == "Tier1"][:4],
                    ["B1", "B3", "B5", "B7"],
                )
                self.assertEqual(len(sample["final_ranking"]), 32)
                self.assertEqual(
                    {entry["rank"] for entry in sample["final_ranking"]},
                    set(range(1, 33)),
                )
                qualification = sample["knockout"]["qualification"]
                if region_entry["region"] == "南部":
                    self.assertEqual(qualification["format"], "south_10_4")
                    self.assertEqual(
                        [column["title"] for column in qualification["columns"]],
                        [
                            "全国赛名额竞争 0-0",
                            "全国赛名额竞争 1-0",
                            "复活赛名额竞争 0-1",
                            "晋级全国赛 2-0",
                            "晋级复活赛 1-1",
                            "淘汰 0-2",
                        ],
                    )
                else:
                    self.assertEqual(qualification["format"], "east_north_9_6")
                    self.assertEqual(
                        [column["title"] for column in qualification["columns"]],
                        [
                            "全国赛名额竞争 0-0",
                            "全国赛名额竞争 1-0",
                            "复活赛名额竞争 0-1",
                            "全国赛名额竞争 2-0",
                            "晋级复活赛 1-1",
                            "复活赛名额竞争 0-2",
                            "晋级全国赛 3-0",
                            "晋级复活赛 2-1",
                            "晋级复活赛 1-2",
                            "淘汰 0-3",
                        ],
                    )
                national_badges = [
                    entry for entry in sample["final_ranking"]
                    if entry.get("earned_badge") == "national"
                ]
                revival_badges = [
                    entry for entry in sample["final_ranking"]
                    if entry.get("earned_badge") == "revival"
                ]
                self.assertEqual(
                    len(national_badges),
                    self.current_national[region_entry["region"]],
                )
                self.assertEqual(
                    len(revival_badges),
                    self.current_revival[region_entry["region"]],
                )
                self.assertTrue(
                    all(entry["qualification_result"] == "national" for entry in national_badges)
                )
                self.assertTrue(
                    all(entry["qualification_result"] == "revival" for entry in revival_badges)
                )
                entrants = {
                    match["left"]["school"]
                    for match in sample["knockout"]["round_of_16"]
                } | {
                    match["right"]["school"]
                    for match in sample["knockout"]["round_of_16"]
                }
                expected_entrants = {
                    entry["school"] for entry in sample["group_final"]["A"][:8]
                } | {
                    entry["school"] for entry in sample["group_final"]["B"][:8]
                }
                self.assertEqual(
                    entrants,
                    expected_entrants,
                )


if __name__ == "__main__":
    unittest.main()
