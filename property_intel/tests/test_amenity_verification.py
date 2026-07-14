from unittest.mock import patch

from django.test import TestCase

from property_intel import amenity_verification as av
from property_intel.models import LocationCell


def make_cell(**overrides):
    defaults = dict(geohash="verify-test", center_latitude=0.5143, center_longitude=35.2698)
    defaults.update(overrides)
    return LocationCell.objects.create(**defaults)


class NameMatchingTests(TestCase):
    def test_exact_match(self):
        self.assertTrue(av._names_match("Shell petrol station", "Shell"))

    def test_substring_match(self):
        self.assertTrue(av._names_match("Moi Girls Eldoret", "Moi Girls High School"))

    def test_unrelated_names_do_not_match(self):
        self.assertFalse(av._names_match("Kdf Eldoret", "Naivas Supermarket"))

    def test_empty_name_never_matches(self):
        self.assertFalse(av._names_match("", "Shell"))


class DetectCoincidentClustersTests(TestCase):
    def test_identical_coordinates_form_a_cluster(self):
        cell = make_cell(
            nearby_schools=[{"name": "School A", "lat": 0.51427, "lng": 35.26978, "distance_m": 3}],
            nearby_petrol_stations=[{"name": "Shell", "lat": 0.51427, "lng": 35.26978, "distance_m": 3}],
        )
        clusters = av.detect_coincident_clusters(cell)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]), 2)

    def test_genuinely_distinct_coordinates_are_not_clustered(self):
        cell = make_cell(
            nearby_schools=[{"name": "School A", "lat": 0.51427, "lng": 35.26978, "distance_m": 3}],
            nearby_petrol_stations=[{"name": "Shell", "lat": 0.52000, "lng": 35.28000, "distance_m": 900}],
        )
        self.assertEqual(av.detect_coincident_clusters(cell), [])


class ResolveSuspectAmenitiesTests(TestCase):
    @patch("property_intel.amenity_verification._query_osm_candidates")
    def test_confident_osm_match_corrects_distance(self, mock_query):
        mock_query.return_value = [{"name": "Shell", "brand": "Shell", "lat": 0.5200, "lng": 35.2750}]
        # Verification only ever triggers for entries caught in a cluster
        # of 2+ categories sharing a coordinate (cost control -- see
        # detect_coincident_clusters/MIN_CLUSTER_SIZE). A single entry
        # alone never forms a cluster, so this needs a second category at
        # the same point, matching the real "3m cluster" scenario this
        # module exists to catch.
        cell = make_cell(
            nearby_petrol_stations=[
                {"name": "Shell petrol station", "lat": 0.51427, "lng": 35.26978, "distance_m": 3},
            ],
            nearby_schools=[
                {"name": "Some School", "lat": 0.51427, "lng": 35.26978, "distance_m": 3},
            ],
        )
        av.resolve_suspect_amenities(cell)
        entry = cell.nearby_petrol_stations[0]
        self.assertEqual(entry["verified_via"], "osm")
        self.assertIsNotNone(entry["distance_m"])
        self.assertNotEqual(entry["distance_m"], 3)

    @patch("property_intel.amenity_verification._query_osm_candidates", return_value=[])
    def test_no_osm_match_suppresses_distance_not_unknown_text(self, mock_query):
        cell = make_cell(
            nearby_schools=[{"name": "School A", "lat": 0.51427, "lng": 35.26978, "distance_m": 3}],
            nearby_petrol_stations=[{"name": "Shell", "lat": 0.51427, "lng": 35.26978, "distance_m": 3}],
        )
        av.resolve_suspect_amenities(cell)
        self.assertIsNone(cell.nearby_schools[0]["distance_m"])
        self.assertTrue(cell.nearby_schools[0]["location_unverified"])

    def test_category_with_no_osm_mapping_is_dropped_entirely(self):
        cell = make_cell(
            nearby_gated_communities=[{"name": "Estate A", "lat": 0.51427, "lng": 35.26978, "distance_m": 3}],
            nearby_schools=[{"name": "School A", "lat": 0.51427, "lng": 35.26978, "distance_m": 3}],
        )
        with patch("property_intel.amenity_verification._query_osm_candidates", return_value=[]):
            av.resolve_suspect_amenities(cell)
        self.assertEqual(cell.nearby_gated_communities, [])

    def test_no_cluster_means_no_changes_and_no_osm_calls(self):
        cell = make_cell(nearby_schools=[{"name": "School A", "lat": 0.51427, "lng": 35.26978, "distance_m": 300}])
        with patch("property_intel.amenity_verification._query_osm_candidates") as mock_query:
            av.resolve_suspect_amenities(cell)
            mock_query.assert_not_called()
