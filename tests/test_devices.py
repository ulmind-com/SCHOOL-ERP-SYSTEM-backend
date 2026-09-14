"""Hardware-facing logic: biometric punch parsing and GPS track maths."""

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.biometrics.service import _parse_clock, parse_attlog
from app.modules.lms.service import join_state
from app.modules.tracking.service import (
    GLITCH_WINDOW_SECONDS,
    MAX_PLAUSIBLE_KMH,
    haversine_km,
)


class TestAttlogParsing:
    def test_documented_tab_separated_form(self):
        punches = parse_attlog("1001\t2026-09-14 08:52:13\t0\t1\n")
        assert len(punches) == 1
        assert punches[0]["biometric_id"] == "1001"
        assert punches[0]["punched_at"] == datetime(2026, 9, 14, 8, 52, 13)
        assert punches[0]["state"] == "0"
        assert punches[0]["verify_mode"] == "1"

    def test_space_separated_firmware_splits_the_timestamp(self):
        """Field firmware sends date and time as separate tokens. Reading the
        status from a fixed index mislabels every punch on this dialect."""
        punches = parse_attlog("1002 2026-09-14 09:31:07 0 15\n")
        assert punches[0]["punched_at"] == datetime(2026, 9, 14, 9, 31, 7)
        assert punches[0]["state"] == "0"
        assert punches[0]["verify_mode"] == "15"

    def test_check_out_state_is_not_swallowed(self):
        punches = parse_attlog("1001\t2026-09-14 16:10:44\t1\t1\n")
        assert punches[0]["state"] == "1"

    def test_minimal_line_defaults_the_state(self):
        punches = parse_attlog("1003\t2026-09-14 10:00:00\n")
        assert punches[0]["state"] == "0"
        assert punches[0]["verify_mode"] == ""

    @pytest.mark.parametrize("body", ["garbage\n", "1004\tnot-a-date\n", "\n\n", "onlyone\n"])
    def test_unparseable_lines_are_skipped_not_raised(self, body):
        assert parse_attlog(body) == []

    def test_mixed_batch_keeps_the_good_lines(self):
        punches = parse_attlog(
            "1001\t2026-09-14 08:00:00\t0\t1\n"
            "junk\n"
            "1002\t2026-09-14 08:05:00\t0\t1\n"
        )
        assert [p["biometric_id"] for p in punches] == ["1001", "1002"]

    def test_blank_body(self):
        assert parse_attlog("") == []


class TestClockParsing:
    def test_valid(self):
        assert _parse_clock("09:15").hour == 9

    @pytest.mark.parametrize("value", ["nonsense", "", None, "25"])
    def test_invalid_falls_back(self, value):
        assert _parse_clock(value).hour == 9


class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_km(22.5, 88.3, 22.5, 88.3) == 0.0

    def test_known_distance(self):
        # Esplanade to Salt Lake Sector V, about 8 km.
        km = haversine_km(22.5645, 88.3505, 22.5800, 88.4300)
        assert 7.5 < km < 9.0

    def test_symmetric(self):
        a = haversine_km(22.5, 88.3, 22.6, 88.4)
        b = haversine_km(22.6, 88.4, 22.5, 88.3)
        assert a == pytest.approx(b)


class TestGlitchWindow:
    """The rule that separates a bad GPS fix from a driver app that was offline."""

    def _speed(self, km: float, seconds: float) -> float:
        return km / (seconds / 3600)

    def test_a_teleport_within_seconds_is_implausible(self):
        assert self._speed(1200, 2) > MAX_PLAUSIBLE_KMH

    def test_a_long_gap_covering_real_distance_is_also_flagged_by_speed(self):
        """Both look implausible by speed — which is exactly why the elapsed
        time, not the speed, decides whether the distance is kept."""
        assert self._speed(9, 15) > MAX_PLAUSIBLE_KMH
        assert 15 < GLITCH_WINDOW_SECONDS

    def test_normal_driving_is_plausible(self):
        assert self._speed(0.83, 90) < MAX_PLAUSIBLE_KMH


class TestOnlineClassWindow:
    def _at(self, offset: timedelta, status: str = "scheduled") -> dict:
        now = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        starts = now + offset
        return (
            {"starts_at": starts, "ends_at": starts + timedelta(minutes=45), "status": status},
            now,
        )

    def test_link_is_withheld_well_before_the_start(self):
        doc, now = self._at(timedelta(hours=2))
        assert join_state(doc, now) == "upcoming"

    def test_link_opens_shortly_before(self):
        doc, now = self._at(timedelta(minutes=10))
        assert join_state(doc, now) == "joinable"

    def test_in_progress_is_live(self):
        doc, now = self._at(timedelta(minutes=-5))
        assert join_state(doc, now) == "live"

    def test_expires_after_the_grace_window(self):
        doc, now = self._at(timedelta(hours=-5))
        assert join_state(doc, now) == "ended"

    def test_cancelled_beats_every_other_state(self):
        doc, now = self._at(timedelta(minutes=-5), status="cancelled")
        assert join_state(doc, now) == "cancelled"

    def test_naive_timestamps_do_not_raise(self):
        """Documents written before timezone handling was tightened must still
        render rather than crash the page they appear on."""
        starts = datetime(2026, 9, 14, 9, 55)
        doc = {"starts_at": starts, "ends_at": starts + timedelta(minutes=45),
               "status": "scheduled"}
        assert join_state(doc, datetime(2026, 9, 14, 10, 0, tzinfo=UTC)) == "live"
