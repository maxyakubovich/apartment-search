"""Shared data shapes.

`Listing` is the normalized form every source and enricher produces, so the
filter ladder and notifier never have to know where a listing came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Listing:
    zpid: str
    url: str
    address: str | None = None
    price: int | None = None
    beds: float | None = None
    baths: float | None = None
    # None means Zillow did not publish a square footage, which is common on
    # landlord-posted SF rentals. Distinct from 0 and handled separately.
    sqft: int | None = None
    description: str = ""
    photos: list[str] = field(default_factory=list)
    floorplans: list[str] = field(default_factory=list)
    lat: float | None = None
    lng: float | None = None
    reso_facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        """Identity that survives the id changing underneath us.

        Floor plans are keyed on the scraper's zpid when it supplies one and a
        synthesised slug when it does not, so the same apartment can arrive
        under two different ids on two different runs and defeat dedup. Address
        plus bed count plus floor area does not move. Price is excluded
        deliberately — it changes, and a price drop should still re-alert.
        """
        import re

        address = re.sub(r"[^a-z0-9]+", "", (self.address or "").lower())
        beds = int(self.beds) if self.beds is not None else "?"
        return f"{address}|{beds}|{self.sqft or '?'}"


@dataclass
class DenVerdict:
    """Result of the den analysis.

    `den_conf` is the only field the ladder consumes; the rest exist so the
    Telegram message can explain itself and so bad calls are debuggable.
    """

    den_conf: float
    has_door: bool | None
    is_passthrough: bool | None
    evidence: str
    # A room explicitly labelled den/office/study on the plan or in the
    # text, whether or not it passes the door test. Tracked separately so
    # a real den that falls short is surfaced rather than silently dropped.
    den_labeled: bool | None = None
    # True only when the unit is affirmatively open-plan with no
    # separable space — gates the weakest ladder rung.
    is_open_plan: bool | None = None
    concerns: str = ""
    # "text" when stage 1 was decisive, "vision" when we escalated to images.
    stage: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def unknown(reason: str) -> "DenVerdict":
        """Used when analysis could not run — never blocks the ladder outright,
        it just means the listing has to clear a higher sqft bar."""
        return DenVerdict(
            den_conf=0.0,
            has_door=None,
            is_passthrough=None,
            evidence="",
            den_labeled=None,
            is_open_plan=None,
            concerns=reason,
            stage="skipped",
        )


@dataclass
class Decision:
    listing: Listing
    verdict: DenVerdict
    notify: bool
    # Name of the ladder rung that matched, or the reason it was dropped.
    reason: str
    sqft_unlisted: bool = False
