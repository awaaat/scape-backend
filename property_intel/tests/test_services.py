"""
property_intel/tests/test_services.py

Covers parse_location_input() — every input format a broker can realistically
paste, plus the garbage/abuse cases the bounds-checking exists for — and the
cache orchestration helpers (get_or_create_location_cell, needs_enrichment).
"""
from unittest.mock import Mock, patch

from django.test import TestCase

from property_intel.models import Broker, LocationCell
from property_intel.services import (
    LocationParseError,
    create_pin,
    get_or_create_location_cell,
    needs_enrichment,
    parse_location_input,
)


class ParseLocationInputTests(TestCase):
    def test_raw_coordinates(self):
        lat, lng, input_type = parse_location_input("-1.153472, 36.964281")
        self.assertAlmostEqual(lat, -1.153472)
        self.assertAlmostEqual(lng, 36.964281)
        self.assertEqual(input_type, "coordinates")

    def test_raw_coordinates_no_space(self):
        lat, lng, input_type = parse_location_input("-1.153472,36.964281")
        self.assertEqual(input_type, "coordinates")

    def test_google_maps_at_link(self):
        lat, lng, input_type = parse_location_input(
            "https://www.google.com/maps/@-1.153472,36.964281,15z"
        )
        self.assertAlmostEqual(lat, -1.153472)
        self.assertEqual(input_type, "google_maps_link")

    def test_google_maps_query_link(self):
        lat, lng, input_type = parse_location_input(
            "https://maps.google.com/?q=-1.153472,36.964281"
        )
        self.assertEqual(input_type, "google_maps_link")

    def test_whatsapp_style_embedded_coords(self):
        lat, lng, input_type = parse_location_input(
            "Check this out! location: -1.153472, 36.964281 near the estate"
        )
        self.assertEqual(input_type, "whatsapp_location")

    @patch("property_intel.services.requests.head")
    def test_short_link_resolves_to_trusted_host(self, mock_head):
        mock_response = Mock()
        mock_response.url = "https://www.google.com/maps/@-1.153472,36.964281,15z"
        mock_response.history = []
        mock_head.return_value = mock_response

        lat, lng, input_type = parse_location_input("https://maps.app.goo.gl/abc123")
        self.assertAlmostEqual(lat, -1.153472)
        self.assertEqual(input_type, "google_maps_short_link")

    @patch("property_intel.services.requests.head")
    def test_short_link_resolving_to_untrusted_host_is_rejected(self, mock_head):
        """A short link is only useful because we trust google.com — if it
        resolves anywhere else, this must refuse rather than silently
        trusting an attacker-controlled redirect target."""
        mock_response = Mock()
        mock_response.url = "https://evil-phishing-site.example/@-1.15,36.96"
        mock_response.history = []
        mock_head.return_value = mock_response

        with self.assertRaises(LocationParseError):
            parse_location_input("https://maps.app.goo.gl/abc123")

    def test_empty_input_rejected(self):
        with self.assertRaises(LocationParseError):
            parse_location_input("")

    def test_garbage_text_rejected(self):
        with self.assertRaises(LocationParseError):
            parse_location_input("call me on 0722123456")

    def test_out_of_bounds_coordinates_rejected(self):
        """New York's coordinates — a real lat/lng pair, just nowhere near
        Kenya. This is the abuse/garbage-catching bounds check, not a
        precise-border check."""
        with self.assertRaises(LocationParseError):
            parse_location_input("40.7128, -74.0060")

    def test_swapped_lat_lng_out_of_earth_range_rejected(self):
        with self.assertRaises(LocationParseError):
            parse_location_input("200.0, 300.0")

    def test_coordinates_are_rounded_to_7_decimal_places(self):
        lat, lng, _ = parse_location_input("-1.15347212345, 36.96428112345")
        self.assertEqual(lat, round(lat, 7))
        self.assertEqual(lng, round(lng, 7))


class LocationCellCacheTests(TestCase):
    def test_same_coordinates_reuse_the_same_cell(self):
        cell1, created1 = get_or_create_location_cell(-1.1534721, 36.9642811)
        cell2, created2 = get_or_create_location_cell(-1.1534722, 36.9642812)  # ~sub-meter difference

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(cell1.pk, cell2.pk)

    def test_times_reused_increments_on_cache_hit(self):
        cell, _ = get_or_create_location_cell(-1.1534721, 36.9642811)
        self.assertEqual(cell.times_reused, 0)

        cell2, created = get_or_create_location_cell(-1.1534721, 36.9642811)
        self.assertFalse(created)
        self.assertEqual(cell2.times_reused, 1)

    def test_needs_enrichment_true_for_incomplete_cell(self):
        cell, _ = get_or_create_location_cell(-1.1534721, 36.9642811)
        self.assertTrue(needs_enrichment(cell))

    def test_needs_enrichment_false_for_fresh_complete_cell(self):
        cell, _ = get_or_create_location_cell(-1.1534721, 36.9642811)
        cell.formatted_address = "Ruiru, Kenya"
        cell.satellite_image_url = "https://example.com/sat.jpg"
        from django.utils import timezone
        cell.amenities_fetched_at = timezone.now()
        cell.air_quality_fetched_at = timezone.now()
        cell.travel_times_fetched_at = timezone.now()
        cell.save()

        self.assertFalse(needs_enrichment(cell))


class CreatePinTests(TestCase):
    def test_create_pin_attaches_the_given_broker(self):
        """Regression test: the original create_pin() accepted a
        `submitted_by` string and never set PropertyPin.broker at all,
        which is a required FK with no default — every call would have
        raised IntegrityError in production."""
        broker = Broker.objects.create(email="broker@example.com")
        pin, cell = create_pin("-1.1534721, 36.9642811", broker=broker)

        self.assertEqual(pin.broker_id, broker.pk)
        self.assertIsInstance(cell, LocationCell)

    def test_create_pin_rejects_bad_input_before_touching_the_db(self):
        broker = Broker.objects.create(email="broker2@example.com")
        with self.assertRaises(LocationParseError):
            create_pin("not a location", broker=broker)
