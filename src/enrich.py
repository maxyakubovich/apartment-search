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

import os
import re
from typing import Any, Iterable

import requests

from .models import Listing

APIFY_BASE = "https://api.apify.com/v2/acts"
RUN_TIMEOUT = 240


def _primary_input(urls: list[str]) -> dict[str, Any]:
    # extractBuildingUnits is a string enum, NOT a boolean — passing True makes
    # the actor exit immediately with an empty dataset and no HTTP error.
    # Valid: disabled | all | for_sale | recently_sold | for_rent | off_market
    return {
        "startUrls": [{"url": u} for u in urls],
        "propertyStatus": "FOR_RENT",
        "extractBuildingUnits": "for_rent",
    }


def _fallback_input(urls: list[str]) -> dict[str, Any]:
    # This actor takes detail URLs under `detailsUrl`, not `startUrls`. Getting
    # that wrong is not a no-op: its `listingUrl` defaults to a New York search,
    # so an unrecognised input would return New York rentals as if they were
    # results. Pin listingUrl to empty so only detailsUrl is ever honoured.
    return {
        "detailsUrl": urls,
        "listingUrl": "",
        "maxItems": max(len(urls) * 50, 100),
    }


# Ordered by preference. Each entry carries its own input builder because the
# actors disagree on field names, and a mismatch fails silently rather than loudly.
ACTORS: list[tuple[str, Any]] = [
    ("maxcopell~zillow-detail-scraper", _primary_input),
    ("parseforge~zillow-scraper", _fallback_input),
]


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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "unit"


def is_building(item: dict[str, Any]) -> bool:
    """Building detail records describe a complex, not a rentable unit.

    They carry `floorPlans` and `lotId` but no `bedrooms`, `price`, or
    `livingArea` — so treating one as a listing yields "? sqft, $?" and a
    description that is building marketing copy rather than a unit layout.
    """
    if str(item.get("__typename", "")).lower().startswith("building"):
        return True
    return "floorPlans" in item and "bedrooms" not in item


def _plan_price(plan: dict[str, Any]) -> int | None:
    """Cheapest actually-available unit on this floor plan.

    Ranges are quoted per plan, but you rent one unit. The low end is the
    honest number to filter and display against.
    """
    candidates: list[int] = []
    for unit in _as_list(plan.get("units")):
        if isinstance(unit, dict):
            value = _to_int(_first(unit, "price", "rent", "minPrice"))
            if value:
                candidates.append(value)
    direct = _to_int(_first(plan, "minPrice", "price", "lowPrice"))
    if direct:
        candidates.append(direct)
    return min(candidates) if candidates else None


