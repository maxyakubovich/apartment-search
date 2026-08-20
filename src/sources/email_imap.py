"""Zillow saved-search alerts, read over IMAP.

Written against real alert emails rather than assumptions, which corrected two
things worth recording:

1. The zpid is already present in the click-tracking href, inside its
   URL-encoded `target=` parameter. No redirect following is needed at all, so
   discovery makes zero network requests and never touches bot protection.

2. Genuine saved-search results are not always `_zpid` links. Digest emails
   ("2 Rental Results for ...") frequently link to whole-building pages
   (`/apartments/san-francisco-ca/argenta/5Xj7m7/`) which carry no zpid — while
   the `_zpid` links further down sit under "Other rentals you might like" and
   are paid or recommended placements, often in Oakland or Alameda.

So the discriminator is not the URL shape. It is `utm_content`: real results
are tagged exactly `forrentimage` / `forrentaddress`, and recommendations carry
a `-_rid-...` suffix. Emails are additionally scoped to one saved search by the
enrollment id in their unsubscribe link.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
import urllib.parse
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message

IMAP_HOST = "imap.gmail.com"
# All Mail rather than INBOX so the watcher keeps working if the alerts are
# filtered, archived, or land under a Gmail category tab.
MAILBOX = '"[Gmail]/All Mail"'

TARGET_RE = re.compile(r"target=([^\s\"'<>]+)")
ENROLLMENT_RE = re.compile(r"encodedEnrollmentId=([A-Za-z0-9_\-]+)")
ZPID_PATH_RE = re.compile(r"/(?:zpid_target/)?(\d{6,12})_zpid\b")
BUILDING_PATH_RE = re.compile(r"/apartments/[^/]+/[^/]+/([A-Za-z0-9]+)/?")

# Exactly these — anything with a `-_rid-` suffix is a recommendation or ad.
RESULT_UTM_CONTENT = {"forrentimage", "forrentaddress"}


@dataclass
class AlertEmail:
    message_id: str
    subject: str
    received: datetime
    body: str


@dataclass
class SourceLink:
    """A listing target pulled out of an alert email."""

    url: str
    # "home" for a single unit (has a zpid), "building" for a complex page that
    # fans out into many units when enriched.
    kind: str
    ident: str

    @property
    def is_building(self) -> bool:
        return self.kind == "building"


def _decode_body(msg: Message) -> str:
    html, text = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
            if part.get_content_type() == "text/html":
                html += body
            elif part.get_content_type() == "text/plain":
                text += body
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            html = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    # Both forms carry the same tracking links; HTML is preferred only because
    # plain-text alternatives are sometimes truncated.
    return html or text


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fetch_alert_emails(
    address: str, app_password: str, lookback_hours: int = 48, limit: int = 50
) -> list[AlertEmail]:
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    # IMAP SINCE is day-granular; the real cutoff is applied below.
    since_token = (since - timedelta(days=1)).strftime("%d-%b-%Y")

    out: list[AlertEmail] = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(address, app_password)
        conn.select(MAILBOX, readonly=True)
        status, data = conn.search(None, f'(SINCE {since_token} FROM "zillow")')
        if status != "OK" or not data or not data[0]:
            return []

        for msg_id in data[0].split()[-limit:]:
            status, raw = conn.fetch(msg_id, "(RFC822)")
            if status != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            received = _parse_date(msg.get("Date"))
            if received and received < since:
                continue
            out.append(
                AlertEmail(
                    message_id=msg.get("Message-ID", msg_id.decode()),
                    subject=msg.get("Subject", ""),
                    received=received or datetime.now(timezone.utc),
                    body=_decode_body(msg),
                )
            )
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def _decode_target(raw: str) -> str:
    """Unwrap a click-tracker `target=` value into the real Zillow URL.

    Handles Proofpoint/urldefense rewriting (which substitutes `*` for `%`)
    so that forwarded copies parse identically to the originals.
    """
    value = raw.split("__;")[0]
    if "*" in value:
        value = value.replace("*", "%")
    return urllib.parse.unquote(value)


def _utm_content(url: str) -> str:
    query = urllib.parse.urlparse(url).query
    values = urllib.parse.parse_qs(query).get("utm_content", [])
    return values[0] if values else ""


def enrollment_id(body: str) -> str | None:
    """The saved search this email belongs to, from its unsubscribe link.

    Scoping by enrollment id rather than by the search's display name means
    renaming the search in Zillow does not silently break the watcher.
    """
    for raw in TARGET_RE.findall(body):
        match = ENROLLMENT_RE.search(_decode_target(raw))
        if match:
            return match.group(1)
    return None


def extract_source_links(body: str) -> list[SourceLink]:
    """Genuine saved-search results only, in order, deduplicated."""
    seen: set[str] = set()
    out: list[SourceLink] = []

    for raw in TARGET_RE.findall(body):
        url = _decode_target(raw)
        if "zillow.com" not in url:
            continue
        if _utm_content(url) not in RESULT_UTM_CONTENT:
            continue  # recommendation, ad, or chrome (logo, footer, view-all)

        zpid = ZPID_PATH_RE.search(url)
        if zpid:
            ident, kind = zpid.group(1), "home"
            canonical = f"https://www.zillow.com/homedetails/{ident}_zpid/"
        else:
            building = BUILDING_PATH_RE.search(url)
            if not building:
                continue
            ident, kind = building.group(1), "building"
            # Strip tracking params; the bare path is the stable building page.
            canonical = urllib.parse.urljoin(url, urllib.parse.urlparse(url).path)

        if ident in seen:
            continue
        seen.add(ident)
        out.append(SourceLink(url=canonical, kind=kind, ident=ident))

    return out


def source_links_from_email(
    alert: AlertEmail, expected_enrollments: Collection[str] | str | None = None
) -> list[SourceLink]:
    """Results from one alert, dropped entirely if it belongs to another search.

    Takes a collection because several saved searches can legitimately feed one
    watcher — Zillow's square-footage filter snaps to fixed anchors, so covering
    a real range means splitting it across searches (one capped at 1,000 sqft,
    another for everything above). Each has its own enrollment id.
    """
    if expected_enrollments:
        # A bare string would make the membership test below match substrings,
        # which would quietly accept alerts from unrelated searches.
        if isinstance(expected_enrollments, str):
            expected_enrollments = [expected_enrollments]
        found = enrollment_id(alert.body)
        # An email with no enrollment id at all is not a saved-search alert
        # (price drops, marketing) and is skipped rather than guessed at.
        if found not in set(expected_enrollments):
            return []
    return extract_source_links(alert.body)
