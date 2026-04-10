#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, ROOT)
sys.path.insert(0, SCRIPTS)

import allocator
import build_dashboard
import qualification
import shark_decision


class SharkDecisionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.distances = allocator.load_distances()
        cls.roster = shark_decision.load_roster()
        cls.strength_info, _ = qualification.compute_strength_table(cls.roster)
        cls._iter_patcher = mock.patch.object(shark_decision.S, "DEFAULT_ITERATIONS", 120)
        cls._iter_patcher.start()
        cls.panel = shark_decision.build_decision_panel([], cls.distances)

    @classmethod
    def tearDownClass(cls):
        cls._iter_patcher.stop()

    def test_default_school_is_shark(self):
        self.assertEqual(self.panel["default_school"], shark_decision.SHARK_SCHOOL)

    def test_panel_covers_all_schools_with_three_ordered_options(self):
        self.assertEqual(self.panel["school_count"], len(self.roster))
        self.assertEqual(set(self.panel["schools"]), set(self.roster))
        for school, entry in self.panel["schools"].items():
            self.assertEqual(entry["target"]["school"], school)
            self.assertEqual(
                [option["volunteer"] for option in entry["options"]],
                list(allocator.REGIONS),
            )

    def test_current_snapshot_options_match_region_and_quota_model_for_shark(self):
        shark = self.panel["schools"][shark_decision.SHARK_SCHOOL]
        for option in shark["options"]:
            teams, _ = shark_decision._build_projected_teams(
                self.roster,
                self.distances,
                {},
                target_school=shark_decision.SHARK_SCHOOL,
                volunteer_override=option["volunteer"],
            )
            allocation = allocator.allocate(teams, verbose=False)
            qualification_panel = qualification.assign_qualifications(
                allocation, self.strength_info, self.roster
            )
            shark_team = next(
                team for team in allocation.teams
                if team.school == shark_decision.SHARK_SCHOOL
            )

            self.assertEqual(option["final_region"], shark_team.assigned)
            self.assertEqual(option["status"], shark_team.status)
            self.assertEqual(
                option["strength_rank_region"],
                qualification_panel["strength_rank_region"][shark_decision.SHARK_SCHOOL],
            )
            self.assertEqual(
                option["national_spots"],
                qualification_panel["national_by_region"][shark_team.assigned],
            )
            self.assertEqual(
                option["revival_spots"],
                qualification_panel["revival_by_region"][shark_team.assigned],
            )
            self.assertIn("revival_probability", option)
            self.assertIn("national_probability", option)

    def test_decision_components_sum_to_decision_score(self):
        for entry in self.panel["schools"].values():
            for option in entry["options"]:
                components = option["decision_components"]
                subtotal = sum(
                    value for key, value in components.items()
                    if key != "total"
                )
                self.assertAlmostEqual(subtotal, components["total"], places=4)
                self.assertAlmostEqual(option["decision_score"], components["total"], places=4)

    def test_select_recommendation_prefers_higher_revival_probability(self):
        options = [
            {
                "volunteer": "南部",
                "decision_score": 88.0,
                "revival_probability": 0.74,
                "national_probability": 0.35,
                "average_rank": 11.2,
                "status": "volunteer",
            },
            {
                "volunteer": "东部",
                "decision_score": 95.0,
                "revival_probability": 0.81,
                "national_probability": 0.21,
                "average_rank": 12.8,
                "status": "volunteer",
            },
            {
                "volunteer": "北部",
                "decision_score": 80.0,
                "revival_probability": 0.81,
                "national_probability": 0.18,
                "average_rank": 13.5,
                "status": "transfer",
            },
        ]

        selected = shark_decision.select_recommendation_option(options)
        self.assertEqual(selected["volunteer"], "东部")


