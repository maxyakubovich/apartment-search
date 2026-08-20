# SF Apartment Watcher

Watches Zillow saved-search alerts for San Francisco rentals, works out whether each
listing has a **private room that closes with a door**, and pushes the good ones to
Telegram within a couple of minutes.

Zillow's own alerts can't express the requirement: the square-footage filter snaps to
750/1,000 anchors, and there's no filter at all for "has a room Taryn can run therapy
sessions in." Bedroom count isn't the criterion either — a 2BR works, a 1BR+den works, a
big open-plan 1BR doesn't.

## How it works

```
Zillow "Instant" alert → Gmail
      │  IMAP poll every 2 min                        [free]
      ▼
  resolve tracking links → zpids  ──► already seen? ──► skip
      │ new only
      ▼
  Apify detail scraper                                [~$0.0017 each]
      │  description, sqft, beds, photos, floor plans
      ▼
  hard gates: price, beds, sqft, boundary             [free]
      │ survivors only
      ▼
  Claude: text pass → vision pass on floor plans      [~1-2 calls]
      │  den_conf, has_door, is_passthrough
      ▼
  notify ladder → Telegram
```

Money is spent as late as possible. Deduplication and the numeric gates run first, so a
listing that's over budget or too small never costs a scraper call or a token.

### Why a self-looping job instead of cron

GitHub's `schedule:` trigger is best-effort — a `*/5` cron commonly delivers 2–4 runs an
hour with 20–60 minute delays. So `watch.yml` polls in an internal loop for just under
the 6-hour job ceiling, then re-dispatches itself. `watchdog.yml` runs on a half-hourly
cron purely to restart the chain if a link breaks; cron lateness is harmless there.

This needs unlimited Actions minutes, which means **the repo must be public**. A private
repo would exhaust its 2,000 free minutes in about 33 hours.

## Matching logic

Den confidence sets the square-footage bar rather than acting as a gate of its own. A
confirmed den earns a lower bar; a listing with no den signal has to be big enough to
wall off a desk on its own.

```
DROP  if price > 6500 · beds ∉ {1,2} · sqft < 850

NOTIFY if beds == 2       and sqft >= 900     second bedroom is the office
NOTIFY if den_conf >= 0.6 and sqft >= 850     confident den
NOTIFY if den_conf >= 0.3 and sqft >= 900     plausible den
NOTIFY if den_conf <  0.3 and sqft >= 1000    no den, but room to sequester a desk
```

**Unlisted square footage** is common on SF landlord-posted rentals. It's treated as
unknown rather than zero: notify only on a confident den, and tag the message.

Every threshold lives in [`src/config.yaml`](src/config.yaml). Expect to tune after a few
days of real traffic.

## Setup

### 1. Zillow — Instant alerts, and widen the saved search

My Zillow → Saved Searches → Edit → frequency **Instant**. Without this there's no
trigger and nothing else matters.

**Then remove the square-footage cap.** The saved search `SF 750-1k sqft` is set to
`sqft: 750–1,000`, which silently excludes the best candidates — a 1,100 sqft 2BR at
$6,000 is exactly what you want and Zillow will never email it. Set the max to **No Max**.
The pipeline does the sqft filtering itself, and does it better: it treats an unpublished
square footage as unknown rather than excluding it, which matters because a good share of
SF landlord listings publish no sqft at all.

Same logic for the `$3,600` minimum rent — there's no reason to exclude a cheap large
unit. Let Zillow be broad and let the ladder be strict.

Only alerts belonging to this one saved search are processed, matched on the enrollment
id in `search.saved_search_enrollment_id`. That id comes from `savedSearchEnrollmentId`
in the search URL and is cross-checked against each email's unsubscribe link, so renaming
the search in Zillow won't break anything.

### 2. Gmail app password

Requires 2FA. https://myaccount.google.com/apppasswords → generate one for "Mail".
This is for the account that *receives* the Zillow alerts.

### 3. Telegram bot

Message [@BotFather](https://t.me/botfather) → `/newbot` → copy the token. Then:

```bash
python3 scripts/telegram_setup.py <YOUR_BOT_TOKEN>
```

That validates the token, clears any webhook (which otherwise makes `getUpdates` return
an empty list forever), long-polls for 60 seconds while you message the bot, prints the
chat id, and sends a confirmation message. Raw `curl .../getUpdates | grep` prints nothing
for about five different reasons and doesn't tell you which.

### 4. Tokens

- Apify: https://console.apify.com/account/integrations (free tier is enough)
- Anthropic: https://console.anthropic.com/settings/keys
- `DISPATCH_PAT`: a fine-grained PAT scoped to this repo with **Actions: read and write**.
  Required because the built-in `GITHUB_TOKEN` is not permitted to trigger another
  workflow run.

### 5. Repository secrets

Settings → Secrets and variables → Actions:

`GMAIL_ADDRESS` `GMAIL_APP_PASSWORD` `TELEGRAM_BOT_TOKEN` `TELEGRAM_CHAT_ID`
`APIFY_TOKEN` `ANTHROPIC_API_KEY` `DISPATCH_PAT`

### 6. Start it

Actions → **watch** → Run workflow. It re-dispatches itself from then on.

## Local use

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt pytest
cp .env.example .env    # fill it in; .env is gitignored
```

```bash
./.venv/bin/python -m src.main --test-telegram
```

```bash
./.venv/bin/python -m src.main --backfill 20
```

`--backfill` replays your recent Zillow alerts and prints the full decision trace for
each listing without sending anything. This is the accuracy check — open a few of the
listings by hand and confirm the den calls are right before trusting it.

```bash
./.venv/bin/python -m src.main --once --dry-run   # one cycle, decide but send nothing
./.venv/bin/python -m src.main --once             # one live cycle
./.venv/bin/python -m pytest tests/ -q
```

## Tuning

Start by watching `--backfill` output for a day or two.

- Too much noise → raise `min_sqft` on the `room_to_sequester` rung, or raise the
  `plausible_den` confidence bar.
- Missing good places → lower `min_sqft_floor`, or widen `den.escalate_band` so more
  listings get the vision pass.
- Wrong den calls specifically → the prompt in [`src/den.py`](src/den.py) is the thing to
  edit, not the thresholds. Switch `models.vision` to `claude-opus-5` if the reasoning
  is close but not quite there.

To restrict by map boundary, paste the polygon from your saved-search URL's
`searchQueryState` into `search.polygon` as `[[lng, lat], ...]`.

## Operational notes

- **State** lives in `state/seen.json`, committed back by the job. It's what stops
  re-notification. Only zpids, prices, and verdicts — but it is public. To make it
  private, swap `_read`/`_write` in [`src/state.py`](src/state.py) for a private Gist;
  nothing else changes.
- **Price drops** on already-sent listings re-notify at a 5% threshold.
- **Parser breakage** is the likeliest failure. If alerts arrive but no links parse,
  you get an explicit Telegram warning rather than silence — the failure mode otherwise
  looks identical to a quiet market.
- **Enrichment failures** leave zpids unrecorded so the next cycle retries, rather than
  marking them handled and losing them.

## A caveat worth knowing

Zillow's Terms of Use prohibit automated access to the site. Reading your own email is
clean; fetching listing details through a third-party scraper is the grey part. If you'd
rather not, set `search.polygon` and the thresholds as you like but replace the Apify
call with a Telegram message containing just the link, and do the den assessment by eye.
