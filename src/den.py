"""Does this apartment have a room Taryn can actually work in?

The requirement is narrower than "has a den". Taryn runs virtual therapy
sessions with children and adolescents, so the space has to be private in a
clinical sense: a door that closes, no one walking through mid-session, and
nothing of the session visible or audible from the rest of the apartment. A
desk nook off the living room fails that test no matter what the listing calls
it, and a windowless walk-through "office" fails it too.

Two stages, to keep image cost off the common path. Stage 1 reads the listing
text. Stage 2 looks at images.

Whenever a floor plan exists, stage 2 always runs. A low text score nearly
always means the description never mentioned the layout — absence of evidence,
not evidence of absence — and the plan settles it outright. Real listings have
gone from 0.10 on text to 0.75 once the plan was read. The confidence band only
governs the weaker case where all we have is interior photos.
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
                    "0.0-1.0 confidence that at least one room other than "
                    "the primary bedroom closes with a real door and is not a "
                    "pass-through. A second bedroom counts and should score "
                    "high. 0.0 means certainly not, 1.0 means clearly shown."
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
            "is_open_plan": {
                "type": ["boolean", "null"],
                "description": (
                    "True if the unit is affirmatively open-plan or loft-style "
                    "with no separable space at all. Only set true when the "
                    "listing or floor plan positively shows this, not merely "
                    "when the layout is unstated."
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
        "required": [
                "den_conf",
                "has_door",
                "is_passthrough",
                "is_open_plan",
                "evidence",
                "concerns",
            ],
    },
}

SYSTEM_PROMPT = """\
You assess rental listings for one specific requirement.

The tenant is a therapist who runs virtual sessions with children and \
adolescents from home. She needs a workspace that is private in a clinical \
sense, because client confidentiality depends on it.

Score exactly one question: **is there at least one room, other than the \
primary bedroom, that closes with a real door and is not a pass-through?**

- A second bedroom counts. It does not matter that it is labelled "bedroom" \
rather than "den" — a door and four walls is what the requirement is about. \
A two-bedroom unit whose bedrooms both open off the living room should score \
high, not low.
- A den, office, study, or unlabelled room with a door counts.
- Curtains, open archways, half-walls, "nooks", dressing areas and lofts \
open to below do NOT count.
- The room must not be the only route to a bedroom, bathroom, or kitchen.
- The primary bedroom itself never counts; it is used for sleeping.

Do not deduct for the absence of the word "den". Do not look for a *third* \
room in a two-bedroom unit — the second bedroom is the answer.

Infer from floor plans and photos when the text is silent: an unlabelled \
room with a door on a floor plan is strong evidence, and a listing that simply \
never describes its layout is unknown rather than negative. A large square \
footage alone is weak evidence and should not push confidence above 0.4 on its \
own, because open-plan lofts are large and have no private rooms at all.

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


def _term_hint(config: dict[str, Any]) -> str:
    """Surface the configured vocabulary as investigative hints.

    Deliberately framed as signals rather than rules — a hard keyword match
    would both miss unlabelled floor-plan rooms and fire on "cozy office nook",
    which is exactly the thing that fails the requirement.
    """
    terms = config.get("den", {}).get("positive_terms") or []
    if not terms:
        return ""
    return (
        "Phrases that often indicate a qualifying space — treat them as prompts "
        "to look closer, not as proof, and note that some (an open 'nook' or "
        "'alcove') frequently fail the door requirement: " + ", ".join(terms) + "."
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
                is_open_plan=data.get("is_open_plan"),
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
        f"Assess this rental listing:\n\n{_describe(listing)}\n\n{_term_hint(config)}",
    )
    if text_verdict is None:
        text_verdict = DenVerdict.unknown("model returned no structured output")
    # `unknown()` marks itself "skipped"; preserve that so a failed text pass
    # short-circuits instead of being relabelled as a real result.
    if text_verdict.stage != "skipped":
        text_verdict.stage = "text"

    if text_verdict.stage == "skipped":
        return text_verdict

    # A floor plan settles the question outright, and a low text score usually
    # means the description simply never mentioned the layout — absence of
    # evidence, not evidence of absence. Listings have been seen to go from
    # 0.10 on text to 0.75 once the plan was read, so whenever a plan exists it
    # is always worth looking at. The band only governs the weaker case where
    # all we have is interior photos.
    if listing.floorplans:
        decisive = False
    else:
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
                f"{_term_hint(config)}\n\n"
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
