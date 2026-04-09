#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import allocator
import qualification


def _team_sort_key(team, strength_info):
    entry = strength_info[team.school]
    return (
        -entry["strength_score"],
        entry["full_form_ranking"] if entry["full_form_ranking"] is not None else 10 ** 9,
        entry["points_rank"] if entry["points_rank"] is not None else 10 ** 9,
        team.school,
    )


class NationalSpotsTest(unittest.TestCase):
    def test_requires_more_than_four_top16(self):
        roster = {}
        region_members = {region: [] for region in allocator.REGIONS}

        counts = {"南部": 4, "东部": 5, "北部": 7}
        idx = 0
        for region, count in counts.items():
            for _ in range(count):
                idx += 1
                school = f"s{idx}"
                roster[school] = {"detail_2025": "top_16"}
                region_members[region].append(SimpleNamespace(school=school))

        top16, floating, national = qualification.compute_national_spots(region_members, roster)
        self.assertEqual(top16, counts)
        self.assertEqual(floating, {"南部": 0, "东部": 2, "北部": 2})
        self.assertEqual(national, {"南部": 8, "东部": 10, "北部": 10})

    def test_max_remainder_tie_breaks_by_region_order(self):
        roster = {}
        region_members = {region: [] for region in allocator.REGIONS}

        for region in allocator.REGIONS:
            for i in range(5):
                school = f"{region}-{i}"
                roster[school] = {"detail_2025": "top_16"}
                region_members[region].append(SimpleNamespace(school=school))

        _, floating, national = qualification.compute_national_spots(region_members, roster)
        self.assertEqual(floating, {"南部": 2, "东部": 1, "北部": 1})
        self.assertEqual(national, {"南部": 10, "东部": 9, "北部": 9})


