"""Ladder and gate coverage.

The ladder is the part of this system most likely to silently do the wrong
thing — a misordered rung means either a missed apartment or a flood of noise,
and neither is obvious from watching Telegram. So every branch gets a case.
"""

from __future__ import annotations

import pytest

from src.config import load_config
from src.filters import evaluate, hard_filter, point_in_polygon
from src.models import DenVerdict, Listing


@pytest.fixture
def config():
    return load_config()


def make(**kwargs) -> Listing:
    base = dict(zpid="1", url="https://zillow.com/x/1_zpid/", price=5000, beds=1)
    base.update(kwargs)
    return Listing(**base)


def den(conf: float) -> DenVerdict:
    return DenVerdict(
        den_conf=conf, has_door=conf >= 0.5, is_passthrough=False, evidence="test"
    )


# --- hard gates -----------------------------------------------------------


def test_drops_over_budget(config):
    ok, reason = hard_filter(make(price=7000, sqft=1200), config)
    assert not ok and "over" in reason


def test_drops_studio_and_three_bed(config):
    assert not hard_filter(make(beds=0, sqft=1000), config)[0]
    assert not hard_filter(make(beds=3, sqft=1400), config)[0]


def test_drops_below_sqft_floor(config):
    ok, reason = hard_filter(make(sqft=800), config)
    assert not ok and "floor" in reason


def test_missing_sqft_survives_hard_filter(config):
    # Unknown is not zero: this has to reach the ladder, not die here.
    assert hard_filter(make(sqft=None), config)[0]


def test_passes_clean_listing(config):
    assert hard_filter(make(sqft=950), config)[0]


# --- ladder ---------------------------------------------------------------


def test_two_bedroom_notifies_without_den_signal(config):
    d = evaluate(make(beds=2, sqft=950), den(0.0), config)
    assert d.notify and d.reason == "two_bedroom"


def test_small_two_bedroom_falls_through_to_den_rungs(config):
    # 880 sqft misses the 2BR rung's 900 bar and has no den signal.
    d = evaluate(make(beds=2, sqft=880), den(0.0), config)
    assert not d.notify


def test_confident_den_clears_lower_bar(config):
    d = evaluate(make(sqft=860), den(0.7), config)
    assert d.notify and d.reason == "confident_den"


def test_plausible_den_needs_more_space(config):
    assert not evaluate(make(sqft=860), den(0.4), config).notify
    d = evaluate(make(sqft=950), den(0.4), config)
    assert d.notify and d.reason == "plausible_den"


def test_no_den_signal_needs_the_most_space(config):
    assert not evaluate(make(sqft=950), den(0.0), config).notify
    d = evaluate(make(sqft=1050), den(0.0), config)
    assert d.notify and d.reason == "room_to_sequester"


# --- missing square footage ----------------------------------------------


def test_unlisted_sqft_requires_confident_den(config):
    d = evaluate(make(sqft=None), den(0.7), config)
    assert d.notify and d.sqft_unlisted

    d = evaluate(make(sqft=None), den(0.4), config)
    assert not d.notify and d.sqft_unlisted


# --- geo ------------------------------------------------------------------


def test_point_in_polygon():
    # Rough box over central SF, [lng, lat] to match Zillow's ordering.
    box = [[-122.45, 37.74], [-122.45, 37.79], [-122.39, 37.79], [-122.39, 37.74]]
    assert point_in_polygon(37.76, -122.42, box)  # inside
    assert not point_in_polygon(37.76, -122.50, box)  # west of it
    assert not point_in_polygon(37.90, -122.42, box)  # north of it


def test_polygon_gate_drops_outside_listings(config):
    config = dict(config)
    config["search"] = dict(config["search"])
    config["search"]["polygon"] = [
        [-122.45, 37.74],
        [-122.45, 37.79],
        [-122.39, 37.79],
        [-122.39, 37.74],
    ]
    inside = make(sqft=950, lat=37.76, lng=-122.42)
    outside = make(sqft=950, lat=37.76, lng=-122.50)
    assert hard_filter(inside, config)[0]
    assert not hard_filter(outside, config)[0]


def test_degenerate_polygon_does_not_drop_everything(config):
    # A malformed polygon should fail open rather than silently muting alerts.
    assert point_in_polygon(37.76, -122.42, [[-122.45, 37.74]])
