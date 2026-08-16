"""Seen-listing persistence.

A single JSON file in the repo. It is the thing that stops the watcher from
re-notifying you about the same apartment every two minutes, so it has to
survive across runs — hence committing it back rather than using the Actions
cache, which is evictable.

Contents are only zpids, prices and verdicts. If the public-repo visibility of
that ever bothers you, swap `_read`/`_write` for a private Gist and nothing
else in the codebase changes.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path(__file__).parent.parent / "state" / "seen.json"

# Re-notify when a listing we already sent drops by at least this fraction.
PRICE_DROP_THRESHOLD = 0.05

# Buildings keep listing new units, so their pages are refetched periodically
# rather than being marked done forever after the first scrape.
BUILDING_RESCRAPE_INTERVAL = timedelta(hours=12)


class State:
    def __init__(self, path: Path | None = None):
        self.path = path or STATE_PATH
        self._data: dict[str, Any] = self._read()
        self._dirty = False

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"listings": {}, "last_alert_seen": None}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            # A truncated write should not wedge the watcher forever. Losing
            # history costs one round of duplicate notifications, which is a far
            # better failure than crash-looping.
            return {"listings": {}, "last_alert_seen": None}

    @property
    def listings(self) -> dict[str, Any]:
        return self._data.setdefault("listings", {})

    def is_new(self, zpid: str) -> bool:
        return zpid not in self.listings

    def should_renotify(self, zpid: str, price: int | None) -> bool:
        """True when a previously-notified listing has dropped materially."""
        prior = self.listings.get(zpid)
        if not prior or price is None:
            return False
        if not prior.get("notified"):
            return False
        old = prior.get("price")
        if not old:
            return False
        return price <= old * (1 - PRICE_DROP_THRESHOLD)

    def record(
        self,
        zpid: str,
        *,
        price: int | None,
        notified: bool,
        reason: str,
        den_conf: float | None = None,
    ) -> None:
        self.listings[zpid] = {
            "price": price,
            "notified": notified,
            "reason": reason,
            "den_conf": den_conf,
            "seen_at": datetime.now(timezone.utc).isoformat(),
        }
        self._dirty = True

    @property
    def sources(self) -> dict[str, Any]:
        return self._data.setdefault("sources", {})

    def should_scrape_source(self, ident: str, is_building: bool) -> bool:
        """Whether a link from an alert email is worth an Apify call.

        A single-unit page is immutable once scraped, so it is fetched once.
        A building page is not: new units are listed in the same complex over
        time, so it is refetched on a cooldown instead of being retired.
        """
        last = self.sources.get(ident)
        if last is None:
            return True
        if not is_building:
            return False
        try:
            when = datetime.fromisoformat(last)
        except (TypeError, ValueError):
            return True
        return datetime.now(timezone.utc) - when >= BUILDING_RESCRAPE_INTERVAL

    def note_source_scraped(self, ident: str) -> None:
        self.sources[ident] = datetime.now(timezone.utc).isoformat()
        self._dirty = True

    def refresh_price(self, zpid: str, price: int | None) -> None:
        """Update the tracked price without touching the notified flag.

        Overwriting the whole record here would erase the fact that we already
        sent this listing, which is exactly what `should_renotify` relies on.
        """
        prior = self.listings.get(zpid)
        if prior is None or price is None or prior.get("price") == price:
            return
        prior["price"] = price
        self._dirty = True

    def get_flag(self, key: str) -> Any:
        return self._data.get(key)

    def set_flag(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._dirty = True

    def note_alert_seen(self) -> None:
        """Timestamp of the last Zillow alert email we successfully parsed.

        Drives the staleness heartbeat: alerts arriving but nothing parsed means
        the email format changed underneath us.
        """
        self._data["last_alert_seen"] = datetime.now(timezone.utc).isoformat()
        self._dirty = True

    def save(self) -> bool:
        """Write only when something changed. Returns whether a write happened."""
        if not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")
        self._dirty = False
        return True

    def commit(self) -> None:
        """Persist state back to the repo. No-op outside a git checkout."""
        try:
            subprocess.run(
                ["git", "add", str(self.path)],
                check=True,
                capture_output=True,
                cwd=self.path.parent.parent,
            )
            result = subprocess.run(
                ["git", "commit", "-m", "chore: update seen listings [skip ci]"],
                capture_output=True,
                cwd=self.path.parent.parent,
            )
            # Exit code 1 with no staged changes is the normal "nothing to do"
            # path, not an error worth surfacing.
            if result.returncode == 0:
                repo = self.path.parent.parent
                # The loop runs for hours and pushes repeatedly, so the remote
                # may well have moved (a code push, or the previous run's final
                # commit). Rebase first rather than letting the push bounce.
                subprocess.run(
                    ["git", "pull", "--rebase", "--autostash"],
                    capture_output=True,
                    cwd=repo,
                )
                subprocess.run(["git", "push"], capture_output=True, cwd=repo)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