class StrengthModelTest(unittest.TestCase):
    def test_history_bonus_weights_are_heavier_and_flatter(self):
        roster = {
            "champion": {
                "school": "champion",
                "team": "C",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "champion",
                "detail_2025_cn": "冠军",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
            "top_32": {
                "school": "top_32",
                "team": "T",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "top_32",
                "detail_2025_cn": "三十二强",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
            "top_16": {
                "school": "top_16",
                "team": "S",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "top_16",
                "detail_2025_cn": "十六强",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
        }
        table, _ = qualification.compute_strength_table(roster)
        self.assertEqual(table["champion"]["strength_components"]["history_bonus"], 40.0)
        self.assertEqual(table["top_16"]["strength_components"]["history_bonus"], 20.0)
        self.assertEqual(table["top_32"]["strength_components"]["history_bonus"], 16.0)

    def test_rmul_bonus_weights_are_halved(self):
        roster = {
            "champion": {
                "school": "champion",
                "team": "C",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "",
                "detail_2025_cn": "—",
                "rmul_2026_top4": "champion",
                "rmul_2026_top4_cn": "冠军",
            },
            "runner_up": {
                "school": "runner_up",
                "team": "R",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "",
                "detail_2025_cn": "—",
                "rmul_2026_top4": "runner_up",
                "rmul_2026_top4_cn": "亚军",
            },
            "fourth_place": {
                "school": "fourth_place",
                "team": "F",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "",
                "detail_2025_cn": "—",
                "rmul_2026_top4": "fourth_place",
                "rmul_2026_top4_cn": "殿军",
            },
        }
        table, _ = qualification.compute_strength_table(roster)
        self.assertEqual(table["champion"]["strength_components"]["rmul_bonus"], 17.0)
        self.assertEqual(table["runner_up"]["strength_components"]["rmul_bonus"], 15.5)
        self.assertEqual(table["fourth_place"]["strength_components"]["rmul_bonus"], 12.5)

    def test_history_priority_then_rmul_then_points(self):
        roster = {
            "base": {
                "school": "base",
                "team": "B",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "",
                "detail_2025_cn": "—",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
            "rmul": {
                "school": "rmul",
                "team": "R",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "",
                "detail_2025_cn": "—",
                "rmul_2026_top4": "champion",
                "rmul_2026_top4_cn": "冠军",
            },
            "history": {
                "school": "history",
                "team": "H",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": None,
                "detail_2025": "champion",
                "detail_2025_cn": "冠军",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
            "points": {
                "school": "points",
                "team": "P",
                "city": "A",
                "team_type": "甲级",
                "points_rank": 1,
                "points": 25.0,
                "full_form_ranking": None,
                "detail_2025": "",
                "detail_2025_cn": "—",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
        }
        table, _ = qualification.compute_strength_table(roster)
        self.assertGreater(
            table["history"]["strength_score"] - table["base"]["strength_score"],
            table["rmul"]["strength_score"] - table["base"]["strength_score"],
        )
        self.assertGreater(
            table["rmul"]["strength_score"] - table["base"]["strength_score"],
            table["points"]["strength_score"] - table["base"]["strength_score"],
        )

    def test_full_form_long_tail_decay_and_non_top_history_zero(self):
        roster = {
            "front": {
                "school": "front",
                "team": "F",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": 5,
                "detail_2025": "division",
                "detail_2025_cn": "分区赛",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
            "tail": {
                "school": "tail",
                "team": "T",
                "city": "A",
                "team_type": "甲级",
                "points_rank": None,
                "points": None,
                "full_form_ranking": 80,
                "detail_2025": "revival",
                "detail_2025_cn": "复活赛",
                "rmul_2026_top4": "",
                "rmul_2026_top4_cn": "—",
            },
        }
        table, _ = qualification.compute_strength_table(roster)
        self.assertGreater(
            table["front"]["strength_components"]["full_form_score"],
            table["tail"]["strength_components"]["full_form_score"] * 10,
        )
        self.assertLess(table["tail"]["strength_components"]["full_form_score"], 2.0)
        self.assertEqual(table["front"]["strength_components"]["history_bonus"], 0.0)
        self.assertEqual(table["tail"]["strength_components"]["history_bonus"], 8.0)

    def test_compute_region_strength_stats(self):
        region_rankings = {
            "南部": [
                SimpleNamespace(school="s1"),
                SimpleNamespace(school="s2"),
                SimpleNamespace(school="s3"),
            ],
            "东部": [
                SimpleNamespace(school="e1"),
                SimpleNamespace(school="e2"),
            ],
            "北部": [],
        }
        strength_info = {
            "s1": {"strength_score": 10.0},
            "s2": {"strength_score": 20.0},
            "s3": {"strength_score": 30.0},
            "e1": {"strength_score": 40.0},
            "e2": {"strength_score": 50.0},
        }

        stats = qualification.compute_region_strength_stats(region_rankings, strength_info)

        self.assertEqual(stats["南部"]["team_count"], 3)
        self.assertEqual(stats["南部"]["avg_strength_score"], 20.0)
        self.assertEqual(stats["南部"]["median_strength_score"], 20.0)
        self.assertAlmostEqual(stats["南部"]["stdev_strength_score"], 8.165, places=3)
        self.assertEqual(stats["南部"]["top8_avg_strength_score"], 20.0)
        self.assertEqual(stats["南部"]["q1_strength_score"], 15.0)
        self.assertEqual(stats["南部"]["q3_strength_score"], 25.0)
        self.assertEqual(stats["南部"]["iqr_strength_score"], 10.0)

        self.assertEqual(stats["东部"]["avg_strength_score"], 45.0)
        self.assertEqual(stats["东部"]["median_strength_score"], 45.0)
        self.assertEqual(stats["东部"]["stdev_strength_score"], 5.0)
        self.assertEqual(stats["北部"]["team_count"], 0)
        self.assertEqual(stats["北部"]["avg_strength_score"], 0.0)


class ProjectionAndQualificationTest(unittest.TestCase):
    def test_partial_live_submission_still_projects_full_roster(self):
        roster = qualification.load_roster()
        distances = allocator.load_distances()
        live = {
            "上海交通大学": "北部",
            "东北大学": "南部",
        }

        teams, volunteer_source = qualification.build_projected_teams(roster, distances, live)
        by_school = {team.school: team for team in teams}

        self.assertEqual(len(teams), len(roster))
        self.assertEqual(volunteer_source["上海交通大学"], "submitted")
        self.assertEqual(volunteer_source["东北大学"], "submitted")
        self.assertEqual(volunteer_source["长沙理工大学"], "host_default")
        self.assertEqual(volunteer_source["华南理工大学"], "nearest_estimate")
        self.assertEqual(by_school["上海交通大学"].volunteer, "北部")
        self.assertEqual(by_school["长沙理工大学"].volunteer, "南部")
        self.assertEqual(by_school["华南理工大学"].volunteer, "南部")

    def test_revival_selection_is_balanced_and_capped(self):
        roster = qualification.load_roster()
        distances = allocator.load_distances()
        teams, _ = qualification.build_projected_teams(roster, distances, {})
        result = allocator.allocate(teams, verbose=False)
        strength_info, _ = qualification.compute_strength_table(roster)
        panel = qualification.assign_qualifications(result, strength_info, roster)

        self.assertEqual(sum(panel["national_by_region"].values()), 28)
        self.assertEqual(sum(panel["revival_by_region"].values()), 16)
        totals = {
            region: panel["national_by_region"][region] + panel["revival_by_region"][region]
            for region in allocator.REGIONS
        }
        for region in allocator.REGIONS:
            self.assertLessEqual(
                totals[region],
                qualification.REVIVAL_SOFT_MAX_ADVANCING_PER_REGION,
            )
        self.assertLessEqual(max(totals.values()) - min(totals.values()), 1)

    def test_revival_selector_uses_soft_cap_even_if_one_region_is_stronger(self):
        region_rankings = {
            "南部": [
                SimpleNamespace(school=f"s{i}", assigned="南部") for i in range(1, 19)
            ],
            "东部": [
                SimpleNamespace(school=f"e{i}", assigned="东部") for i in range(1, 19)
            ],
            "北部": [
                SimpleNamespace(school=f"n{i}", assigned="北部") for i in range(1, 19)
            ],
        }
        strength_info = {}
        for idx, team in enumerate(region_rankings["南部"], start=1):
            strength_info[team.school] = {
                "strength_score": 20.0 - idx,
                "full_form_ranking": idx,
                "points_rank": idx,
            }
        for idx, team in enumerate(region_rankings["东部"], start=1):
            strength_info[team.school] = {
                "strength_score": 40.0 - idx,
                "full_form_ranking": idx,
                "points_rank": idx,
            }
        for idx, team in enumerate(region_rankings["北部"], start=1):
            strength_info[team.school] = {
                "strength_score": 80.0 - idx,
                "full_form_ranking": idx,
                "points_rank": idx,
            }

        national_by_region = {"南部": 8, "东部": 10, "北部": 10}
        revival_by_region, selected, _, _, soft_cap = qualification._select_revival_teams(
            region_rankings, strength_info, national_by_region
        )

        self.assertEqual(len(selected), qualification.REVIVAL_TOTAL)
        self.assertEqual(soft_cap["东部"], 5)
        self.assertEqual(soft_cap["北部"], 5)
        self.assertLessEqual(revival_by_region["东部"], 5)
        self.assertLessEqual(revival_by_region["北部"], 5)
        self.assertLessEqual(
            national_by_region["北部"] + revival_by_region["北部"],
            qualification.REVIVAL_SOFT_MAX_ADVANCING_PER_REGION,
        )


if __name__ == "__main__":
    unittest.main()
