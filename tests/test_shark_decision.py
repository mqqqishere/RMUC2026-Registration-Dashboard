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
        cls.panel = shark_decision.build_decision_panel([], cls.distances)

    def test_options_follow_region_order(self):
        self.assertEqual(
            [option["shark_volunteer"] for option in self.panel["options"]],
            list(allocator.REGIONS),
        )

    def test_current_snapshot_options_match_qualification_model(self):
        for option in self.panel["options"]:
            teams, _ = shark_decision._build_projected_teams(
                self.roster,
                self.distances,
                {},
                option["shark_volunteer"],
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
                option["qualification_status"],
                qualification_panel["qualification_status"][shark_decision.SHARK_SCHOOL],
            )
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

    def test_decision_components_sum_to_decision_score(self):
        for option in self.panel["options"]:
            components = option["decision_components"]
            subtotal = sum(
                value for key, value in components.items()
                if key != "total"
            )
            self.assertAlmostEqual(subtotal, components["total"], places=4)
            self.assertAlmostEqual(option["decision_score"], components["total"], places=4)

    def test_select_recommendation_prefers_higher_decision_score(self):
        options = [
            {
                "shark_volunteer": "南部",
                "decision_score": 88.0,
                "qualification_status": "national",
                "advance_margin": 1,
                "national_margin": 1,
                "status": "volunteer",
            },
            {
                "shark_volunteer": "东部",
                "decision_score": 95.0,
                "qualification_status": "revival",
                "advance_margin": 3,
                "national_margin": -2,
                "status": "volunteer",
            },
            {
                "shark_volunteer": "北部",
                "decision_score": 80.0,
                "qualification_status": "national",
                "advance_margin": 0,
                "national_margin": 0,
                "status": "transfer",
            },
        ]

        selected = shark_decision.select_recommendation_option(options)
        self.assertEqual(selected["shark_volunteer"], "东部")


class DashboardPayloadTest(unittest.TestCase):
    def test_build_payload_includes_shark_decision(self):
        with mock.patch.object(build_dashboard.allocator, "load_teams_live", return_value=[]):
            payload = build_dashboard.build_payload()

        self.assertIn("regions", payload)
        self.assertIn("strength_ranking", payload)
        self.assertIn("advancement_panel", payload)
        self.assertIn("shark_decision", payload)
        self.assertIsNotNone(payload["shark_decision"])
        self.assertEqual(
            [option["shark_volunteer"] for option in payload["shark_decision"]["options"]],
            list(allocator.REGIONS),
        )
        self.assertNotIn("sensitivity", payload["shark_decision"])


class FrontendTemplateSmokeTest(unittest.TestCase):
    def test_index_contains_shark_decision_panel_hooks(self):
        with open(os.path.join(ROOT, "docs", "index.html"), encoding="utf-8") as f:
            html = f.read()

        self.assertIn('data-tab="shark-decision"', html)
        self.assertIn('id="panel-shark-decision"', html)
        self.assertIn("function renderSharkDecision(data)", html)
        self.assertIn("renderSharkDecision(data);", html)
        self.assertNotIn("decision.sensitivity.length", html)
        self.assertNotIn("情景矩阵", html)


if __name__ == "__main__":
    unittest.main()
