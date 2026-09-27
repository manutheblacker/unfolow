# unfolow

Unfollow every account you follow, using Instagram's private *app* API.

Dry run by default. Nothing is unfollowed until you pass `--execute`.

> The filename is `unfolow.py` (one `l` short of `unfollow`). It is the name the
> script has always had here; rename it if you like, just update the commands
> below.

## What it does

Two endpoints, both from the private app API:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `friendships/<your_id>/following/` | page through accounts you follow |
| `POST` | `friendships/destroy/<user_id>/` | unfollow one account |

There is **no bulk unfollow endpoint**. Every unfollow is one HTTP request, so
runtime is `accounts × delay` and cannot be shortened past that floor without
hammering the API.

## Requirements

- Python **3.10+** (developed and tested on 3.12)
- `instagrapi==3.0.14`
- `python-dotenv` (optional; only for reading `.env`)

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env          # contains your password
```

Then edit `.env`:

```ini
IG_USERNAME="your_login"
IG_PASSWORD="your_password"
```

## Usage

```bash
.venv/bin/python unfolow.py                  # dry run: prints the full list
.venv/bin/python unfolow.py --execute        # actually unfollows
.venv/bin/python unfolow.py --help           # full flag list
```

### Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--execute` | off | Actually unfollow. Without it, the run is a dry run. |
| `--delay N` | `3.0` | Seconds between unfollows, jittered 0.5×–1.5×. Sole pacing knob. |
| `--relogin` | off | Discard the cached session and authenticate fresh. |
| `--yes` | off | Skip the interactive `yes` confirmation. |

## Credentials

Resolved in this order, **first match wins**:

1. Exported variables — `IG_USERNAME` / `IG_PASSWORD`
2. `.env` beside the script
3. A hidden interactive prompt

So `IG_PASSWORD=hunter2 .venv/bin/python unfolow.py` overrides `.env` without
editing it. `.env` is gitignored; never commit real values.

`python-dotenv` is imported defensively. If it is missing, the script still
works — you just fall back to exported variables or the prompt.

> The loader passes an **explicit** `dotenv_path`. A bare `load_dotenv()` walks up
> the directory tree, and this project sits inside a parent directory that has
> its own `.env`; without the explicit path it would load that one instead and
> silently resolve nothing.

## Two-factor authentication

The script never asks for a code up front. It attempts the login, and only when
Instagram replies `TwoFactorRequired` does it prompt — then retries **exactly
once**. A rejected code is not retried, because repeatedly submitting bad codes
locks accounts.

For a non-interactive run, export a code that is seconds old:

```bash
export IG_VERIFICATION_CODE=123456
.venv/bin/python unfolow.py
```

TOTP codes die after 30 seconds, so `IG_VERIFICATION_CODE` is deliberately **not**
read from `.env` — a stale saved code would just break unattended runs.

The session is cached afterwards, so this is a one-time step.

## Safety rails

- **Dry run by default.** `--execute` is required to change anything.
- **The whole followee list is fetched before the first unfollow.** Unfollowing
  while paging would shift the list under the cursor and silently skip accounts.
- **Resumable.** Every outcome is appended to `.unfollow_log.jsonl`. Re-running
  skips whatever already succeeded, so a crash or a hard stop costs you nothing.
- **Sessions are cached** in `.ig_settings.json` and reused. Excessive logins are
  the fastest way to get an account flagged, so avoid `--relogin` reflexively.
- **You are excluded from your own target list.**

## Pacing

`--delay` is the entire cost, because instagrapi's own per-request delay is
disabled after login:

| Component | Per account |
| --- | --- |
| Network round trip | ~0.2s |
| `--delay` × jitter (mean 1.0) | as configured |
| instagrapi internal delay | disabled (`delay_range = None`) |

At the `3.0` default, roughly **3.2s per account** — about 27 minutes for 500.

| `--delay` | Per account | 500 accounts |
| --- | --- | --- |
| `1` | ~1.2s | ~10 min |
| `3` (default) | ~3.2s | ~27 min |
| `5` | ~5.2s | ~43 min |

Throttling backs off automatically (exponential, capped at 300s), so a faster
setting degrades rather than breaks. But sustained 429s are themselves a signal.
If you want it quicker than the default, `--delay 1` is the reasonable step;
going below that buys very little and invites a challenge.

**Concurrency is deliberately not implemented.** `Client` mutates shared state
(`last_json`, `last_response`, `_users_following`) on every request, so parallel
unfollows would race — and a burst of parallel requests is precisely the pattern
that triggers a checkpoint.

## When things go wrong

| Message | What to do |
| --- | --- |
| `ChallengeRequired` / `FeedbackRequired` | Open the Instagram app, clear the challenge, then re-run with `--relogin`. |
| `SentryBlock` | Hard rate limit. **Stop for several hours**, then re-run with a higher `--delay`. |
| `PleaseWaitFewMinutes` / throttled | Handled automatically with backoff. No action needed. |
| `LoginRequired` | Session expired. Re-run with `--relogin`. |
| `TwoFactorRequired` after submitting a code | Code was stale. Export a fresh one. |
| `Missing credentials` | No `.env`, no exported vars, no TTY to prompt on. |

A hard stop exits cleanly and the log means the next run resumes exactly where
it left off. Ctrl-C is also safe.

## Files

| File | Purpose | Gitignored |
| --- | --- | --- |
| `.env` | Your credentials, mode `600` | yes |
| `.ig_settings.json` | Cached authenticated session, mode `600` | yes |
| `.unfollow_log.jsonl` | Per-account resume log, mode `600` | yes |
| `.env.example` | Template to copy | no |

To start completely fresh, delete `.ig_settings.json` (forces a fresh login) or
`.unfollow_log.jsonl` (forgets what was already unfollowed).

## A note on the approach

This automates a private, undocumented API. Instagram's Terms of Use prohibit
automated access, and bulk unfollowing at speed can get an account
checkpointed, rate-limited, or suspended. Use it on an account whose worst-case
loss is small, keep the default delay, and prefer unfollowing from the app itself
if you only have a few hundred accounts to clear.
