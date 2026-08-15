"""Listing detail via Apify.

Zillow has no usable API and its detail pages sit behind Akamai, so detail data
comes from a third-party scraper actor. This module is the only place that
knows that — everything downstream consumes a normalized `Listing`.

Apify actor output shapes drift between versions and between actors, so the
normalizer reads defensively from several plausible field names rather than
trusting one schema. When a field genuinely is not there we leave it `None`;
the ladder is built to handle unknowns, and inventing a zero would silently
drop good apartments.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import requests

from .models import Listing

APIFY_BASE = "https://api.apify.com/v2/acts"
PRIMARY_ACTOR = "maxcopell~zillow-detail-scraper"
FALLBACK_ACTOR = "parseforge~zillow-scraper"
RUN_TIMEOUT = 240


class EnrichmentError(RuntimeError):
    pass


def _first(d: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d and d[key] not in (None, "", []):
            return d[key]
    return None


def _to_int(value: Any) -> int | None:
    """Coerce Zillow's assorted numeric renderings ('$4,250/mo', '1,100 sqft')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


def _address(raw: Any) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        parts = [
            raw.get("streetAddress"),
            raw.get("city"),
            raw.get("state"),
            raw.get("zipcode"),
        ]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    return None


def _photo_urls(item: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Split imagery into (interior photos, floor plans).

    Floor plans are the single most useful input for the den question, so they
    are pulled out and always sent to the vision pass even when we sample only
    a handful of regular photos.
    """
    photos: list[str] = []
    floorplans: list[str] = []

    def classify(url: str, label: str = "") -> None:
        if not url or not url.startswith("http"):
            return
        haystack = f"{url} {label}".lower()
        target = floorplans if ("floor" in haystack or "plan" in haystack) else photos
        if url not in target:
            target.append(url)

    for entry in _as_list(_first(item, "responsivePhotos", "photos", "images")):
        if isinstance(entry, str):
            classify(entry)
            continue
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("caption") or entry.get("subjectType") or "")
        # responsivePhotos nests the real URLs under mixedSources.jpeg[]
        mixed = entry.get("mixedSources") or {}
        jpegs = mixed.get("jpeg") if isinstance(mixed, dict) else None
        if jpegs:
            best = max(jpegs, key=lambda j: j.get("width") or 0)
            classify(best.get("url", ""), label)
            continue
        classify(str(_first(entry, "url", "src", "href") or ""), label)

    for entry in _as_list(item.get("floorPlans")):
        if isinstance(entry, str):
            classify(entry, "floorplan")
        elif isinstance(entry, dict):
            classify(str(_first(entry, "url", "src") or ""), "floorplan")

    return photos, floorplans


def _as_list(value: Any) -> Iterable[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize(item: dict[str, Any], zpid: str | None = None) -> Listing | None:
    """Apify item -> Listing. Returns None if it lacks a usable identity."""
    resolved = str(_first(item, "zpid", "id") or zpid or "").strip()
    if not resolved:
        return None

    photos, floorplans = _photo_urls(item)
    reso = item.get("resoFacts") or {}

    # livingArea comes through as a number, a string with units, or only inside
    # resoFacts depending on the listing and the actor version.
    sqft = _to_int(
        _first(item, "livingAreaValue", "livingArea", "floorSize")
        or (reso.get("livingArea") if isinstance(reso, dict) else None)
    )
    # Guard against a unit price or year sneaking into the sqft slot.
    if sqft is not None and not (100 <= sqft <= 20000):
        sqft = None

    return Listing(
        zpid=resolved,
        url=str(
            _first(item, "hdpUrl", "url", "detailUrl")
            or f"https://www.zillow.com/homedetails/{resolved}_zpid/"
        ),
        address=_address(_first(item, "address", "streetAddress", "abbreviatedAddress")),
        price=_to_int(_first(item, "price", "unformattedPrice", "rentZestimate")),
        beds=_to_float(_first(item, "bedrooms", "beds")),
        baths=_to_float(_first(item, "bathrooms", "baths")),
        sqft=sqft,
        description=str(_first(item, "description", "homeDescription") or ""),
        photos=photos,
        floorplans=floorplans,
        lat=_to_float(_first(item, "latitude", "lat")),
        lng=_to_float(_first(item, "longitude", "lng", "long")),
        reso_facts=reso if isinstance(reso, dict) else {},
    )


def _run_actor(
    actor: str, urls: list[str], token: str, timeout: int = RUN_TIMEOUT
) -> list[dict[str, Any]]:
    endpoint = f"{APIFY_BASE}/{actor}/run-sync-get-dataset-items"
    payload = {
        "startUrls": [{"url": u} for u in urls],
        "propertyStatus": "FOR_RENT",
        "extractBuildingUnits": True,
    }
    resp = requests.post(
        endpoint,
        params={"token": token, "timeout": timeout},
        json=payload,
        timeout=timeout + 30,
    )
    if resp.status_code >= 400:
        raise EnrichmentError(f"{actor} returned {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data if isinstance(data, list) else []


def fetch_details(zpids: list[str], token: str) -> list[Listing]:
    """Enrich a batch of zpids. Falls back to a second actor before giving up.

    Failures are raised rather than swallowed so the caller can leave the zpids
    unrecorded and retry them on the next cycle — dropping a listing silently
    is the one outcome worth avoiding.
    """
    if not zpids:
        return []

    urls = [f"https://www.zillow.com/homedetails/{z}_zpid/" for z in zpids]

    try:
        items = _run_actor(PRIMARY_ACTOR, urls, token)
    except (EnrichmentError, requests.RequestException) as primary_error:
        try:
            items = _run_actor(FALLBACK_ACTOR, urls, token)
        except (EnrichmentError, requests.RequestException) as fallback_error:
            raise EnrichmentError(
                f"both actors failed; primary={primary_error} fallback={fallback_error}"
            ) from fallback_error

    listings: list[Listing] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        listing = normalize(item)
        if listing:
            listings.append(listing)
    return listings
