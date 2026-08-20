#!/usr/bin/env python3
"""Check every credential independently before a real run.

    python3 scripts/preflight.py

Four services have to work for a cycle to complete, and a failure in any of
them surfaces as the same unhelpful stack trace partway through. This tests
each one in isolation and reports which are good, so a broken key is a
five-second answer rather than a debugging session.

Reads from .env, or the environment if that wins.
"""

from __future__ import annotations

import imaplib
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import load_config, load_dotenv, optional  # noqa: E402

OK, BAD, WARN = "✓", "✗", "!"


def _get(url: str, timeout: int = 30) -> tuple[int, dict | str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except Exception:
            return exc.code, ""
    except Exception as exc:
        return 0, str(exc)


def check_gmail() -> bool:
    address, password = optional("GMAIL_ADDRESS"), optional("GMAIL_APP_PASSWORD")
    if not address or not password:
        print(f"{BAD} Gmail — GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set in .env")
        return False
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(address, password.replace(" ", ""))
        conn.select('"[Gmail]/All Mail"', readonly=True)
        status, data = conn.search(None, '(FROM "zillow")')
        count = len(data[0].split()) if status == "OK" and data and data[0] else 0
        conn.logout()
        print(f"{OK} Gmail — logged in as {address}, {count} Zillow email(s) in All Mail")
        if count == 0:
            print(f"  {WARN} No Zillow mail found. Is this the account the alerts go to?")
        return True
    except imaplib.IMAP4.error as exc:
        print(f"{BAD} Gmail — login rejected: {exc}")
        print("  Use a 16-character App Password, not your account password.")
        print("  https://myaccount.google.com/apppasswords")
        return False
    except Exception as exc:
        print(f"{BAD} Gmail — {exc}")
        return False


def check_apify() -> bool:
    token = optional("APIFY_TOKEN")
    if not token:
        print(f"{BAD} Apify — APIFY_TOKEN not set in .env")
        return False
    status, body = _get(f"https://api.apify.com/v2/users/me?token={token}")
    if status != 200 or not isinstance(body, dict):
        print(f"{BAD} Apify — token rejected (HTTP {status})")
        return False
    data = body.get("data", {})
    print(f"{OK} Apify — authenticated as {data.get('username', 'unknown')}")
    return True


def check_anthropic() -> bool:
    key = optional("ANTHROPIC_API_KEY")
    if not key:
        print(f"{BAD} Anthropic — ANTHROPIC_API_KEY not set in .env")
        return False
    model = load_config()["models"]["text"]
    try:
        import anthropic
    except ImportError:
        print(f"{WARN} Anthropic — key present, but the SDK isn't installed here.")
        print("  ./.venv/bin/python scripts/preflight.py  (or pip install anthropic)")
        return False
    try:
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model=model, max_tokens=4, messages=[{"role": "user", "content": "hi"}]
        )
        print(f"{OK} Anthropic — key works, model {model} reachable")
        return True
    except Exception as exc:
        print(f"{BAD} Anthropic — {type(exc).__name__}: {str(exc)[:160]}")
        return False


def check_telegram() -> bool:
    token, chat = optional("TELEGRAM_BOT_TOKEN"), optional("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(f"{BAD} Telegram — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        return False
    status, body = _get(
        "https://api.telegram.org/bot{}/sendMessage?{}".format(
            token,
            urllib.parse.urlencode(
                {"chat_id": chat, "text": "✓ Preflight check — all wired up."}
            ),
        )
    )
    if status != 200 or not (isinstance(body, dict) and body.get("ok")):
        detail = body.get("description") if isinstance(body, dict) else body
        print(f"{BAD} Telegram — {detail}")
        return False
    print(f"{OK} Telegram — test message delivered to chat {chat}")
    return True


def main() -> int:
    load_dotenv()
    print("Checking credentials...\n")
    results = [
        check_gmail(),
        check_apify(),
        check_anthropic(),
        check_telegram(),
    ]
    print()
    if all(results):
        print("All four good. Next:  ./.venv/bin/python -m src.main --backfill 20")
        return 0
    print(f"{sum(results)}/4 working — fix the ✗ lines above, then re-run.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
