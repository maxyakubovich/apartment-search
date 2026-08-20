"""Coverage for the pieces between the mailbox and Telegram.

Everything here runs offline. The Apify normalizer is exercised against the
several field shapes real actor output actually arrives in, and the den
escalation logic is driven by a fake client so the two-stage behaviour is
verifiable without spending API calls.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

import pytest

from src import den as den_module
from src import enrich, notify
from src.config import load_config
from src.filters import evaluate, hard_filter
from src.models import DenVerdict, Listing
from src.sources.email_imap import (
    AlertEmail,
    enrollment_id,
    extract_source_links,
    source_links_from_email,
)
from src.state import State


@pytest.fixture
def config():
    return load_config()


# --- Apify normalization --------------------------------------------------


def test_normalize_canonical_shape():
    item = {
        "zpid": "12345678",
        "hdpUrl": "https://www.zillow.com/homedetails/x/12345678_zpid/",
        "address": {
            "streetAddress": "123 Fell St APT 4",
            "city": "San Francisco",
            "state": "CA",
            "zipcode": "94102",
        },
        "price": "$5,200/mo",
        "bedrooms": 1,
        "bathrooms": 1.5,
        "livingArea": "1,050 sqft",
        "description": "Bright 1BR plus a separate den with french doors.",
        "latitude": 37.7765,
        "longitude": -122.4231,
        "resoFacts": {"roomTypes": ["Den"], "homeType": "Apartment"},
    }
    listing = enrich.normalize(item)
    assert listing.zpid == "12345678"
    assert listing.price == 5200
    assert listing.sqft == 1050
    assert listing.beds == 1 and listing.baths == 1.5
    assert listing.address == "123 Fell St APT 4, San Francisco, CA, 94102"
    assert listing.lat == pytest.approx(37.7765)
    assert listing.reso_facts["roomTypes"] == ["Den"]


def test_normalize_alternate_field_names():
    # Different actors expose the same facts under different keys.
    listing = enrich.normalize(
        {"id": "999", "unformattedPrice": 4800, "beds": 2, "livingAreaValue": 980}
    )
    assert listing.zpid == "999" and listing.price == 4800
    assert listing.beds == 2 and listing.sqft == 980
    # No hdpUrl provided — we synthesize a canonical one.
    assert "999_zpid" in listing.url


def test_normalize_rejects_implausible_sqft():
    # Guards against a year or a unit price landing in the sqft slot.
    assert enrich.normalize({"zpid": "1", "livingArea": 2024}).sqft == 2024
    assert enrich.normalize({"zpid": "1", "livingArea": 40}).sqft is None
    assert enrich.normalize({"zpid": "1", "livingArea": 999999}).sqft is None


def test_normalize_missing_sqft_stays_none():
    # The distinction the whole ladder depends on: unknown is not zero.
    assert enrich.normalize({"zpid": "1", "price": 5000}).sqft is None


def test_normalize_without_identity_returns_none():
    assert enrich.normalize({"price": 5000}) is None


def test_photo_split_pulls_out_floorplans():
    item = {
        "zpid": "1",
        "responsivePhotos": [
            {
                "caption": "Living room",
                "mixedSources": {
                    "jpeg": [
                        {"url": "https://cdn/small.jpg", "width": 384},
                        {"url": "https://cdn/large.jpg", "width": 1536},
                    ]
                },
            },
            {"caption": "Floor Plan", "url": "https://cdn/fp.png"},
        ],
    }
    listing = enrich.normalize(item)
    # Largest variant wins for the interior shot.
    assert listing.photos == ["https://cdn/large.jpg"]
    assert listing.floorplans == ["https://cdn/fp.png"]


# --- email link extraction ------------------------------------------------


ENROLLMENT = "X1-SS5o5te731uya10000000000_5f90w"
FIXTURES = Path(__file__).parent / "fixtures"


def _link(target: str) -> str:
    """A click-tracker href wrapping an encoded target, as Zillow sends them."""
    return (
        '<a href="https://click.mail.zillow.com/f/a/abc**A/AAA*/xyz'
        f'?target={urllib.parse.quote(target, safe="")}">x</a>'
    )


def test_extracts_zpid_result_from_tracking_link():
    # The zpid is already in the href — no redirect following, no network call.
    target = (
        "https://www.zillow.com/routing/email/property-notifications/zpid_target/"
        f"15147609_zpid/{ENROLLMENT}_sse/?z&rtoken=abc&utm_content=forrentimage"
    )
    links = extract_source_links(_link(target))
    assert len(links) == 1
    assert links[0].ident == "15147609"
    assert links[0].kind == "home"
    assert links[0].url == "https://www.zillow.com/homedetails/15147609_zpid/"


def test_extracts_building_page_result():
    # Digest emails link genuine results to building pages that have no zpid.
    target = (
        "https://www.zillow.com/apartments/san-francisco-ca/argenta/5Xj7m7/"
        "?rtoken=abc&utm_content=forrentaddress"
    )
    links = extract_source_links(_link(target))
    assert len(links) == 1
    assert links[0].ident == "5Xj7m7" and links[0].is_building
    assert (
        links[0].url
        == "https://www.zillow.com/apartments/san-francisco-ca/argenta/5Xj7m7/"
    )


def test_rejects_recommendations_and_ads():
    # These sit under "Other rentals you might like" and are often not even in
    # San Francisco. utm_content is the only thing separating them from results.
    recommendation = (
        "https://www.zillow.com/routing/email/property-notifications/zpid_target/"
        f"2097096984_zpid/{ENROLLMENT}_sse/?utm_content=forrentimage-_rid-QaYAp59_"
    )
    ad = (
        "https://www.zillow.com/apartments/alameda-ca/admirals-cove/CkBZvL/"
        "?utm_content=forrentimage-_rid-premium-property_"
    )
    assert extract_source_links(_link(recommendation) + _link(ad)) == []


def test_ignores_chrome_links():
    body = (
        _link("https://www.zillow.com/?utm_content=headerzillowlogo")
        + _link(
            "https://www.zillow.com/routing/email/property-notifications/"
            f"view-all_target/{ENROLLMENT}_sse/?utm_content=viewAll"
        )
        + _link("https://www.zillow.com/learn/rental-pricing-transparency?utm_content=x")
    )
    assert extract_source_links(body) == []


def test_deduplicates_image_and_address_links():
    # Every result appears twice: once on the photo, once on the address.
    base = (
        "https://www.zillow.com/routing/email/property-notifications/zpid_target/"
        f"15147609_zpid/{ENROLLMENT}_sse/?utm_content="
    )
    body = _link(base + "forrentimage") + _link(base + "forrentaddress")
    assert len(extract_source_links(body)) == 1


def test_reads_enrollment_id_from_unsubscribe_link():
    target = (
        "https://www.zillow.com/email/unsubscribe?encodedZuid=X1-ZUwurd"
        f"&encodedEnrollmentId={ENROLLMENT}&subscriptionType=saved_search"
    )
    assert enrollment_id(_link(target)) == ENROLLMENT


def test_email_from_a_different_saved_search_is_skipped():
    body = _link(
        "https://www.zillow.com/email/unsubscribe?encodedEnrollmentId=X1-SS-OTHER"
    ) + _link(
        "https://www.zillow.com/routing/email/property-notifications/zpid_target/"
        "15147609_zpid/X1-SS-OTHER_sse/?utm_content=forrentimage"
    )
    alert = AlertEmail("m", "2 Rental Results for 'Other Search'", None, body)
    assert source_links_from_email(alert, ENROLLMENT) == []
    # With no id configured, nothing is filtered out.
    assert len(source_links_from_email(alert, None)) == 1


def test_urldefense_rewritten_links_still_parse():
    # Forwarded copies get rewritten by Proofpoint, which swaps % for *.
    body = (
        "<https://urldefense.com/v3/__https://click.mail.zillow.com/f/a/abc**A/AAA*/xyz"
        "?target=https*3A*2F*2Fwww.zillow.com*2Frouting*2Femail"
        f"*2Fproperty-notifications*2Fzpid_target*2F15147609_zpid*2F{ENROLLMENT}_sse"
        "*2F*3Fz*26utm_content*3Dforrentimage__;fn5-fn4lJSUl!!G92We9d!abc$ >"
    )
    links = extract_source_links(body)
    assert len(links) == 1 and links[0].ident == "15147609"


@pytest.mark.skipif(
    not (FIXTURES / "instant_single.txt").exists(), reason="fixture not captured"
)
def test_against_real_captured_alert():
    """The regression guard: a real alert, with its real recommendation noise."""
    body = (FIXTURES / "instant_single.txt").read_text()
    alert = AlertEmail("m", "81 Lansing St just listed in 'SF 750-1k sqft'", None, body)
    idents = {link.ident for link in source_links_from_email(alert, ENROLLMENT)}
    assert idents == {"15147609"}  # only the real result
    assert "2096009960" not in idents  # 1300 Lawton — a recommendation
    assert "CkBZvL" not in idents  # Alameda — a paid placement


# --- den escalation -------------------------------------------------------


class FakeBlock:
    type = "tool_use"

    def __init__(self, payload):
        self.input = payload


class FakeResponse:
    def __init__(self, payload):
        self.content = [FakeBlock(payload)]


class FakeClient:
    """Returns queued payloads in order and records how many calls happened."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.payloads.pop(0))


