#!/usr/bin/env python3
"""Let the current ladder re-judge everything it has not already sent.

    python3 scripts/reset_unnotified.py [--dry-run]

State is what stops re-notification, but it also freezes old verdicts: a
listing recorded as "clears no ladder rung" under yesterday's logic is never
reconsidered under today's, because is_new() returns False. Every time the
ladder or the den prompt changes, the listings it would now catch are exactly
the ones already marked seen.

This drops every not-yet-notified record and clears the scraped-source
cooldowns, so the next cycle re-derives and re-judges them. Records that were
actually sent are kept, so nothing is delivered twice.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STATE = Path(__file__).parent.parent / "state" / "seen.json"


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not STATE.exists():
        print("No state file — nothing to reset.")
        return 0

    data = json.loads(STATE.read_text())
    listings = data.get("listings", {})

    kept = {k: v for k, v in listings.items() if v.get("notified")}
    dropped = len(listings) - len(kept)
    sources = len(data.get("sources", {}))

    print(f"  {len(listings)} recorded, {len(kept)} already sent (kept)")
    print(f"  {dropped} not sent -> cleared for re-judgement")
    print(f"  {sources} source cooldown(s) cleared so buildings re-scrape")

    reconsidered = [
        (k, v) for k, v in listings.items()
        if not v.get("notified") and "ladder rung" in str(v.get("reason", ""))
    ]
    if reconsidered:
        print(f"\n  {len(reconsidered)} of those were den-analysis rejections:")
        for key, value in sorted(reconsidered, key=lambda kv: kv[1].get("price") or 0)[:8]:
            print(f"    {key:12} ${value.get('price')}  {str(value.get('reason'))[:46]}")

    if dry:
        print("\n  --dry-run: nothing written.")
        return 0

    data["listings"] = kept
    data["sources"] = {}
    STATE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print("\n✓ Written. The next cycle re-judges these under the current ladder.")
    print("  The 15 already sent stay suppressed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
