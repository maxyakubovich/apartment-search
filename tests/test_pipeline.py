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
from src.filters import evaluate
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
