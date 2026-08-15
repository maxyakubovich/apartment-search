"""Zillow saved-search alerts, read over IMAP.

This is the trigger for the whole pipeline. Zillow's "Instant" alerts land in
Max's personal Gmail; we poll for them and pull the listing links out.

The awkward part is that Zillow wraps every link in click-tracking redirects,
so the zpid is not in the href. We resolve those by walking the redirect chain
one hop at a time and stopping the moment a `_zpid` appears — which means we
learn the listing identity without ever fetching the listing page itself, and
so never touch Zillow's bot protection here.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message

import requests

IMAP_HOST = "imap.gmail.com"
# All Mail rather than INBOX so the watcher keeps working if you later add a
# filter that archives or labels the Zillow alerts.
MAILBOX = '"[Gmail]/All Mail"'

# Matches the canonical detail URL and the bare id form Zillow uses in some
# alert templates. Deliberately permissive — a false positive costs one cheap
# lookup, a false negative costs an apartment.
ZPID_RE = re.compile(r"(\d{6,12})_zpid", re.I)
ZILLOW_LINK_RE = re.compile(
    r"https?://[^\s\"'<>]*?(?:zillow\.com|zillowstatic\.com)[^\s\"'<>]*", re.I
)
# Building/complex pages have no zpid; they are identified by a /b/<slug>-<id> path.
BUILDING_RE = re.compile(r"zillow\.com/b/([a-z0-9\-]+)/?", re.I)

MAX_REDIRECT_HOPS = 8
REQUEST_TIMEOUT = 15


@dataclass
class AlertEmail:
    message_id: str
    subject: str
    received: datetime
    html: str


def _decode_body(msg: Message) -> str:
    """Prefer the HTML part; fall back to plain text."""
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
            charset = msg.get_content_charset() or "utf-8"
            html = payload.decode(charset, errors="replace")
    return html or text


def fetch_alert_emails(
    address: str, app_password: str, lookback_hours: int = 48, limit: int = 50
) -> list[AlertEmail]:
    """Pull recent Zillow alert emails, newest last."""
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    # IMAP SINCE has day granularity only; we re-filter by real timestamp below.
    since_token = (since - timedelta(days=1)).strftime("%d-%b-%Y")

    out: list[AlertEmail] = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        conn.login(address, app_password)
        conn.select(MAILBOX, readonly=True)
        status, data = conn.search(None, f'(SINCE {since_token} FROM "zillow")')
        if status != "OK" or not data or not data[0]:
            return []

        ids = data[0].split()[-limit:]
        for msg_id in ids:
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
                    html=_decode_body(msg),
                )
            )
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def extract_candidate_links(html: str) -> list[str]:
    """Every Zillow-ish URL in the email, deduped, image assets removed."""
    seen: set[str] = set()
    out: list[str] = []
    for url in ZILLOW_LINK_RE.findall(html):
        url = url.rstrip(").,;'\"")
        if "zillowstatic.com" in url.lower():
            continue  # image CDN, never a listing
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def resolve_zpid(url: str, session: requests.Session | None = None) -> str | None:
    """Walk the click-tracking chain until a zpid appears.

    Redirects are followed manually so we can stop the instant the identity is
    known. Following automatically would fetch the final Zillow page body and
    run straight into bot protection for no benefit.
    """
    match = ZPID_RE.search(url)
    if match:
        return match.group(1)

    session = session or requests.Session()
    current = url
    for _ in range(MAX_REDIRECT_HOPS):
        try:
            resp = session.get(
                current,
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (compatible; apartment-watcher/1.0)"},
            )
        except requests.RequestException:
            return None

        location = resp.headers.get("Location")
        if not location:
            return None

        match = ZPID_RE.search(location)
        if match:
            return match.group(1)

        current = requests.compat.urljoin(current, location)

    return None


def canonical_url(zpid: str) -> str:
    return f"https://www.zillow.com/homedetails/{zpid}_zpid/"


def listing_ids_from_email(alert: AlertEmail) -> list[str]:
    """zpids referenced by a single alert email, in order of appearance."""
    session = requests.Session()
    found: list[str] = []
    for link in extract_candidate_links(alert.html):
        zpid = resolve_zpid(link, session)
        if zpid and zpid not in found:
            found.append(zpid)
    return found
