"""Hard gates and the notify ladder.

Two distinct stages, deliberately separated:

`hard_filter` runs on cheap data and drops listings before they cost anything —
no Apify enrichment, no Claude call. `evaluate` runs afterward and decides
whether a surviving listing is worth a Telegram push.

The ladder encodes the actual requirement: den confidence sets the square
footage bar rather than acting as a gate of its own. A confirmed den earns a
lower bar; a listing with no den signal has to be large enough to wall off a
desk on its own.
"""

from __future__ import annotations

from typing import Any

from .models import Decision, DenVerdict, Listing


def point_in_polygon(lat: float, lng: float, polygon: list[list[float]]) -> bool:
    """Ray-casting test. Polygon is [[lng, lat], ...] to match Zillow's ordering.

    Hand-rolled rather than pulling in shapely: the dependency is heavy, the
    algorithm is fifteen lines, and it runs in a CI job we want to stay fast.
    """
    inside = False
    n = len(polygon)
    if n < 3:
        return True
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        # Does the edge straddle the horizontal ray, and is the crossing east of us?
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < x_cross:
                inside = not inside
        j = i
    return inside


def hard_filter(listing: Listing, config: dict[str, Any]) -> tuple[bool, str]:
    """Cheap gates. Returns (passed, reason_if_dropped)."""
    search = config["search"]

    if listing.price is not None and listing.price > search["max_rent"]:
        return False, f"price ${listing.price:,} over ${search['max_rent']:,} cap"

    if listing.beds is not None:
        beds = int(listing.beds)
        if beds not in search["allowed_beds"]:
            return False, f"{beds}BR outside {search['allowed_beds']}"

    # A missing sqft is unknown, not zero — it survives here and is handled by
    # the stricter missing-sqft rule in `evaluate`.
    if listing.sqft is not None and listing.sqft < search["min_sqft_floor"]:
        return False, f"{listing.sqft} sqft below {search['min_sqft_floor']} floor"

    polygon = search.get("polygon")
    if polygon and listing.lat is not None and listing.lng is not None:
        if not point_in_polygon(listing.lat, listing.lng, polygon):
            return False, "outside search boundary"

    return True, ""


def evaluate(
    listing: Listing, verdict: DenVerdict, config: dict[str, Any]
) -> Decision:
    """Apply the notify ladder. First matching rung wins."""
    sqft = listing.sqft
    beds = int(listing.beds) if listing.beds is not None else None

    # Square footage unlisted: we cannot reason about space, so require a
    # confident den and flag the uncertainty in the message.
    if sqft is None:
        threshold = config["missing_sqft"]["min_den_conf"]
        if verdict.den_conf >= threshold:
            return Decision(
                listing=listing,
                verdict=verdict,
                notify=True,
                reason="confident_den (sqft unlisted)",
                sqft_unlisted=True,
            )
        return Decision(
            listing=listing,
            verdict=verdict,
            notify=False,
            reason=(
                f"sqft unlisted and den confidence {verdict.den_conf:.2f} "
                f"below {threshold}"
            ),
            sqft_unlisted=True,
        )

    for rung in config["ladder"]:
        cond = rung["when"]

        if "beds" in cond and beds != cond["beds"]:
            continue
        if "min_den_conf" in cond and verdict.den_conf < cond["min_den_conf"]:
            continue
        if "min_sqft" in cond and sqft < cond["min_sqft"]:
            continue
        # The weakest rung assumes floor area implies a corner that can be
        # walled off. That assumption fails outright in a loft, so it is
        # withdrawn when the layout is affirmatively open-plan.
        if cond.get("not_open_plan") and verdict.is_open_plan is True:
            continue

        return Decision(
            listing=listing, verdict=verdict, notify=True, reason=rung["name"]
        )

    return Decision(
        listing=listing,
        verdict=verdict,
        notify=False,
        reason=(
            f"{sqft} sqft with den confidence {verdict.den_conf:.2f} "
            f"clears no ladder rung"
        ),
    )