def payload(conf, **kw):
    base = {
        "den_conf": conf,
        "has_door": True,
        "is_passthrough": False,
        "evidence": "e",
        "concerns": "",
    }
    base.update(kw)
    return base


def listing_with_images(**kw):
    base = dict(
        zpid="1",
        url="u",
        description="A nice place",
        photos=["https://cdn/a.jpg"],
        floorplans=["https://cdn/fp.png"],
    )
    base.update(kw)
    return Listing(**base)


def test_confident_text_result_skips_vision(config):
    client = FakeClient(payload(0.95))
    verdict = den_module.analyze(listing_with_images(), config, client)
    assert verdict.den_conf == 0.95
    assert verdict.stage == "text"
    assert len(client.calls) == 1  # no image cost incurred


def test_clearly_negative_text_result_skips_vision(config):
    client = FakeClient(payload(0.05))
    verdict = den_module.analyze(listing_with_images(), config, client)
    assert verdict.stage == "text" and len(client.calls) == 1


def test_ambiguous_text_escalates_to_vision(config, monkeypatch):
    # Stub image fetching so the test stays offline.
    monkeypatch.setattr(
        den_module,
        "_fetch_image",
        lambda url: {"type": "image", "source": {"type": "base64", "data": "x"}},
    )
    client = FakeClient(payload(0.5), payload(0.8))
    verdict = den_module.analyze(listing_with_images(), config, client)
    assert verdict.den_conf == 0.8
    assert verdict.stage == "vision"
    assert len(client.calls) == 2