def _plan_images(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    """A floor plan's own diagram is the single best evidence for the den
    question, so it is collected explicitly rather than left to keyword luck."""
    photos, plans = _photo_urls(plan)
    for key in ("floorPlanUnitPhotos", "floorPlanPhotos", "floorPlanImages"):
        for entry in _as_list(plan.get(key)):
            url = entry if isinstance(entry, str) else str(
                _first(entry, "url", "src") or "" if isinstance(entry, dict) else ""
            )
            if url.startswith("http") and url not in plans:
                plans.append(url)
    return photos, plans


def expand_building(item: dict[str, Any]) -> list[Listing]:
    """One Listing per floor plan in a building record."""
    lot = str(_first(item, "lotId", "zpid", "buildingName") or "building")
    address = _address(_first(item, "fullAddress", "address", "streetAddress"))
    building_name = _first(item, "buildingName")
    base_description = str(_first(item, "description") or "")
    gallery, _ = _photo_urls(item)
    url = str(_first(item, "bdpUrl", "url") or "")
    if url and not url.startswith("http"):
        url = "https://www.zillow.com" + url

    out: list[Listing] = []
    for plan in _as_list(item.get("floorPlans")):
        if not isinstance(plan, dict):
            continue
        name = str(_first(plan, "name", "floorPlanName", "modelName") or "")
        beds = _to_float(_first(plan, "beds", "bedrooms"))
        baths = _to_float(_first(plan, "baths", "bathrooms"))
        sqft = _to_int(_first(plan, "sqft", "minSqft", "livingArea", "squareFeet"))
        if sqft is not None and not (100 <= sqft <= 20000):
            sqft = None

        photos, floorplans = _plan_images(plan)
        ident = str(_first(plan, "zpid") or f"{lot}-{_slug(name)}")

        # The floor plan name carries the layout hint the building blurb lacks
        # ("A4 - 1 Bed + Den"), so it leads the text the den analysis sees.
        parts = [p for p in (building_name, name and f"Floor plan: {name}") if p]
        description = "\n".join([*parts, base_description]).strip()

        out.append(
            Listing(
                zpid=ident,
                url=url or f"https://www.zillow.com/apartments/{lot}/",
                address=", ".join(p for p in (name, address) if p) or address,
                price=_plan_price(plan),
                beds=beds,
                baths=baths,
                sqft=sqft,
                description=description,
                photos=photos or gallery,
                floorplans=floorplans,
                lat=_to_float(_first(item, "latitude", "lat")),
                lng=_to_float(_first(item, "longitude", "lng")),
                reso_facts={"buildingName": building_name} if building_name else {},
            )
        )
    return out


def _run_actor(
    actor: str, build_input: Any, urls: list[str], token: str, timeout: int = RUN_TIMEOUT
) -> list[dict[str, Any]]:
    endpoint = f"{APIFY_BASE}/{actor}/run-sync-get-dataset-items"
    resp = requests.post(
        endpoint,
        params={"token": token, "timeout": timeout},
        json=build_input(urls),
        timeout=timeout + 30,
    )
    if resp.status_code >= 400:
        raise EnrichmentError(f"{actor} returned {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    if not isinstance(data, list):
        raise EnrichmentError(f"{actor} returned {type(data).__name__}, expected a list")
    if not data:
        # An input the actor rejects produces an empty dataset and a 2xx, which
        # is indistinguishable from success unless we treat it as a failure.
        raise EnrichmentError(f"{actor} returned no items for {len(urls)} url(s)")
    return data


def fetch_details(urls: list[str], token: str) -> list[Listing]:
    """Enrich a batch of listing URLs. Falls back to a second actor before
    giving up.

    Takes URLs rather than zpids because saved-search alerts frequently link to
    whole-building pages that have no zpid at all. `extractBuildingUnits` fans
    those out into one item per unit, each with its own real zpid, which is the
    granularity notifications and dedup should work at.

    Failures are raised rather than swallowed so the caller can leave these
    unrecorded and retry on the next cycle — dropping a listing silently is the
    one outcome worth avoiding.
    """
    if not urls:
        return []

    items: list[dict[str, Any]] = []
    failures: list[str] = []
    for actor, build_input in ACTORS:
        try:
            items = _run_actor(actor, build_input, urls, token)
            break
        except (EnrichmentError, requests.RequestException) as exc:
            failures.append(f"{actor}: {exc}")
    if not items:
        raise EnrichmentError("; ".join(failures))

    listings: list[Listing] = []
    buildings = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if is_building(item):
            buildings += 1
            expanded = expand_building(item)
            if os.environ.get("WATCHER_DUMP_RAW") and not expanded:
                print(
                    f"[enrich] building {item.get('buildingName') or item.get('lotId')}"
                    f" produced no floor plans; keys present: "
                    + ", ".join(k for k in ("floorPlans", "bestMatchedUnit") if k in item)
                )
            listings.extend(expanded)
            continue
        listing = normalize(item)
        if listing:
            listings.append(listing)

    if os.environ.get("WATCHER_DUMP_RAW"):
        print(
            f"[enrich] {len(urls)} url(s) in -> {len(items)} record(s) "
            f"({buildings} building) -> {len(listings)} listing(s)"
        )
        if len(items) < len(urls):
            # Usually dead listings: a 30-day backfill replays alerts for units
            # that have since been taken down. Worth showing rather than hiding.
            print(f"[enrich] {len(urls) - len(items)} url(s) returned nothing")
        for sample in listings[:2]:
            print(
                f"[enrich] sample: {sample.address!r} {sample.beds}bd "
                f"{sample.sqft}sqft ${sample.price} "
                f"{len(sample.floorplans)}fp/{len(sample.photos)}photos"
            )

    return listings
