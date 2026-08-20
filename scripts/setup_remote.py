#!/usr/bin/env python3
"""Point this repo at your GitHub repo and push.

    python3 scripts/setup_remote.py

Prompts for your GitHub username rather than making you edit a command with a
placeholder in it, checks the repository actually exists and is public before
pushing, and sets the upstream so later pushes are a bare `git push`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "apartment-search"


def git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def lookup(user: str) -> dict | None:
    url = f"https://api.github.com/repos/{user}/{REPO}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"_status": exc.code}
    except Exception:
        return None


def main() -> int:
    current = git("remote", "get-url", "origin")[1]
    if current and "YOUR_USERNAME" not in current:
        print(f"origin is already {current}")
    else:
        print("origin is unset or still has the docs placeholder in it.\n")

    try:
        user = input("Your GitHub username: ").strip().strip("<>\"'/ ")
    except EOFError:
        return 1
    if not user:
        print("No username given.")
        return 1

    info = lookup(user)
    if info is None:
        print("! Could not reach github.com to verify. Continuing anyway.")
    elif info.get("_status") == 404:
        print(f"\n✗ https://github.com/{user}/{REPO} does not exist (or is private).")
        print("  Create it at https://github.com/new — name it exactly")
        print(f"  '{REPO}', set it to Public, and add no README or .gitignore.")
        return 1
    elif info.get("private"):
        # A private repo would burn its 2,000 free Actions minutes in ~33h,
        # which breaks the two-minute polling the whole design depends on.
        print(f"\n! {user}/{REPO} exists but is PRIVATE.")
        print("  Actions minutes are capped on private repos; the watch loop")
        print("  needs a public repo. Change it in Settings → General → Danger Zone.")

    url = f"https://github.com/{user}/{REPO}.git"
    code, out = git("remote", "set-url", "origin", url)
    if code != 0:
        git("remote", "add", "origin", url)
    print(f"\n✓ origin → {url}")

    print("\nPushing (you may be asked to authenticate)...\n")
    code, out = git("push", "-u", "origin", "main")
    print(out)
    if code != 0:
        print("\n✗ Push failed. If it asked for a password: GitHub needs a")
        print("  Personal Access Token, not your account password. Easiest fix is")
        print("  the GitHub CLI — `brew install gh && gh auth login` — then re-run.")
        return 1

    print(f"\n✓ Pushed. Now open:")
    print(f"  https://github.com/{user}/{REPO}/actions/workflows/backfill.yml")
    print("  → Run workflow → leave count at 20 → Run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