def test_ambiguous_without_images_does_not_escalate(config):
    client = FakeClient(payload(0.5))
    listing = listing_with_images(photos=[], floorplans=[])
    verdict = den_module.analyze(listing, config, client)
    assert verdict.stage == "text" and len(client.calls) == 1


def test_unfetchable_images_fall_back_to_text_verdict(config, monkeypatch):
    monkeypatch.setattr(den_module, "_fetch_image", lambda url: None)
    client = FakeClient(payload(0.5))
    verdict = den_module.analyze(listing_with_images(), config, client)
    assert verdict.den_conf == 0.5 and verdict.stage == "text"


def test_api_failure_yields_unknown_not_crash(config):
    class Exploding:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("503 overloaded")

    verdict = den_module.analyze(listing_with_images(), config, Exploding())
    assert verdict.stage == "skipped"
    assert verdict.den_conf == 0.0
    assert "503" in verdict.concerns


def test_confidence_is_clamped(config):
    client = FakeClient(payload(1.7))
    assert den_module.analyze(listing_with_images(), config, client).den_conf == 1.0


# --- state ----------------------------------------------------------------


def test_state_roundtrip_and_renotify(tmp_path):
    state = State(tmp_path / "seen.json")
    assert state.is_new("111")

    state.record("111", price=5000, notified=True, reason="confident_den")
    assert not state.is_new("111")

    assert not state.should_renotify("111", 4900)  # 2% drop, not material
    assert state.should_renotify("111", 4700)  # 6% drop
    assert not state.should_renotify("111", 5200)  # went up

    assert state.save()
    reloaded = State(tmp_path / "seen.json")
    assert not reloaded.is_new("111")
    assert reloaded.should_renotify("111", 4700)


