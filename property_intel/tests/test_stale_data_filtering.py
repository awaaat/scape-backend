"""
Tests for the CLOSED_PERMANENTLY / CLOSED_TEMPORARILY filtering added to
google_client.py, and for verify_amenities_step actually being reachable
from the enrichment pipeline (it previously wasn't -- see google_client.py
docstring on that function for the story).
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from property_intel import google_client
from property_intel.models import LocationCell


def make_cell(**overrides):
    defaults = dict(geohash="staletest1", center_latitude=0.5143, center_longitude=35.2698)
    defaults.update(overrides)
    return LocationCell.objects.create(**defaults)


def _fake_places_response(places):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"places": places}
    return resp


class SearchNearbyClosedFilterTests(TestCase):
    @patch("property_intel.google_client.requests.post")
    def test_closed_permanently_is_dropped(self, mock_post):
        mock_post.return_value = _fake_places_response([
            {
                "displayName": {"text": "Demolished Mall"},
                "location": {"latitude": 0.515, "longitude": 35.270},
                "businessStatus": "CLOSED_PERMANENTLY",
                "id": "p1",
            },
            {
                "displayName": {"text": "Active Mall"},
                "location": {"latitude": 0.516, "longitude": 35.271},
                "businessStatus": "OPERATIONAL",
                "id": "p2",
            },
        ])
        cell = make_cell()
        results = google_client._search_nearby(cell, "shopping_mall")
        names = [r["name"] for r in results]
        self.assertNotIn("Demolished Mall", names)
        self.assertIn("Active Mall", names)

    @patch("property_intel.google_client.requests.post")
    def test_closed_temporarily_is_kept_and_flagged(self, mock_post):
        mock_post.return_value = _fake_places_response([
            {
                "displayName": {"text": "Renovating Shop"},
                "location": {"latitude": 0.515, "longitude": 35.270},
                "businessStatus": "CLOSED_TEMPORARILY",
                "id": "p3",
            },
        ])
        cell = make_cell()
        results = google_client._search_nearby(cell, "supermarket")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["temporarily_closed"])
        self.assertEqual(results[0]["business_status"], "CLOSED_TEMPORARILY")

    @patch("property_intel.google_client.requests.post")
    def test_operational_place_not_flagged(self, mock_post):
        mock_post.return_value = _fake_places_response([
            {
                "displayName": {"text": "Normal Shop"},
                "location": {"latitude": 0.515, "longitude": 35.270},
                "businessStatus": "OPERATIONAL",
                "id": "p4",
            },
        ])
        cell = make_cell()
        results = google_client._search_nearby(cell, "supermarket")
        self.assertFalse(results[0]["temporarily_closed"])


class SearchTextClosedFilterTests(TestCase):
    @patch("property_intel.google_client.requests.post")
    def test_closed_permanently_is_dropped_in_text_search(self, mock_post):
        mock_post.return_value = _fake_places_response([
            {
                "displayName": {"text": "Gone University Annex"},
                "location": {"latitude": 0.515, "longitude": 35.270},
                "businessStatus": "CLOSED_PERMANENTLY",
                "id": "p5",
            },
        ])
        cell = make_cell()
        results = google_client._search_text(cell, "university near 0.5143,35.2698")
        self.assertEqual(results, [])


class VerifyAmenitiesStepWiringTests(TestCase):
    """The regression this whole patch exists to prevent: the cascade
    module existing and passing its own tests is not the same as it
    actually being called during real enrichment."""

    def test_verify_amenities_step_exists_and_is_callable(self):
        self.assertTrue(hasattr(google_client, "verify_amenities_step"))
        self.assertTrue(callable(google_client.verify_amenities_step))

    def test_verify_amenities_step_is_in_the_pipeline(self):
        import inspect
        source = inspect.getsource(google_client.enrich_location_cell)
        self.assertIn("verify_amenities_step", source)

    def test_verify_amenities_step_never_raises(self):
        """A broken cascade must not take down report generation."""
        cell = make_cell(
            nearby_schools=[{"name": "A", "lat": 0.5143, "lng": 35.2698, "distance_m": 3}],
            nearby_banks=[{"name": "B", "lat": 0.5143, "lng": 35.2698, "distance_m": 3}],
        )
        with patch(
            "property_intel.amenity_verification.resolve_suspect_amenities",
            side_effect=RuntimeError("boom"),
        ):
            result = google_client.verify_amenities_step(cell)
        self.assertIs(result, cell)  # falls back to returning the cell unchanged


class PdfDiscoverAmenityFieldsClosedFilterTests(TestCase):
    def test_closed_permanently_entry_excluded_even_if_cached(self):
        """Simulates a LocationCell fetched BEFORE this patch existed --
        a CLOSED_PERMANENTLY entry with a real distance_m sitting in the
        DB. Must be filtered at read time, not just at next fetch."""
        from property_intel import pdf

        cell = make_cell(
            nearby_schools=[
                {"name": "Demolished School", "lat": 0.515, "lng": 35.270, "distance_m": 200, "business_status": "CLOSED_PERMANENTLY"},
                {"name": "Active School", "lat": 0.516, "lng": 35.271, "distance_m": 300, "business_status": "OPERATIONAL"},
            ],
        )
        fields = dict(pdf._discover_amenity_fields(cell))
        names = [e["name"] for e in fields.get("Schools", [])]
        self.assertNotIn("Demolished School", names)
        self.assertIn("Active School", names)
