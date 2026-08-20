"""Orchestrator.

    python -m src.main --test-telegram      verify bot wiring
    python -m src.main --once --dry-run     one cycle, decide but send nothing
    python -m src.main --backfill 20 --dry-run
                                            replay recent alerts, print the
                                            full decision trace (accuracy check)
    python -m src.main --once               one live cycle
    python -m src.main --loop               continuous; what CI runs

Order of operations is chosen so money is spent as late as possible: dedupe
against state first, then the free numeric gates, and only then Apify and
Claude.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from . import den as den_module
from . import enrich, notify
from .config import load_config, load_dotenv, optional, require
from .filters import evaluate, hard_filter
from .sources.email_imap import SourceLink, fetch_alert_emails, source_links_from_email
from .state import State

PARSER_WARNING_KEY = "last_parser_warning"


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def _anthropic_client():
    import anthropic

    return anthropic.Anthropic(api_key=require("ANTHROPIC_API_KEY"))


def _warn_parser_stalled(state: State, token: str, chat_id: str, dry_run: bool) -> None:
    """Alerts arriving but nothing parseable means Zillow changed their template.

    Worth an explicit ping: the failure mode otherwise is total silence, which
    is indistinguishable from a quiet market.
    """
    last = state.get_flag(PARSER_WARNING_KEY)
    if last:
        when = datetime.fromisoformat(last)
        if datetime.now(timezone.utc) - when < timedelta(hours=24):
            return
    msg = (
        "⚠️ Zillow alert emails are arriving but no listing links could be "
        "parsed from them. The email template likely changed — the watcher "
        "needs its parser updated."
    )
    _log(msg)
    if not dry_run:
        notify.send_text(token, chat_id, msg)
    state.set_flag(PARSER_WARNING_KEY, datetime.now(timezone.utc).isoformat())


def cycle(
    config: dict,
    state: State,
    *,
    dry_run: bool = False,
    backfill: int | None = None,
) -> int:
    """One pass. Returns the number of listings notified."""
    gmail = require("GMAIL_ADDRESS")
    gmail_pw = require("GMAIL_APP_PASSWORD")
    tg_token = optional("TELEGRAM_BOT_TOKEN")
    tg_chat = optional("TELEGRAM_CHAT_ID")

    lookback = config["runtime"]["imap_lookback_hours"]
    limit = backfill or 50
    if backfill:
        lookback = max(lookback, 24 * 30)

    saved_searches = config["search"].get("saved_search_enrollment_ids") or []

    alerts = fetch_alert_emails(gmail, gmail_pw, lookback_hours=lookback, limit=limit)
    if not alerts:
        _log("no Zillow alerts in window")
        return 0

    links: list[SourceLink] = []
    price_change_idents: set[str] = set()
    for alert in alerts:
        found = source_links_from_email(alert, saved_searches)
        known = {link.ident for link in links}
        links.extend(link for link in found if link.ident not in known)
        # Price-change alerts are the one case worth re-examining something we
        # have already scraped and either sent or dismissed.
        if "price" in alert.subject.lower():
            price_change_idents.update(link.ident for link in found)

    _log(f"{len(alerts)} alert email(s), {len(links)} result link(s) across {len(saved_searches) or 'all'} search(es)")

    if not links:
        _warn_parser_stalled(state, tg_token, tg_chat, dry_run)
        return 0

    state.note_alert_seen()

    if backfill:
        targets = links
    else:
        targets = [
            link
            for link in links
            if link.ident in price_change_idents
            or state.should_scrape_source(link.ident, link.is_building)
        ]

    if not targets:
        _log("nothing new")
        return 0

    buildings = sum(1 for link in targets if link.is_building)
    _log(f"enriching {len(targets)} link(s) ({buildings} building page(s))")
    try:
        listings = enrich.fetch_details(
            [link.url for link in targets], require("APIFY_TOKEN")
        )
    except enrich.EnrichmentError as exc:
        # Leave these unrecorded so the next cycle retries, rather than
        # treating a transient scraper failure as "already handled".
        _log(f"enrichment failed, will retry next cycle: {exc}")
        return 0

    if not backfill:
        for link in targets:
            state.note_source_scraped(link.ident)

    _log(f"  -> {len(listings)} unit(s) returned")

    client = _anthropic_client()
    notified = 0

    for listing in listings:
        passed, drop_reason = hard_filter(listing, config)
        if not passed:
            _log(f"  {listing.zpid} dropped — {drop_reason}")
            state.record(
                listing.zpid,
                price=listing.price,
                notified=False,
                reason=drop_reason,
            )
            continue

        # Seen-check before the Claude call, not after: a listing we are not
        # going to send is not worth analysing.
        if not backfill and not state.is_new(listing.zpid):
            if not state.should_renotify(listing.zpid, listing.price):
                _log(f"  {listing.zpid} skip — seen, no material price drop")
                state.refresh_price(listing.zpid, listing.price)
                continue
            _log(f"  {listing.zpid} price dropped to ${listing.price:,} — re-checking")

        verdict = den_module.analyze(listing, config, client)
        decision = evaluate(listing, verdict, config)

        mark = "SEND" if decision.notify else "skip"
        _log(
            f"  {listing.zpid} {mark} — {decision.reason} "
            f"(den {verdict.den_conf:.2f} via {verdict.stage}, "
            f"{listing.sqft or '?'} sqft, ${listing.price or '?'})"
        )
        if verdict.evidence:
            _log(f"      evidence: {verdict.evidence}")

        if decision.notify and not dry_run:
            if notify.send_listing(tg_token, tg_chat, decision):
                notified += 1
            else:
                _log(f"  {listing.zpid} telegram send failed")
        elif decision.notify:
            notified += 1

        if not backfill:
            state.record(
                listing.zpid,
                price=listing.price,
                notified=decision.notify,
                reason=decision.reason,
                den_conf=verdict.den_conf,
            )

    return notified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SF apartment watcher")
    parser.add_argument("--once", action="store_true", help="run a single cycle")
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument("--dry-run", action="store_true", help="decide but never send")
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="N",
        help="replay the last N alert emails ignoring seen-state",
    )
    parser.add_argument(
        "--test-telegram", action="store_true", help="send a test message and exit"
    )
    parser.add_argument(
        "--seed-state",
        action="store_true",
        help="mark everything currently visible as seen, without notifying",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    config = load_config()

    if args.test_telegram:
        ok = notify.send_text(
            require("TELEGRAM_BOT_TOKEN"),
            require("TELEGRAM_CHAT_ID"),
            "🏠 Apartment watcher wired up correctly.",
        )
        print("sent" if ok else "FAILED — check token and chat id")
        return 0 if ok else 1

    state = State()

    if args.backfill:
        count = cycle(config, state, dry_run=True, backfill=args.backfill)
        print(f"\n{count} listing(s) would have been sent.")
        return 0

    if args.seed_state:
        # Without this the first live run notifies every match already sitting
        # in the mailbox window at once. Records them as seen so the watcher
        # starts quiet and only reports genuinely new listings.
        count = cycle(config, state, dry_run=True)
        state.save()
        state.commit()
        print(f"\nSeeded. {count} existing match(es) marked seen, none sent.")
        print("The watcher will now only notify on listings newer than this.")
        return 0

    if args.once or not args.loop:
        cycle(config, state, dry_run=args.dry_run)
        if state.save() and not args.dry_run:
            state.commit()
        return 0

    started = time.monotonic()
    poll = config["runtime"]["poll_seconds"]
    budget = config["runtime"]["max_loop_seconds"]
    _log(f"loop starting — every {poll}s for up to {budget}s")

    while time.monotonic() - started < budget:
        try:
            cycle(config, state, dry_run=args.dry_run)
            if state.save() and not args.dry_run:
                state.commit()
        except Exception as exc:  # noqa: BLE001 - a bad cycle must not end the loop
            _log(f"cycle error: {exc}")
        time.sleep(poll)

    _log("loop budget exhausted, exiting for redispatch")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        # Missing-secret errors are the common first-run failure. A one-line
        # message is far more useful here than a traceback.
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
