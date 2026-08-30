from django.test import TestCase

from business_intel.models import BusinessLocationCell
from business_intel.engines.competition import compute_competition_score
from business_intel.engines.anchor import compute_anchor_score


class CompetitionEngineTests(TestCase):
    def test_no_competitors_scores_high(self):
        cell = BusinessLocationCell.objects.create(
            geohash="testcell1", center_latitude=-1.15, center_longitude=36.96,
            category_search_results={"pharmacy": {"results": []}},
        )
        score, evidence = compute_competition_score(cell, "pharmacy")
        self.assertGreaterEqual(score, 80)
        self.assertTrue(evidence)

    def test_many_strong_competitors_scores_low(self):
        results = [{"rating": 4.5, "userRatingCount": 100} for _ in range(8)]
        cell = BusinessLocationCell.objects.create(
            geohash="testcell2", center_latitude=-1.15, center_longitude=36.96,
            category_search_results={"pharmacy": {"results": results}},
        )
        score, _ = compute_competition_score(cell, "pharmacy")
        self.assertLess(score, 40)


class AnchorEngineTests(TestCase):
    def test_no_anchors_scores_low(self):
        cell = BusinessLocationCell.objects.create(
            geohash="testcell3", center_latitude=-1.15, center_longitude=36.96,
        )
        score, evidence = compute_anchor_score(cell)
        self.assertLessEqual(score, 30)
        self.assertTrue(evidence)

    def test_university_nearby_scores_higher(self):
        cell = BusinessLocationCell.objects.create(
            geohash="testcell4", center_latitude=-1.15, center_longitude=36.96,
            nearby_anchors=[{"name": "Test University", "category": "university", "distance_m": 300}],
        )
        score, _ = compute_anchor_score(cell)
        self.assertGreater(score, 30)