class DashboardPayloadTest(unittest.TestCase):
    def test_choice_alignment_hint_distinguishes_adjusted_and_suboptimal(self):
        adjusted = build_dashboard._choice_alignment_hint({
            "observed_state": {
                "live_volunteer": "北部",
                "projected_region": "东部",
            },
            "recommendation": {
                "volunteer": "东部",
                "final_region": "东部",
            },
        })
        suboptimal = build_dashboard._choice_alignment_hint({
            "observed_state": {
                "live_volunteer": "南部",
                "projected_region": "南部",
            },
            "recommendation": {
                "volunteer": "东部",
                "final_region": "东部",
            },
        })

        self.assertEqual(adjusted["choice_alignment"], "adjusted_to_optimal")
        self.assertEqual(adjusted["choice_alignment_label"], "调后最优")
        self.assertIn("模型最优赛区 东部", adjusted["choice_alignment_detail"])
        self.assertEqual(suboptimal["choice_alignment"], "suboptimal")
        self.assertEqual(suboptimal["choice_alignment_label"], "未按最优")
        self.assertIn("模型最优解为 东部", suboptimal["choice_alignment_detail"])

    def test_build_bundle_splits_lazy_artifacts(self):
        with mock.patch.object(build_dashboard.allocator, "load_teams_live", return_value=[]), \
             mock.patch.object(build_dashboard.swiss_simulation, "DEFAULT_SAMPLE_POOL", 2), \
             mock.patch.object(build_dashboard.shark_decision.S, "DEFAULT_ITERATIONS", 120):
            bundle = build_dashboard.build_dashboard_bundle()

        payload = bundle["payload"]
        volunteer_decision = bundle["volunteer_decision"]
        global_swiss_samples = bundle["global_swiss_samples"]

        self.assertIn("regions", payload)
        self.assertIn("strength_ranking", payload)
        self.assertIn("advancement_panel", payload)
        self.assertIn("shark_decision", payload)
        self.assertIn("swiss_simulation", payload)
        self.assertEqual(payload["shark_decision"]["default_school"], shark_decision.SHARK_SCHOOL)
        self.assertEqual(payload["shark_decision"]["lazy_path"], "volunteer_decision.json")
        self.assertEqual(payload["swiss_simulation"]["default_school"], shark_decision.SHARK_SCHOOL)
        self.assertEqual(len(payload["swiss_simulation"]["schools"]), len(payload["teams"]))
        self.assertNotIn("sample_regions", payload["swiss_simulation"])
        self.assertEqual(payload["swiss_simulation"]["samples_path"], "global_swiss_samples.json")
        self.assertEqual(payload["swiss_simulation"]["meta"]["sample_pool_size"], 2)
        shark_team = next(team for team in payload["teams"] if team["school"] == shark_decision.SHARK_SCHOOL)
        self.assertIn("swiss_revival_probability", shark_team)
        self.assertIn("swiss_rank_region", shark_team)
        self.assertIn("choice_alignment", shark_team)

        self.assertEqual(volunteer_decision["default_school"], shark_decision.SHARK_SCHOOL)
        self.assertEqual(len(volunteer_decision["schools"]), len(payload["teams"]))
        self.assertEqual(
            [option["volunteer"] for option in volunteer_decision["schools"][shark_decision.SHARK_SCHOOL]["options"]],
            list(allocator.REGIONS),
        )
        self.assertIn("revival_probability", volunteer_decision["schools"][shark_decision.SHARK_SCHOOL]["recommendation"])
        self.assertIn("national_probability", volunteer_decision["schools"][shark_decision.SHARK_SCHOOL]["recommendation"])

        self.assertEqual(global_swiss_samples["default_region"], payload["swiss_simulation"]["default_region"])
        self.assertEqual(len(global_swiss_samples["sample_regions"]), 3)
        self.assertTrue(all(len(entry["samples"]) == 2 for entry in global_swiss_samples["sample_regions"]))

    def test_build_payload_returns_lightweight_main_json(self):
        with mock.patch.object(build_dashboard.allocator, "load_teams_live", return_value=[]), \
             mock.patch.object(build_dashboard.swiss_simulation, "DEFAULT_SAMPLE_POOL", 2), \
             mock.patch.object(build_dashboard.shark_decision.S, "DEFAULT_ITERATIONS", 120):
            payload = build_dashboard.build_payload()

        self.assertNotIn("sample_regions", payload["swiss_simulation"])
        self.assertEqual(payload["shark_decision"]["lazy_path"], "volunteer_decision.json")


class FrontendTemplateSmokeTest(unittest.TestCase):
    def test_index_contains_lazy_loaded_decision_and_global_swiss_hooks(self):
        with open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8") as f:
            html = f.read()

        self.assertIn('data-tab="shark-decision"', html)
        self.assertIn(">志愿决策<", html)
        self.assertIn('id="panel-shark-decision"', html)
        self.assertIn("function renderSharkDecision(data)", html)
        self.assertIn("function ensureVolunteerDecisionLoaded(data)", html)
        self.assertIn("function ensureGlobalSwissSamplesLoaded(data)", html)
        self.assertIn('id="decisionSchoolSelect"', html)
        self.assertIn("void loadVolunteerDecisionStage()", html)
        self.assertIn("void loadGlobalSwissStage()", html)
        self.assertIn('id="panel-swiss-simulation"', html)
        self.assertIn("function renderSwissSimulation(data)", html)
        self.assertIn('data-tab="global-swiss"', html)
        self.assertIn('id="panel-global-swiss"', html)
        self.assertIn("function renderGlobalSwissStage(data)", html)
        self.assertIn('id="globalSwissFlowViewport"', html)
        self.assertIn('id="globalSwissZoomRange"', html)
        self.assertIn('id="globalSwissRandomBtn"', html)
        self.assertIn("资格确定：", html)
        self.assertIn("瑞士轮 · 复活及以上概率", html)
        self.assertIn("调后最优", html)
        self.assertIn("未按最优", html)
        self.assertIn("function currentQualificationStatus(entry)", html)
        self.assertIn("function currentQualificationLabel(entry)", html)
        self.assertIn('id="advancementMetricSelect"', html)
        self.assertIn("纯实力分分析", html)
        self.assertIn("瑞士轮 · 国赛概率", html)
        self.assertIn("function currentAdvancementAnalysis()", html)
        self.assertIn("function advancementDisplayState(entry)", html)


if __name__ == "__main__":
    unittest.main()
