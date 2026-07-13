"""
property_intel/tests/test_fraud.py

Covers every signal in compute_suspicion_score() individually, the combined
threshold behavior, the OTP trust window (without which OTP verification
would never "stick" across requests), and that every scoring call writes a
FraudReviewLog row.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from property_intel import fraud
from property_intel.models import (
    Broker,
    DeviceFingerprint,
    FraudReviewLog,
    LocationCell,
    PropertyPin,
)


def make_fingerprint(**kwargs):
    kwargs.setdefault("fingerprint_hash", "fp-" + str(id(kwargs)))
    return DeviceFingerprint.objects.create(**kwargs)


def make_cell(lat=-1.15, lng=36.96, geohash="s0x1abc"):
    return LocationCell.objects.create(geohash=geohash, center_latitude=lat, center_longitude=lng)


class IsDisposableEmailTests(TestCase):
    def test_known_disposable_domain(self):
        self.assertTrue(fraud.is_disposable_email("someone@mailinator.com"))

    def test_normal_domain(self):
        self.assertFalse(fraud.is_disposable_email("someone@gmail.com"))

    def test_case_insensitive(self):
        self.assertTrue(fraud.is_disposable_email("Someone@MAILINATOR.com"))


class VelocityCheckTests(TestCase):
    def test_under_threshold_is_fine(self):
        fp = make_fingerprint(fingerprint_hash="v1")
        broker = Broker.objects.create(email="v1@example.com", device_fingerprint=fp)
        cell = make_cell(geohash="v1cell")
        for _ in range(fraud.VELOCITY_MAX_PINS):
            PropertyPin.objects.create(
                raw_input="x", latitude=-1.15, longitude=36.96, location_cell=cell, broker=broker,
            )
        self.assertFalse(fraud._check_velocity(fp))

    def test_over_threshold_flags(self):
        fp = make_fingerprint(fingerprint_hash="v2")
        broker = Broker.objects.create(email="v2@example.com", device_fingerprint=fp)
        cell = make_cell(geohash="v2cell")
        for _ in range(fraud.VELOCITY_MAX_PINS + 1):
            PropertyPin.objects.create(
                raw_input="x", latitude=-1.15, longitude=36.96, location_cell=cell, broker=broker,
            )
        self.assertTrue(fraud._check_velocity(fp))

    def test_none_fingerprint_is_safe(self):
        self.assertFalse(fraud._check_velocity(None))


class RepeatedPinClusterTests(TestCase):
    def test_few_distinct_devices_is_fine(self):
        cell = make_cell(geohash="cluster1")
        for i in range(fraud.CLUSTER_MAX_DISTINCT_DEVICES):
            fp = make_fingerprint(fingerprint_hash=f"cluster-fp-{i}")
            broker = Broker.objects.create(email=f"cluster{i}@example.com", device_fingerprint=fp)
            PropertyPin.objects.create(
                raw_input="x", latitude=-1.15, longitude=36.96, location_cell=cell, broker=broker,
            )
        self.assertFalse(fraud._check_repeated_pin_cluster(cell))

    def test_many_distinct_devices_flags(self):
        cell = make_cell(geohash="cluster2")
        for i in range(fraud.CLUSTER_MAX_DISTINCT_DEVICES + 1):
            fp = make_fingerprint(fingerprint_hash=f"cluster2-fp-{i}")
            broker = Broker.objects.create(email=f"cluster2-{i}@example.com", device_fingerprint=fp)
            PropertyPin.objects.create(
                raw_input="x", latitude=-1.15, longitude=36.96, location_cell=cell, broker=broker,
            )
        self.assertTrue(fraud._check_repeated_pin_cluster(cell))


class IPFanoutTests(TestCase):
    def test_alone_on_an_ip_is_fine(self):
        fp = make_fingerprint(fingerprint_hash="lonely", known_ips=["5.5.5.5"])
        self.assertFalse(fraud._check_ip_fanout(fp))

    def test_many_devices_on_same_ip_flags(self):
        fp = make_fingerprint(fingerprint_hash="fanout-main", known_ips=["1.2.3.4"])
        for i in range(fraud.IP_FANOUT_MAX_DISTINCT_FINGERPRINTS):
            make_fingerprint(fingerprint_hash=f"fanout-other-{i}", known_ips=["1.2.3.4"])
        self.assertTrue(fraud._check_ip_fanout(fp))

    def test_stale_sightings_outside_window_dont_count(self):
        fp = make_fingerprint(fingerprint_hash="fanout-main2", known_ips=["9.8.7.6"])
        stale_cutoff = timezone.now() - timedelta(hours=fraud.IP_FANOUT_WINDOW_HOURS + 1)
        for i in range(fraud.IP_FANOUT_MAX_DISTINCT_FINGERPRINTS):
            other = make_fingerprint(fingerprint_hash=f"fanout-stale-{i}", known_ips=["9.8.7.6"])
            DeviceFingerprint.objects.filter(pk=other.pk).update(last_seen_at=stale_cutoff)
        self.assertFalse(fraud._check_ip_fanout(fp))


class PaymentMethodReuseTests(TestCase):
    def test_no_hash_set_is_fine(self):
        broker = Broker.objects.create(email="nopay@example.com")
        self.assertFalse(fraud._check_payment_method_reuse(broker))

    def test_unique_hash_is_fine(self):
        broker = Broker.objects.create(email="unique@example.com", payment_method_hash="a" * 64)
        self.assertFalse(fraud._check_payment_method_reuse(broker))

    def test_shared_hash_across_brokers_flags(self):
        shared_hash = "b" * 64
        Broker.objects.create(email="first@example.com", payment_method_hash=shared_hash)
        second = Broker.objects.create(email="second@example.com", payment_method_hash=shared_hash)
        self.assertTrue(fraud._check_payment_method_reuse(second))


class ComputeSuspicionScoreTests(TestCase):
    def test_clean_request_scores_zero(self):
        fp = make_fingerprint(fingerprint_hash="clean")
        broker = Broker.objects.create(email="clean@gmail.com", device_fingerprint=fp)
        score, reasons = fraud.compute_suspicion_score(
            device_fingerprint=fp, broker=broker, email="clean@gmail.com", location_cell=None,
        )
        self.assertEqual(score, 0)
        self.assertEqual(reasons, [])

    def test_disposable_email_crosses_otp_threshold(self):
        fp = make_fingerprint(fingerprint_hash="disp")
        broker = Broker.objects.create(email="x@mailinator.com", device_fingerprint=fp)
        score, reasons = fraud.compute_suspicion_score(
            device_fingerprint=fp, broker=broker, email="x@mailinator.com", location_cell=None,
        )
        self.assertGreaterEqual(score, fraud.OTP_THRESHOLD)
        fp.refresh_from_db()
        self.assertTrue(fp.requires_otp_verification)

    def test_datacenter_ip_plus_disposable_email_crosses_manual_review(self):
        fp = make_fingerprint(fingerprint_hash="badactor", is_datacenter_ip=True, ip_asn_name="DigitalOcean")
        broker = Broker.objects.create(email="x@mailinator.com", device_fingerprint=fp)
        score, reasons = fraud.compute_suspicion_score(
            device_fingerprint=fp, broker=broker, email="x@mailinator.com", location_cell=None,
        )
        self.assertGreaterEqual(score, fraud.MANUAL_REVIEW_THRESHOLD)

    def test_every_call_writes_an_audit_log_row(self):
        fp = make_fingerprint(fingerprint_hash="audited")
        broker = Broker.objects.create(email="audited@gmail.com", device_fingerprint=fp)
        fraud.compute_suspicion_score(device_fingerprint=fp, broker=broker, email="audited@gmail.com", location_cell=None)
        self.assertEqual(FraudReviewLog.objects.filter(device_fingerprint=fp).count(), 1)

    def test_recently_verified_otp_suppresses_re_triggering(self):
        """Regression test: without the trust window, a device that just
        verified OTP would be asked again on its very next request, since
        a stable signal like a disposable email recomputes the same score
        every single time."""
        fp = make_fingerprint(fingerprint_hash="trusted", otp_verified_at=timezone.now())
        broker = Broker.objects.create(email="trusted@mailinator.com", device_fingerprint=fp)

        score, _ = fraud.compute_suspicion_score(
            device_fingerprint=fp, broker=broker, email="trusted@mailinator.com", location_cell=None,
        )
        fp.refresh_from_db()
        self.assertGreaterEqual(score, fraud.OTP_THRESHOLD)  # score itself is still elevated...
        self.assertFalse(fp.requires_otp_verification)        # ...but OTP isn't re-demanded

    def test_expired_otp_trust_window_re_triggers(self):
        stale_verification = timezone.now() - timedelta(days=fraud.OTP_TRUST_WINDOW_DAYS + 1)
        fp = make_fingerprint(fingerprint_hash="stale-trust", otp_verified_at=stale_verification)
        broker = Broker.objects.create(email="stale@mailinator.com", device_fingerprint=fp)

        fraud.compute_suspicion_score(
            device_fingerprint=fp, broker=broker, email="stale@mailinator.com", location_cell=None,
        )
        fp.refresh_from_db()
        self.assertTrue(fp.requires_otp_verification)
