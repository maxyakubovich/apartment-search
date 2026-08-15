"""Does this apartment have a room Taryn can actually work in?

The requirement is narrower than "has a den". Taryn runs virtual therapy
sessions with children and adolescents, so the space has to be private in a
clinical sense: a door that closes, no one walking through mid-session, and
nothing of the session visible or audible from the rest of the apartment. A
desk nook off the living room fails that test no matter what the listing calls
it, and a windowless walk-through "office" fails it too.

Two stages, to keep image cost off the common path. Stage 1 reads the listing
text. If that lands confidently at either end we stop. Only genuinely ambiguous
listings pay for the vision pass over floor plans and photos.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import requests

from .models import DenVerdict, Listing

IMAGE_TIMEOUT = 20
MAX_IMAGE_BYTES = 4 * 1024 * 1024

REPORT_TOOL = {
    "name": "report_den",
    "description": "Report whether this apartment has a private, door-closing workspace.",
    "input_schema": {
        "type": "object",
        "properties": {
            "den_conf": {
                "type": "number",
                "description": (
                    "0.0-1.0 confidence that this unit has a room, beyond the "
                    "primary bedroom, that can be fully closed off with a door "
                    "and used for private video therapy sessions. 0.0 means "
                    "certainly not, 1.0 means clearly documented."
                ),
            },
            "has_door": {
                "type": ["boolean", "null"],
                "description": "Does the space close with a door? null if undeterminable.",
            },
            "is_passthrough": {
                "type": ["boolean", "null"],
                "description": (
                    "Must someone walk through this space to reach a bedroom, "
                    "bathroom, or kitchen? null if undeterminable."
                ),
            },
            "evidence": {
                "type": "string",
                "description": (
                    "One sentence citing the specific listing phrase or visual "
                    "detail behind the score. Quote the listing where possible."
                ),
            },
            "concerns": {
                "type": "string",
                "description": (
                    "One sentence on anything that would compromise privacy: "
                    "open-plan layout, glass doors, no door, sightlines from "
                    "shared space. Empty string if none."
                ),
            },
        },
        "required": ["den_conf", "has_door", "is_passthrough", "evidence", "concerns"],
    },
}

SYSTEM_PROMPT = """\
You assess rental listings for one specific requirement.

The tenant is a therapist who runs virtual sessions with children and \
adolescents from home. She needs a workspace that is private in a clinical \
sense, because client confidentiality depends on it:

- It closes with a real door. Curtains, open archways, half-walls, and \
"nooks" do not count.
- Nobody has to walk through it to reach a bedroom, bathroom, or kitchen \
during a session.
- It is a distinct space, not a corner of the living room or bedroom.
- It is separate from the primary bedroom, which is used for sleeping.

A second bedroom satisfies this, as long as it is not a walk-through.

Score conservatively but do not require the listing to use the word "den". \
Infer from floor plans and photos when the text is silent: an unlabeled room \
with a door on a floor plan is strong evidence. A large square-footage number \
alone is weak evidence and should not push confidence above 0.4 on its own, \
because open-plan lofts are large and have no private rooms at all.

Report through the report_den tool."""


def _relevant_facts(listing: Listing) -> dict[str, Any]:
    """The resoFacts keys that actually bear on layout, not the whole blob."""
    keys = (
        "roomTypes",
        "otherRooms",
        "rooms",
        "atAGlanceFacts",
        "interiorFeatures",
        "homeType",
        "livingArea",
        "stories",
        "hasAdditionalParcels",
    )
    facts = listing.reso_facts or {}
    return {k: facts[k] for k in keys if k in facts and facts[k]}


def _describe(listing: Listing) -> str:
    return json.dumps(
        {
            "address": listing.address,
            "price_per_month": listing.price,
            "bedrooms": listing.beds,
            "bathrooms": listing.baths,
            "square_feet": listing.sqft,
            "description": listing.description[:6000],
            "facts": _relevant_facts(listing),
        },
        indent=2,
    )


def _call(client, model: str, content: Any) -> DenVerdict | None:
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[REPORT_TOOL],
            tool_choice={"type": "tool", "name": "report_den"},
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:  # noqa: BLE001 - surface as unknown, never crash the loop
        return DenVerdict.unknown(f"analysis failed: {exc}")

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            data = block.input
            return DenVerdict(
                den_conf=max(0.0, min(1.0, float(data.get("den_conf", 0.0)))),
                has_door=data.get("has_door"),
                is_passthrough=data.get("is_passthrough"),
                evidence=str(data.get("evidence", "")).strip(),
                concerns=str(data.get("concerns", "")).strip(),
            )
    return None


def _fetch_image(url: str) -> dict[str, Any] | None:
    try:
        resp = requests.get(url, timeout=IMAGE_TIMEOUT)
        if resp.status_code != 200 or len(resp.content) > MAX_IMAGE_BYTES:
            return None
        media_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            return None
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(resp.content).decode(),
            },
        }
    except requests.RequestException:
        return None


def analyze(listing: Listing, config: dict[str, Any], client) -> DenVerdict:
    """Text pass, then a vision pass only if the text was inconclusive."""
    models = config["models"]
    band_low, band_high = config["den"]["escalate_band"]

    text_verdict = _call(
        client,
        models["text"],
        f"Assess this rental listing:\n\n{_describe(listing)}",
    )
    if text_verdict is None:
        text_verdict = DenVerdict.unknown("model returned no structured output")
    # `unknown()` marks itself "skipped"; preserve that so a failed text pass
    # short-circuits instead of being relabelled as a real result.
    if text_verdict.stage != "skipped":
        text_verdict.stage = "text"

    if text_verdict.stage == "skipped":
        return text_verdict

    decisive = text_verdict.den_conf < band_low or text_verdict.den_conf > band_high
    images_available = bool(listing.floorplans or listing.photos)
    if decisive or not images_available:
        return text_verdict

    # Floor plans answer the question directly, so they always go first and are
    # never crowded out by the interior-photo sample.
    urls = listing.floorplans + listing.photos[: config["den"]["max_photos"]]
    blocks: list[dict[str, Any]] = []
    for url in urls:
        image = _fetch_image(url)
        if image:
            blocks.append(image)

    if not blocks:
        return text_verdict

    blocks.append(
        {
            "type": "text",
            "text": (
                f"Assess this rental listing. The images above are its floor plans "
                f"and interior photos ({len(listing.floorplans)} floor plan(s) first).\n\n"
                f"{_describe(listing)}\n\n"
                f"A text-only read scored {text_verdict.den_conf:.2f} and was "
                f"inconclusive. Use the images to settle it."
            ),
        }
    )

    vision_verdict = _call(client, models["vision"], blocks)
    if vision_verdict is None or vision_verdict.stage == "skipped":
        return text_verdict
    vision_verdict.stage = "vision"
    return vision_verdict