def test_refresh_price_preserves_notified_flag(tmp_path):
    state = State(tmp_path / "seen.json")
    state.record("111", price=5000, notified=True, reason="confident_den")
    state.refresh_price("111", 4950)
    assert state.listings["111"]["price"] == 4950
    # The flag survives, so a later real drop still triggers a re-alert.
    assert state.listings["111"]["notified"] is True
    assert state.should_renotify("111", 4600)


def test_corrupt_state_file_does_not_wedge(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    state = State(path)
    assert state.is_new("anything")


# --- message formatting ---------------------------------------------------


def test_message_includes_the_decisive_facts(config):
    listing = Listing(
        zpid="1",
        url="https://www.zillow.com/homedetails/1_zpid/",
        address="123 Fell St, San Francisco, CA",
        price=5200,
        beds=1,
        baths=1.0,
        sqft=1050,
    )
    verdict = DenVerdict(
        den_conf=0.82,
        has_door=True,
        is_passthrough=False,
        evidence='Listing says "separate den with french doors"',
        concerns="",
    )
    msg = notify.format_message(evaluate(listing, verdict, config))
    assert "123 Fell St" in msg
    assert "$5,200/mo" in msg
    assert "1,050 sqft" in msg
    assert "Den confirmed" in msg
    assert "0.82" in msg
    assert "french doors" in msg
    assert "not a walk-through" in msg
    assert 'href="https://www.zillow.com/homedetails/1_zpid/"' in msg


def test_message_flags_unlisted_sqft_and_walkthrough(config):
    listing = Listing(zpid="1", url="u", address="X", price=5000, beds=1, sqft=None)
    verdict = DenVerdict(
        den_conf=0.7,
        has_door=False,
        is_passthrough=True,
        evidence="Open alcove",
        concerns="No door; visible from living room",
    )
    msg = notify.format_message(evaluate(listing, verdict, config))
    assert "sqft unlisted" in msg
    assert "walk-through" in msg
    assert "no door" in msg
    assert "No door; visible from living room" in msg


def test_message_escapes_html_in_listing_text(config):
    listing = Listing(zpid="1", url="u", address="A & B <lofts>", price=5000, beds=2, sqft=950)
    verdict = DenVerdict(0.9, True, False, "quiet & bright", "")
    msg = notify.format_message(evaluate(listing, verdict, config))
    assert "A &amp; B &lt;lofts&gt;" in msg
    assert "quiet &amp; bright" in msg


def _alert_for(enrollment: str, zpid: str) -> AlertEmail:
    body = _link(
        f"https://www.zillow.com/email/unsubscribe?encodedEnrollmentId={enrollment}"
    ) + _link(
        "https://www.zillow.com/routing/email/property-notifications/zpid_target/"
        f"{zpid}_zpid/{enrollment}_sse/?utm_content=forrentimage"
    )
    return AlertEmail("m", "Rental Results", None, body)


def test_accepts_any_of_several_saved_searches():
    # Zillow's sqft filter snaps to anchors, so a real range needs two searches.
    # Both must feed the watcher or one search's alerts vanish silently.
    small, large = ENROLLMENT, "X1-SSover1000sqft_abc"
    configured = [small, large]

    assert len(source_links_from_email(_alert_for(small, "111111"), configured)) == 1
    assert len(source_links_from_email(_alert_for(large, "222222"), configured)) == 1
    assert source_links_from_email(_alert_for("X1-SSunrelated", "333333"), configured) == []


def test_a_bare_string_is_not_treated_as_a_character_set():
    # `found not in "X1-SSabc"` would substring-match and wrongly accept
    # alerts from unrelated searches. A lone string must behave as one id.
    assert len(source_links_from_email(_alert_for(ENROLLMENT, "111111"), ENROLLMENT)) == 1
    # A strict prefix of the configured id must still be rejected.
    prefix = ENROLLMENT[:12]
    assert source_links_from_email(_alert_for(prefix, "444444"), ENROLLMENT) == []


# --- Apify actor inputs ---------------------------------------------------


def test_primary_actor_input_uses_string_enums():
    """Regression: extractBuildingUnits is a string enum, not a boolean.

    Passing True made the actor exit immediately with an empty dataset and a
    2xx status — zero listings, no error, ten seconds. Pinning the exact enum
    values here because the failure is completely silent.
    """
    from src.enrich import _primary_input

    payload = _primary_input(["https://www.zillow.com/homedetails/1_zpid/"])
    assert payload["extractBuildingUnits"] == "for_rent"
    assert payload["propertyStatus"] == "FOR_RENT"
    assert not isinstance(payload["extractBuildingUnits"], bool)
    assert payload["startUrls"] == [{"url": "https://www.zillow.com/homedetails/1_zpid/"}]


def test_fallback_actor_input_pins_listing_url():
    """Regression: this actor's listingUrl defaults to a New York search.

    It takes detail URLs under `detailsUrl`, so sending `startUrls` would leave
    the default in play and return New York rentals as though they were results.
    """
    from src.enrich import _fallback_input

    payload = _fallback_input(["https://www.zillow.com/homedetails/1_zpid/"])
    assert payload["detailsUrl"] == ["https://www.zillow.com/homedetails/1_zpid/"]
    assert payload["listingUrl"] == ""
    assert "startUrls" not in payload


def test_empty_actor_result_is_an_error_not_success():
    """A rejected input yields 2xx + empty dataset, which must not read as 'no
    matches'. It has to raise so the fallback runs and the zpids stay unrecorded."""
    import requests

    from src import enrich

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return []

    def fake_post(*args, **kwargs):
        return FakeResponse()

    original = requests.post
    requests.post = fake_post
    try:
        with pytest.raises(enrich.EnrichmentError, match="no items"):
            enrich._run_actor("a~b", lambda u: {}, ["u"], "tok")
    finally:
        requests.post = original


# --- building expansion ---------------------------------------------------


def _building() -> dict:
    """Shape mirrors a real building detail record: floorPlans and lotId, but
    no bedrooms/price/livingArea at the top level."""
    return {
        "__typename": "BuildingComplex",
        "lotId": "5Xj7m7",
        "buildingName": "Argenta",
        "fullAddress": "1 Polk St, San Francisco, CA 94102",
        "description": "Luxury living downtown.",
        "bdpUrl": "/apartments/san-francisco-ca/argenta/5Xj7m7/",
        "latitude": 37.78,
        "longitude": -122.41,
        "floorPlans": [
            {
                "name": "A4 - 1 Bed + Den",
                "beds": 1,
                "baths": 1,
                "sqft": 950,
                "units": [
                    {"unitNumber": "810", "price": "$5,200"},
                    {"unitNumber": "910", "price": 4900},
                ],
                "floorPlanUnitPhotos": [{"url": "https://cdn/fp-a4.png"}],
            },
            {"name": "S1", "beds": 0, "baths": 1, "sqft": 520, "minPrice": 3200},
        ],
    }


def test_building_records_are_detected():
    from src.enrich import is_building

    assert is_building(_building())
    # A real unit record has bedrooms and must not be expanded.
    assert not is_building({"zpid": "1", "bedrooms": 2, "price": 5000})


def test_building_expands_into_one_listing_per_floor_plan():
    from src.enrich import expand_building

    listings = expand_building(_building())
    assert len(listings) == 2

    den = listings[0]
    assert den.beds == 1 and den.sqft == 950
    # Cheapest available unit, not the range top — you rent one unit.
    assert den.price == 4900
    assert den.floorplans == ["https://cdn/fp-a4.png"]
    # The plan name carries the layout signal the building blurb lacks, and has
    # to reach the den analysis.
    assert "1 Bed + Den" in den.description
    assert "Argenta" in den.description


def test_expanded_plans_get_distinct_stable_ids():
    from src.enrich import expand_building

    ids = [listing.zpid for listing in expand_building(_building())]
    assert len(set(ids)) == 2
    assert all(i.startswith("5Xj7m7-") for i in ids)
    # Stable across runs, so dedup works.
    assert ids == [listing.zpid for listing in expand_building(_building())]


def test_studio_floor_plan_is_dropped_by_the_bed_filter(config):
    from src.enrich import expand_building
    from src.filters import hard_filter

    studio = expand_building(_building())[1]
    assert not hard_filter(studio, config)[0]


def test_building_without_floor_plans_yields_nothing():
    from src.enrich import expand_building

    assert expand_building({"lotId": "x", "floorPlans": []}) == []


# --- escalation and open-plan gating --------------------------------------


def test_floorplan_always_escalates_even_on_a_low_text_score(config, monkeypatch):
    """Regression: real listings scored 0.10 on text and 0.75 once the floor
    plan was read. A low text score usually means the description never
    mentioned the layout, so a plan must never be left unexamined."""
    monkeypatch.setattr(
        den_module, "_fetch_image", lambda url: {"type": "image", "source": {}}
    )
    client = FakeClient(payload(0.05), payload(0.75))
    verdict = den_module.analyze(listing_with_images(), config, client)
    assert verdict.den_conf == 0.75 and verdict.stage == "vision"
    assert len(client.calls) == 2


def test_photos_only_still_respects_the_band(config, monkeypatch):
    monkeypatch.setattr(den_module, "_fetch_image", lambda url: {"type": "image"})
    listing = listing_with_images(floorplans=[], photos=["https://cdn/a.jpg"])
    client = FakeClient(payload(0.05))
    verdict = den_module.analyze(listing, config, client)
    assert verdict.stage == "text" and len(client.calls) == 1


def test_open_plan_loft_is_withheld_from_the_weakest_rung(config):
    """A 1,021 sqft unit the model calls affirmatively open-plan buys no
    privacy, so floor area alone must not push it through."""
    # Floor plan present, so the layout_unknown rung cannot apply and this
    # isolates the open-plan gate on room_to_sequester.
    big = Listing(zpid="1", url="u", price=5587, beds=1, sqft=1021,
                  floorplans=["https://cdn/fp.png"])
    open_plan = DenVerdict(0.05, False, None, "open layout", is_open_plan=True)
    assert not evaluate(big, open_plan, config).notify

    # Same size, layout merely unstated -> still notified.
    unknown = DenVerdict(0.05, None, None, "no layout given", is_open_plan=None)
    decision = evaluate(big, unknown, config)
    assert decision.notify and decision.reason == "room_to_sequester"


def test_open_plan_does_not_block_a_confirmed_den(config):
    # Only the weakest rung is gated; a real door still wins.
    verdict = DenVerdict(0.8, True, False, "separate den", is_open_plan=True)
    listing = Listing(zpid="1", url="u", price=5000, beds=1, sqft=900)
    assert evaluate(listing, verdict, config).notify


def test_two_bedroom_message_hides_the_den_score(config):
    """The second bedroom IS the private room, so a low den score measures
    something irrelevant and would make a good flat look weak."""
    listing = Listing(zpid="1", url="u", address="X", price=6000, beds=2, sqft=950)
    verdict = DenVerdict(0.05, True, False, "two bedrooms off the living room")
    msg = notify.format_message(evaluate(listing, verdict, config))
    assert "second bedroom works as the office" in msg
    assert "confidence" not in msg
    assert "🟢" in msg


def test_floor_plans_with_no_availability_are_skipped():
    """A building publishes every layout it offers, including ones with nothing
    free. Those have no price, would skip the budget gate entirely (it only
    applies when price is known), and would be sent showing '$?'."""
    from src.enrich import expand_building

    item = {
        "__typename": "Building",
        "lotId": "X",
        "fullAddress": "1 Test St",
        "floorPlans": [
            {"name": "B2 nothing free", "beds": 2, "baths": 2, "sqft": 950},
            {"name": "B3 available", "beds": 2, "baths": 2, "sqft": 960,
             "units": [{"price": 5900}]},
            {"name": "B4 plan price only", "beds": 2, "sqft": 970, "minPrice": 6100},
        ],
    }
    listings = expand_building(item)
    assert [l.price for l in listings] == [5900, 6100]
    assert all("nothing-free" not in l.zpid for l in listings)


def test_labeled_den_surfaces_even_when_it_fails_the_door_test(config):
    """Real case: an 851 sqft unit at $4,973 whose floor plan showed
    'DEN 10'6" x 6'6"' scored 0.15 because the front door opened into it, and
    was dropped. A literally labelled den is worth seeing with the caveat
    attached rather than suppressed."""
    listing = Listing(zpid="349565562", url="u", address="X", price=4973,
                      beds=1, sqft=851)
    verdict = DenVerdict(
        0.15, has_door=False, is_passthrough=True,
        evidence='Floor plan shows "DEN 10\'6\\" x 6\'6\\"" at the entry',
        den_labeled=True,
    )
    decision = evaluate(listing, verdict, config)
    assert decision.notify and decision.reason == "labeled_den"

    msg = notify.format_message(decision)
    assert "labels a den" in msg
    assert "walk-through" in msg  # the caveat is stated, not hidden
    assert "no door" in msg


def test_unlabeled_low_confidence_still_needs_the_space(config):
    # Without a labelled den the rung must not fire. Floor plan present, so
    # the layout is known and layout_unknown cannot carry it instead.
    listing = Listing(zpid="1", url="u", price=5000, beds=1, sqft=880,
                      floorplans=["https://cdn/fp.png"])
    verdict = DenVerdict(0.15, None, None, "nothing stated", den_labeled=False)
    assert not evaluate(listing, verdict, config).notify


def test_labeled_den_below_the_floor_is_still_dropped(config):
    listing = Listing(zpid="1", url="u", price=5000, beds=1, sqft=800)
    assert not hard_filter(listing, config)[0]


def test_unknown_layout_is_not_the_same_as_confirmed_no_den(config):
    """Eight in-budget 1BRs were skipped citing "no floor plan provided", scored
    identically to units whose plan had been read and showed one bedroom. The
    first group is unknown, the second settled; only the first takes this rung."""
    unknown = Listing(zpid="428167240", url="u", address="X", price=4150,
                      beds=1, sqft=882, floorplans=[])
    verdict = DenVerdict(0.05, None, None, "no floor plan provided")
    decision = evaluate(unknown, verdict, config)
    assert decision.notify and decision.reason == "layout_unknown"
    assert "No floor plan published" in notify.format_message(decision)

    # Same size and score, but the plan was read and showed nothing.
    seen = Listing(zpid="2094946200", url="u", price=6154, beds=1, sqft=903,
                   floorplans=["https://cdn/fp.png"])
    settled = DenVerdict(0.03, False, None, "plan shows a single bedroom")
    assert not evaluate(seen, settled, config).notify


def test_unknown_layout_still_respects_open_plan_and_floor(config):
    stated_open = DenVerdict(0.05, None, None, "open layout", is_open_plan=True)
    listing = Listing(zpid="1", url="u", price=5587, beds=1, sqft=1021, floorplans=[])
    assert not evaluate(listing, stated_open, config).notify


def test_state_commits_are_throttled_except_after_a_send(tmp_path, monkeypatch):
    """Committing every two-minute cycle produced ~700 commits a day and made
    every human push collide with the bot. But a send must be committed at once:
    losing that record would resend the listing."""
    calls = []
    monkeypatch.setattr(
        "src.state.subprocess.run",
        lambda *a, **k: calls.append(a[0][:2]) or type("R", (), {"returncode": 1})(),
    )
    state = State(tmp_path / "seen.json")

    state.commit()
    assert calls, "first commit should always run"

    calls.clear()
    state.commit()
    assert calls == [], "second commit inside the interval must be skipped"

    state.commit(force=True)
    assert calls, "a send must commit immediately regardless of the interval"


# --- ladder versioning ----------------------------------------------------


def test_bumping_the_ladder_version_reopens_unsent_listings(tmp_path):
    """Editing state/seen.json by hand cannot work while the watcher runs: it
    holds its own copy in memory and writes it back wholesale, which is exactly
    how a reset got clobbered. Versioning does the same job declaratively."""
    state = State(tmp_path / "seen.json")
    state.record("111", price=5000, notified=False, reason="clears no rung",
                 ladder_version=1)

    assert not state.is_new("111", ladder_version=1)   # same logic, settled
    assert state.is_new("111", ladder_version=2)       # logic moved on, reopen


def test_already_sent_listings_are_never_resent_after_a_bump(tmp_path):
    state = State(tmp_path / "seen.json")
    state.record("222", price=6000, notified=True, reason="two_bedroom",
                 ladder_version=1)
    assert not state.is_new("222", ladder_version=99)


def test_sources_are_rescraped_after_a_bump(tmp_path):
    """A new ladder needs the units re-derived, not merely re-judged."""
    state = State(tmp_path / "seen.json")
    state.note_source_scraped("5Xj7m7", ladder_version=1)
    assert not state.should_scrape_source("5Xj7m7", is_building=False, ladder_version=1)
    assert state.should_scrape_source("5Xj7m7", is_building=False, ladder_version=2)


def test_legacy_bare_timestamp_sources_still_parse(tmp_path):
    # Written before versioning existed; must not crash or block forever.
    import json
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"listings": {}, "sources": {"X": "2026-08-20T00:00:00+00:00"}}))
    state = State(path)
    assert state.should_scrape_source("X", is_building=False, ladder_version=2)


