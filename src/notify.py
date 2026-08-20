"""Telegram delivery.

Messages are written to be triaged from a phone lock screen: address and price
first, then the one thing that decides whether it is worth opening — whether
the private-room requirement is actually met, and what the evidence was.
"""

from __future__ import annotations

import html

import requests

from .models import Decision

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20
# Telegram truncates media-group captions at 1024 characters.
CAPTION_LIMIT = 1024

RUNG_LABELS = {
    "two_bedroom": "2BR — second bedroom works as the office",
    "confident_den": "Den confirmed",
    "plausible_den": "Den likely",
    "labeled_den": "Listing labels a den — but check the caveat",
    "room_to_sequester": "No den found, but room to wall off a desk",
}


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _headline(decision: Decision) -> str:
    listing = decision.listing
    bits = []
    if listing.price:
        bits.append(f"${listing.price:,}/mo")
    if listing.beds is not None:
        bits.append(f"{int(listing.beds)} bd")
    if listing.baths is not None:
        baths = int(listing.baths) if listing.baths == int(listing.baths) else listing.baths
        bits.append(f"{baths} ba")
    if listing.sqft:
        bits.append(f"{listing.sqft:,} sqft")
    elif decision.sqft_unlisted:
        bits.append("sqft unlisted")
    return " · ".join(bits)


def format_message(decision: Decision) -> str:
    listing = decision.listing
    verdict = decision.verdict

    label = RUNG_LABELS.get(decision.reason, decision.reason)

    # On the 2BR rung the second bedroom is itself the private room, so the
    # den score is measuring something you do not care about. Printing it
    # would make a perfectly good flat look like a weak match.
    show_conf = decision.reason != "two_bedroom"
    if decision.reason == "two_bedroom":
        icon = "🟢"
    else:
        icon = "🟢" if verdict.den_conf >= 0.6 else "🟡" if verdict.den_conf >= 0.3 else "⚪"

    headline = f"{icon} <b>{_esc(label)}</b>"
    if show_conf:
        headline += f" (confidence {verdict.den_conf:.2f})"

    lines = [
        f"🏠 <b>{_esc(listing.address or 'Address not listed')}</b>",
        _esc(_headline(decision)),
        "",
        headline,
    ]

    if verdict.evidence:
        lines.append(f"<i>{_esc(verdict.evidence)}</i>")

    flags = []
    if verdict.has_door is True:
        flags.append("has a door")
    elif verdict.has_door is False:
        flags.append("no door")
    if verdict.is_passthrough is True:
        flags.append("⚠️ walk-through")
    elif verdict.is_passthrough is False:
        flags.append("not a walk-through")
    if flags:
        lines.append(_esc(" · ".join(flags)))

    if verdict.concerns:
        lines.append(f"⚠️ {_esc(verdict.concerns)}")

    if decision.sqft_unlisted:
        lines.append("⚠️ Zillow did not publish square footage for this unit.")

    lines.append("")
    lines.append(f'<a href="{_esc(listing.url)}">View on Zillow</a>')

    return "\n".join(lines)


def send_text(token: str, chat_id: str, text: str) -> bool:
    resp = requests.post(
        API.format(token=token, method="sendMessage"),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=TIMEOUT,
    )
    return resp.ok


def send_listing(token: str, chat_id: str, decision: Decision) -> bool:
    """Photos plus caption when imagery exists, otherwise a plain message.

    Falls back to text on any media failure — a delivered message without
    pictures beats a silent drop.
    """
    caption = format_message(decision)
    listing = decision.listing
    images = (listing.floorplans[:1] + listing.photos[:2])[:3]

    if images and len(caption) <= CAPTION_LIMIT:
        media = [
            {
                "type": "photo",
                "media": url,
                **({"caption": caption, "parse_mode": "HTML"} if i == 0 else {}),
            }
            for i, url in enumerate(images)
        ]
        try:
            resp = requests.post(
                API.format(token=token, method="sendMediaGroup"),
                json={"chat_id": chat_id, "media": media},
                timeout=TIMEOUT,
            )
            if resp.ok:
                return True
        except requests.RequestException:
            pass

    return send_text(token, chat_id, caption)
