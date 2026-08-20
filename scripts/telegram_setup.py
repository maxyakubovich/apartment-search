#!/usr/bin/env python3
"""Find your Telegram chat id, and say why if it can't.

    python3 scripts/telegram_setup.py <BOT_TOKEN>

`curl .../getUpdates | grep chat` prints nothing for at least five different
reasons, and they need different fixes. This checks each one in order and says
which it is. Uses only the standard library so it runs without the venv.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"


def call(token: str, method: str, **params) -> dict:
    url = API.format(token=token, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


EXAMPLE_TOKEN = "8123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def clean(raw: str) -> str:
    """Salvage a pasted token.

    Tokens get pasted with the documentation's angle brackets still attached,
    with quotes around them, or with the `bot` URL prefix included. All three
    produce an identical 'Unauthorized' and none are worth a round trip.
    """
    token = raw.strip().strip("<>\"'").strip()
    if token.lower().startswith("bot") and ":" in token[3:]:
        token = token[3:]
    return token


def read_token() -> str:
    if len(sys.argv) > 1:
        return clean(sys.argv[1])
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return clean(os.environ["TELEGRAM_BOT_TOKEN"])
    # Prompting sidesteps shell quoting and paste-mangling entirely, which is
    # the most common way this step goes wrong.
    try:
        return clean(input("Paste your bot token from @BotFather: "))
    except EOFError:
        return ""


def main() -> int:
    token = read_token()
    if not token:
        print("No token given. Run: python3 scripts/telegram_setup.py")
        return 1
    if token == EXAMPLE_TOKEN:
        print("✗ That's the example token from the docs, not yours.")
        print("  Open Telegram → @BotFather → /mybots → your bot → API Token.")
        return 1

    # 1. Is the token even valid? A typo here looks identical to "no messages".
    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"✗ Token rejected: {me.get('description')}")
        print("  Re-copy it from @BotFather — it looks like 8123456789:AAH...")
        print("  Common causes: a stray space, or the word 'bot' pasted in front.")
        return 1
    bot = me["result"]
    print(f"✓ Token valid — bot is @{bot['username']} ({bot.get('first_name','')})")

    # 2. A webhook silently makes getUpdates return an empty list forever.
    hook = call(token, "getWebhookInfo")
    hook_url = hook.get("result", {}).get("url") if hook.get("ok") else None
    if hook_url:
        print(f"\n✗ A webhook is set ({hook_url}).")
        print("  While one exists, getUpdates always returns nothing. Clearing it...")
        cleared = call(token, "deleteWebhook")
        print("  ✓ Webhook cleared." if cleared.get("ok") else f"  ✗ {cleared.get('description')}")
    else:
        print("✓ No webhook set (getUpdates is usable)")

    # 3. Long-poll so the message can be sent *after* the script starts. Plain
    #    getUpdates only returns what already arrived, which is the usual
    #    reason a first attempt comes back empty.
    print(f"\n→ Now open Telegram, find @{bot['username']}, and send it any message.")
    print("  Waiting up to 60 seconds...\n")

    updates = call(token, "getUpdates", timeout=60, limit=100)
    if not updates.get("ok"):
        print(f"✗ getUpdates failed: {updates.get('description')}")
        return 1

    chats: dict[int, str] = {}
    for update in updates.get("result", []):
        msg = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or {}
        )
        chat = msg.get("chat")
        if chat:
            label = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            ) or chat.get("username", "")
            chats[chat["id"]] = f"{label} ({chat.get('type')})"

    if not chats:
        print("✗ Still no messages.\n")
        print("  Checklist:")
        print("   1. You must message the BOT directly, not a group or Saved Messages.")
        print(f"      Open: https://t.me/{bot['username']}")
        print("   2. Press the blue START button, then send any text.")
        print("   3. If you already ran getUpdates once and it consumed the update,")
        print("      just send the bot another message and re-run this script.")
        print("   4. Updates older than 24 hours are discarded by Telegram.")
        return 1

    print("✓ Found chat id(s):\n")
    for chat_id, label in chats.items():
        print(f"    TELEGRAM_CHAT_ID = {chat_id}      # {label}")

    if len(chats) == 1:
        chat_id = next(iter(chats))
        sent = call(token, "sendMessage", chat_id=chat_id, text="🏠 Apartment watcher connected.")
        print("\n✓ Test message sent — check Telegram." if sent.get("ok")
              else f"\n✗ Send failed: {sent.get('description')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