def test_null_ladder_version_does_not_crash_the_loop(tmp_path):
    """A record with an explicit null would raise TypeError on int(None) and
    take down the whole watch loop. Null and absent must behave identically."""
    import json
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({"listings": {
        "a": {"notified": False, "ladder_version": None},
        "b": {"notified": False},
    }}))
    state = State(path)
    assert state.is_new("a", ladder_version=2)
    assert state.is_new("b", ladder_version=2)


# --- duplicate suppression ------------------------------------------------


def test_same_apartment_under_a_different_id_is_not_resent(tmp_path):
    """Floor plans are keyed on the scraper's zpid when it supplies one and a
    synthesised slug when it does not, so one apartment can arrive under two
    ids across runs and defeat dedup. The fingerprint catches it."""
    first = Listing(zpid="2066681346", url="u", address="1 Polk St, San Francisco",
                    price=4835, beds=1, sqft=855)
    later = Listing(zpid="5Xj7m7-a2-h", url="u", address="1 Polk St, San Francisco",
                    price=4835, beds=1, sqft=855)
    assert first.fingerprint == later.fingerprint

    state = State(tmp_path / "seen.json")
    state.record("2066681346", price=4835, notified=True, reason="layout_unknown",
                 fingerprint=first.fingerprint, address=first.address)
    assert state.already_notified(later.fingerprint) == "2066681346"


def test_fingerprint_ignores_price_so_drops_still_alert(tmp_path):
    a = Listing(zpid="1", url="u", address="1 Polk St", price=5000, beds=1, sqft=900)
    b = Listing(zpid="1", url="u", address="1 Polk St", price=4500, beds=1, sqft=900)
    assert a.fingerprint == b.fingerprint


def test_different_units_in_one_building_stay_distinct(tmp_path):
    a = Listing(zpid="1", url="u", address="A2, 1 Polk St", beds=1, sqft=855)
    b = Listing(zpid="2", url="u", address="B4, 1 Polk St", beds=2, sqft=1100)
    assert a.fingerprint != b.fingerprint


def test_addressless_listing_is_not_treated_as_a_duplicate(tmp_path):
    """Without an address the fingerprint is not a usable identity, and matching
    on it would silently suppress every such listing after the first."""
    state = State(tmp_path / "seen.json")
    nameless = Listing(zpid="1", url="u", address=None, beds=1, sqft=900)
    state.record("1", price=5000, notified=True, reason="x",
                 fingerprint=nameless.fingerprint)
    other = Listing(zpid="2", url="u", address=None, beds=1, sqft=900)
    assert state.already_notified(other.fingerprint) is None
