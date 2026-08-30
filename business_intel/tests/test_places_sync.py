"""
business_intel/tests/test_places_sync.py

Tests sync_places_to_business_entities -- the function that closes the
gap between search_places_category()'s real Google Places results and
everything downstream that queries BusinessEntity (engines.competition,
in particular). Before this function existed, nothing ever wrote to
BusinessEntity at all, so compute_competition_score() silently returned
"zero competitors" for every location, everywhere.

This exact scenario (a pharmacy search returning two open competitors
and one permanently-closed one) was run against a real Django + sqlite
DB during development and independently verified to produce the correct
haversine-filtered, status-filtered result set competition.py's own
query expects -- these are the same assertions, formalized as a
repeatable test.

Run with: python manage.py test business_intel.tests.test_places_sync
"""
from decimal import Decimal

from django.test import TestCase

from business_intel.models import (
    BusinessLocationCell, BusinessEntity, BusinessSnapshot,
    CategoryEnrichmentLog, compute_geohash,
)
from business_intel.google_client import sync_places_to_business_entities


FAKE_PLACES_RESPONSE = [
    {
        "id": "ChIJ_pharmacy_1",
        "displayName": {"text": "Goodlife Pharmacy Westgate"},
        "location": {"latitude": -1.1503, "longitude": 36.9615},
        "rating": 4.3,
        "userRatingCount": 212,
        "businessStatus": "OPERATIONAL",
    },
    {
        "id": "ChIJ_pharmacy_2",
        "displayName": {"text": "Corner Chemist"},
        "location": {"latitude": -1.1510, "longitude": 36.9598},
        "rating": None,
        "userRatingCount": 0,
        "businessStatus": "OPERATIONAL",
    },
    {
        "id": "ChIJ_pharmacy_3_closed",
        "displayName": {"text": "Defunct Pharmacy"},
        "location": {"latitude": -1.1499, "longitude": 36.9601},
        "rating": 3.9,
        "userRatingCount": 40,
        "businessStatus": "CLOSED_PERMANENTLY",
    },
]


class SyncPlacesToBusinessEntitiesTests(TestCase):
    def setUp(self):
        lat, lng = Decimal("-1.15"), Decimal("36.96")
        self.cell = BusinessLocationCell.objects.create(
            geohash=compute_geohash(lat, lng), center_latitude=lat, center_longitude=lng,
        )

    def test_first_sync_creates_entities_and_snapshots(self):
        synced = sync_places_to_business_entities(self.cell, "pharmacy", FAKE_PLACES_RESPONSE)
        self.assertEqual(len(synced), 3)
        self.assertEqual(BusinessEntity.objects.count(), 3)
        self.assertEqual(BusinessSnapshot.objects.count(), 3)

        goodlife = BusinessEntity.objects.get(place_id="ChIJ_pharmacy_1")
        self.assertEqual(goodlife.name, "Goodlife Pharmacy Westgate")
        self.assertEqual(goodlife.current_status, "operational")
        self.assertEqual(float(goodlife.current_rating), 4.3)
        self.assertEqual(goodlife.current_review_count, 212)
        self.assertEqual(goodlife.first_observed_at, goodlife.last_observed_at)

    def test_closed_permanently_status_maps_correctly(self):
        sync_places_to_business_entities(self.cell, "pharmacy", FAKE_PLACES_RESPONSE)
        closed = BusinessEntity.objects.get(place_id="ChIJ_pharmacy_3_closed")
        self.assertEqual(closed.current_status, "closed_permanently")

    def test_resync_is_idempotent_and_tracks_trend(self):
        sync_places_to_business_entities(self.cell, "pharmacy", FAKE_PLACES_RESPONSE)
        first_observed = BusinessEntity.objects.get(place_id="ChIJ_pharmacy_1").first_observed_at

        updated = list(FAKE_PLACES_RESPONSE)
        updated[0] = {**FAKE_PLACES_RESPONSE[0], "rating": 4.5, "userRatingCount": 240}
        sync_places_to_business_entities(self.cell, "pharmacy", updated)

        self.assertEqual(BusinessEntity.objects.count(), 3, "re-sync must not create duplicates")
        self.assertEqual(BusinessSnapshot.objects.count(), 6, "each sync appends snapshots, never overwrites")

        goodlife = BusinessEntity.objects.get(place_id="ChIJ_pharmacy_1")
        self.assertEqual(float(goodlife.current_rating), 4.5)
        self.assertEqual(goodlife.current_review_count, 240)
        self.assertEqual(goodlife.first_observed_at, first_observed, "first_observed_at must never move")

    def test_has_category_data_distinguishes_unsearched_from_zero_results(self):
        sync_places_to_business_entities(self.cell, "pharmacy", FAKE_PLACES_RESPONSE)
        self.assertTrue(self.cell.has_category_data("pharmacy"))
        self.assertFalse(self.cell.has_category_data("gym"), "a never-searched category must report False")

    def test_malformed_entries_are_skipped_not_fatal(self):
        malformed = [
            {"id": "no_location", "displayName": {"text": "Bad Entry"}, "businessStatus": "OPERATIONAL"},
            {"displayName": {"text": "No ID Entry"}, "location": {"latitude": -1.1, "longitude": 36.9}},
            FAKE_PLACES_RESPONSE[0],
        ]
        synced = sync_places_to_business_entities(self.cell, "pharmacy", malformed)
        self.assertEqual(len(synced), 1)

    def test_competition_engine_query_finds_synced_operational_competitors_only(self):
        """This is the actual question that started this fix: 'how do we
        know there's a competitor within 50m'. Reproduces
        engines.competition.compute_competition_score's exact query
        (category + operational status + haversine) against data this
        function produced, and confirms the closed competitor is
        correctly excluded."""
        import math
        sync_places_to_business_entities(self.cell, "pharmacy", FAKE_PLACES_RESPONSE)

        def haversine_m(lat1, lon1, lat2, lon2):
            R = 6371000.0
            phi1, phi2 = math.radians(lat1), math.radians(lat2)
            dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
            a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
            return 2 * R * math.asin(math.sqrt(a))

        qs = BusinessEntity.objects.filter(category="pharmacy", current_status="operational")
        found = [
            (biz.name, haversine_m(-1.15, 36.96, float(biz.latitude), float(biz.longitude)))
            for biz in qs
        ]
        names = {n for n, _ in found}
        self.assertEqual(len(found), 2)
        self.assertIn("Goodlife Pharmacy Westgate", names)
        self.assertIn("Corner Chemist", names)
        self.assertNotIn("Defunct Pharmacy", names, "closed_permanently must be excluded")
        for _, distance_m in found:
            self.assertLess(distance_m, 300, "both fixtures are within ~200m of the subject point")