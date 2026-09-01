"""The four anomaly rules, each fired on its own, with no database in sight.

These are pure functions over a list of scan rows, and testing them that way is
the point: a rule that can only be exercised by arranging fifty HTTP requests is
a rule nobody will ever tune, and this is the module a judge will ask about.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.config import get_settings
from app.core.clock import now
from app.db.models.enums import SuspicionLevel
from app.db.models.scan import Scan
from app.verification.anomaly import (
    REGION_CENTROIDS,
    SignalCode,
    centroid_for,
    evaluate,
    haversine_km,
    normalise_region,
)

pytestmark = pytest.mark.unit

SETTINGS = get_settings()
ITEM = uuid.uuid4()


def scan(
    *,
    at: datetime,
    region: str | None = "IN-GJ",
    device: str | None = "device-a",
) -> Scan:
    """A scan row built in memory. Never flushed -- the rules never query."""
    row = Scan(
        item_id=ITEM,
        tag_code="X7K29M4P3RQ8",
        country_code=region.split("-")[0] if region else None,
        region_code=region,
        device_fingerprint=device,
    )
    row.created_at = at
    return row


class TestGeography:
    def test_a_bare_subdivision_is_qualified_by_its_country(self) -> None:
        assert normalise_region("IN", "GJ") == "IN-GJ"
        assert normalise_region("in", "gj") == "IN-GJ"

    def test_an_already_qualified_code_passes_through(self) -> None:
        assert normalise_region(None, "IN-AS") == "IN-AS"

    def test_a_subdivision_with_no_country_is_dropped(self) -> None:
        # "GJ" alone would compare equal to a "GJ" somewhere else entirely.
        assert normalise_region(None, "GJ") is None

    def test_nothing_in_is_nothing_out(self) -> None:
        assert normalise_region("IN", None) is None
        assert normalise_region("IN", "  ") is None

    def test_retired_codes_still_resolve(self) -> None:
        # Real headers still carry IN-OR and IN-UT years after ISO moved on.
        assert centroid_for("IN-OR") == centroid_for("IN-OD")
        assert centroid_for("IN-UT") == centroid_for("IN-UK")

    def test_an_unknown_subdivision_falls_back_to_its_country(self) -> None:
        assert centroid_for("IN-ZZ") is not None

    def test_an_unknown_country_has_no_point(self) -> None:
        assert centroid_for("ZZ-ZZ") is None

    def test_gujarat_to_assam_is_about_two_thousand_kilometres(self) -> None:
        distance = haversine_km(REGION_CENTROIDS["IN-GJ"], REGION_CENTROIDS["IN-AS"])
        assert 1_900 < distance < 2_300

    def test_distance_to_self_is_zero(self) -> None:
        assert haversine_km(REGION_CENTROIDS["IN-GJ"], REGION_CENTROIDS["IN-GJ"]) == 0


class TestQuiet:
    def test_no_scans_is_no_signal(self) -> None:
        verdict = evaluate([], SETTINGS)
        assert verdict.level is SuspicionLevel.NONE
        assert verdict.reason is None

    def test_one_ordinary_scan_is_no_signal(self) -> None:
        verdict = evaluate([scan(at=now())], SETTINGS)
        assert verdict.level is SuspicionLevel.NONE

    def test_a_slow_journey_between_two_states_is_no_signal(self) -> None:
        # Bought in Gujarat, gifted in Assam a week later. Ordinary, and a
        # system that flagged it would be wrong far more often than right.
        start = now() - timedelta(days=7)
        verdict = evaluate(
            [scan(at=start, region="IN-GJ"), scan(at=now(), region="IN-AS")], SETTINGS
        )
        assert verdict.level is SuspicionLevel.NONE


class TestEachSignalAlone:
    def test_geographic_spread_alone_is_a_watch(self) -> None:
        # Four regions, spread over months so velocity cannot also fire.
        base = now() - timedelta(days=400)
        scans = [
            scan(at=base + timedelta(days=90 * index), region=region)
            for index, region in enumerate(("IN-GJ", "IN-MH", "IN-KA", "IN-TN"))
        ]
        verdict = evaluate(scans, SETTINGS)
        assert verdict.codes == (SignalCode.GEOGRAPHIC_SPREAD,)
        assert verdict.level is SuspicionLevel.WATCH
        assert "4 different regions" in (verdict.reason or "")

    def test_volume_alone_is_a_watch(self) -> None:
        base = now() - timedelta(days=30)
        scans = [
            scan(at=base + timedelta(minutes=index), region="IN-GJ")
            for index in range(SETTINGS.scan_anomaly_max_scans + 1)
        ]
        verdict = evaluate(scans, SETTINGS)
        assert verdict.codes == (SignalCode.VOLUME,)
        assert verdict.level is SuspicionLevel.WATCH
        assert str(SETTINGS.scan_anomaly_max_scans + 1) in (verdict.reason or "")

    def test_device_diversity_alone_is_a_watch(self) -> None:
        scans = [
            scan(at=now() - timedelta(minutes=index), region="IN-GJ", device=f"device-{index}")
            for index in range(SETTINGS.scan_anomaly_max_devices + 1)
        ]
        verdict = evaluate(scans, SETTINGS)
        assert verdict.codes == (SignalCode.DEVICE_DIVERSITY,)
        assert verdict.level is SuspicionLevel.WATCH
        assert "different devices" in (verdict.reason or "")

    def test_devices_outside_the_window_do_not_count(self) -> None:
        old = now() - timedelta(days=5)
        scans = [
            scan(at=old, region="IN-GJ", device=f"device-{index}")
            for index in range(SETTINGS.scan_anomaly_max_devices + 3)
        ]
        verdict = evaluate(scans, SETTINGS)
        assert SignalCode.DEVICE_DIVERSITY not in verdict.codes

    def test_impossible_velocity_alone_is_already_suspicious(self) -> None:
        # The one signal with no innocent reading: a shop window explains a lot
        # of scans, and explains nothing about one object in two places.
        start = now() - timedelta(seconds=60)
        verdict = evaluate(
            [scan(at=start, region="IN-GJ"), scan(at=now(), region="IN-AS")], SETTINGS
        )
        assert verdict.codes == (SignalCode.IMPOSSIBLE_VELOCITY,)
        assert verdict.level is SuspicionLevel.SUSPICIOUS
        assert "km/h" in (verdict.reason or "")

    def test_two_places_at_one_instant_is_reported_not_divided_by_zero(self) -> None:
        instant = now()
        verdict = evaluate(
            [scan(at=instant, region="IN-GJ"), scan(at=instant, region="IN-AS")], SETTINGS
        )
        assert verdict.level is SuspicionLevel.SUSPICIOUS
        assert "instantly" in (verdict.reason or "")


class TestEscalation:
    def test_two_signals_together_are_suspicious(self) -> None:
        base = now() - timedelta(days=200)
        spread = [
            scan(at=base + timedelta(days=30 * index), region=region)
            for index, region in enumerate(("IN-GJ", "IN-MH", "IN-KA", "IN-TN"))
        ]
        volume = [
            scan(at=base + timedelta(days=150, minutes=index), region="IN-TN")
            for index in range(SETTINGS.scan_anomaly_max_scans + 1)
        ]
        verdict = evaluate(spread + volume, SETTINGS)
        assert {SignalCode.GEOGRAPHIC_SPREAD, SignalCode.VOLUME} <= set(verdict.codes)
        assert verdict.level is SuspicionLevel.SUSPICIOUS

    def test_the_reason_names_every_rule_that_fired(self) -> None:
        base = now() - timedelta(days=200)
        scans = [
            scan(at=base + timedelta(days=30 * index), region=region)
            for index, region in enumerate(("IN-GJ", "IN-MH", "IN-KA", "IN-TN"))
        ]
        # One more region, a minute after the last one: spread and velocity
        # both fire, so the reason has to carry two sentences.
        scans.append(scan(at=scans[-1].created_at + timedelta(minutes=1), region="IN-JK"))
        verdict = evaluate(scans, SETTINGS)
        reason = verdict.reason or ""
        # Every rule contributes a sentence; a level with no explanation behind
        # it is the thing this module exists not to produce.
        assert len(verdict.signals) >= 2
        for signal in verdict.signals:
            assert signal.detail in reason

    def test_scans_with_no_region_are_simply_not_located(self) -> None:
        # A client behind an edge that sends no geo headers still gets a
        # working scan; it just contributes nothing to the located rules.
        scans = [scan(at=now(), region=None), scan(at=now(), region=None)]
        verdict = evaluate(scans, SETTINGS)
        assert verdict.level is SuspicionLevel.NONE
