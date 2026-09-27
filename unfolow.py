#!/usr/bin/env python3
"""
Unfollow every account you follow, using the private *app* API via instagrapi.

Endpoints used:
    GET  friendships/<user_id>/following/   -> page through your followees
    POST friendships/destroy/<user_id>/     -> unfollow one account

Why instagrapi and not instagram_private_api:
    That library has been unmaintained since 2019 and can no longer log in.
    Instagram now requires a client-generated csrf token, RSA+AES-GCM encrypted
    passwords, and has retired the legacy login path it speaks. instagrapi is
    maintained and tracks those changes.

Safety rails:
  * Dry run by default. Pass --execute to actually unfollow.
  * Session is cached in .ig_settings.json, and instagrapi reuses a valid
    session instead of re-authenticating (excessive logins are the fastest way
    to get an account flagged).
  * Resumable: progress is appended to .unfollow_log.jsonl, so a crash or
    throttle mid-run does not lose track of who was already unfollowed.
  * Randomised delay between unfollows, exponential backoff on throttle.
  * No double-pacing: instagrapi's internal per-request delay is disabled after
    login, so --delay is the sole pacing knob. This is intentional to avoid
    invisible overhead. (accounts * delay ≈ runtime)

Usage:
    pip install instagrapi python-dotenv
    cp .env.example .env      # then fill in IG_USERNAME / IG_PASSWORD
    python3 unfolow.py                # dry run, prints the list
    python3 unfolow.py --execute      # actually unfollows

Credentials:
    Resolved in this order, first match wins: an exported IG_USERNAME/IG_PASSWORD,
    then .env beside this script, then a hidden interactive prompt. .env is
    gitignored and should be mode 600; copy .env.example rather than committing
    real values. IG_VERIFICATION_CODE is deliberately *not* read from .env,
    because a TOTP code is dead within 30 seconds and a stale one in a file
    would break non-interactive runs for no benefit.

Two-factor:
    The script only asks for a verification code once Instagram actually demands
    one, and retries the login exactly once with it. A rejected code is not
    retried, because hammering bad codes locks accounts. For a non-interactive
    run, set IG_VERIFICATION_CODE in the environment to a fresh code from your
    authenticator app; otherwise it prompts. Either way the session is cached
    afterwards, so this is a one-time step.
"""

import argparse
import getpass
import json
import os
import random
import sys
import time
from pathlib import Path

from instagrapi import Client
try:
    from dotenv import load_dotenv
except ImportError:  # optional: exported vars + the prompt still work without it
    load_dotenv = None
from instagrapi.exceptions import (
    BadPassword,
    ChallengeRequired,
    ClientConnectionError,
    ClientError,
    ClientLoginRequired,
    ClientThrottledError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    PrivateAccount,
    SentryBlock,
    TwoFactorRequired,
    UserNotFound,
)

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / ".ig_settings.json"
LOG_PATH = BASE_DIR / ".unfollow_log.jsonl"
ENV_PATH = BASE_DIR / ".env"

# Failures that mean "stop and let a human look at the app", not "retry".
HARD_STOPS = (
    ChallengeRequired,
    FeedbackRequired,
    SentryBlock,
    TwoFactorRequired,
    ClientLoginRequired,
    LoginRequired,
)
# Failures worth retrying later in the same run.
SOFT_FAILURES = (PleaseWaitFewMinutes, ClientThrottledError)


# --------------------------------------------------------------------------- #
# credentials + client
# --------------------------------------------------------------------------- #
def load_env_file():
    """
    Pull IG_* out of .env, if python-dotenv is installed and the file exists.

    The path is explicit on purpose: a bare load_dotenv() walks *up* from this
    file looking for .env, and this project sits inside a parent directory that
    has its own .env full of unrelated AWS/SMTP secrets. Finding that one
    instead would silently resolve no credentials at all.

    override=False keeps precedence sane: a genuinely exported variable always
    beats the file, so `IG_PASSWORD=... python3 unfolow.py` does the obvious
    thing and .env stays a default rather than an override.
    """
    if load_dotenv is None or not ENV_PATH.exists():
        return False
    load_dotenv(dotenv_path=ENV_PATH, override=False)
    return True


def resolve_credentials():
    """Read credentials from the environment, falling back to a hidden prompt."""
    load_env_file()
    username = os.environ.get("IG_USERNAME")
    password = os.environ.get("IG_PASSWORD")

    if username and password:
        return username, password

    if sys.stdin.isatty():
        username = username or input("Instagram username: ").strip()
        password = password or getpass.getpass("Instagram password: ")
        if username and password:
            return username, password

    raise SystemExit(
        "Missing credentials. Put them in .env next to this script:\n"
        "  IG_USERNAME=your_login\n"
        "  IG_PASSWORD=your_password\n"
        "or export them:\n"
        "  export IG_USERNAME=your_login IG_PASSWORD=your_password"
    )


def resolve_verification_code():
    """
    Get a 2FA code, but only once Instagram has actually asked for one.

    Called from login_with_2fa() after a TwoFactorRequired, never up front:
    asking before the first login attempt would prompt for a code that does not
    exist yet, on every run, including accounts with no 2FA at all.
    """
    code = os.environ.get("IG_VERIFICATION_CODE", "").strip()
    if code:
        return code

    if not sys.stdin.isatty():
        raise SystemExit(
            "Instagram requires a verification code and there is no terminal to\n"
            "prompt on. Set it in the environment instead:\n"
            "  export IG_VERIFICATION_CODE=$(pass show instagram/otp)"
        )

    print(
        "  Instagram needs a verification code.\n"
        "  Enter the 6-digit code from your authenticator app (or a backup code).\n"
        "  Codes rotate every 30 seconds, so use a fresh one."
    )
    return input("  Verification code: ").strip()


def load_settings():
    """Return the cached session dict, or None."""
    if not SETTINGS_PATH.exists():
        return None
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"! Ignoring unreadable session cache: {exc}", file=sys.stderr)
        return None

    # An instagrapi session always carries these. A file left behind by the old
    # instagram_private_api script uses a different shape and would only produce
    # confusing auth errors, so drop it.
    if not isinstance(data, dict) or "cookies" not in data or "uuids" not in data:
        print("· Ignoring session cache from a different client library", file=sys.stderr)
        return None

    return data


def save_settings(client):
    """Persist the session so the next run reuses it instead of logging in."""
    data = json.loads(json.dumps(client.get_settings(), default=str))
    SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    SETTINGS_PATH.chmod(0o600)


def login_with_2fa(client, username, password, relogin=False):
    """
    Log in, asking for a verification code only if Instagram demands one.

    The first attempt sends no code. Only on TwoFactorRequired do we prompt (or
    read IG_VERIFICATION_CODE) and retry -- exactly once. A rejected code raises
    straight back out rather than looping, because retrying bad codes is how an
    account gets locked.
    """
    try:
        return client.login(username, password, relogin=relogin, verification_code="")
    except TwoFactorRequired:
        code = resolve_verification_code()
        if not code:
            raise SystemExit("No verification code entered.")
        print("→ Submitting verification code...")
        # No relogin= here: instagrapi guards against more than one relogin
        # attempt, and the session is mid-flow rather than stale.
        return client.login(username, password, verification_code=code)


def build_client(force_login=False):
    """
    Create an authenticated client.

    instagrapi's Client() does not authenticate; login() must be called
    explicitly. Passing cached settings lets it validate and reuse an existing
    session, so repeat runs do not re-authenticate.
    """
    username, password = resolve_credentials()

    if force_login and SETTINGS_PATH.exists():
        SETTINGS_PATH.unlink()
        print("· Cleared cached session")

    settings = None if force_login else load_settings()
    client = Client(settings=settings) if settings else Client()
    # Login fires several requests back to back (prelogin, login, Bloks 2FA),
    # so keep instagrapi's own jitter for that phase.
    client.delay_range = [1, 3]
    # login() short-circuits when a cached session is still good, and
    # re-authenticates with the supplied credentials when it is not.
    login_with_2fa(client, username, password, relogin=force_login)
    # Now hand pacing over to --delay alone. Leaving [1, 3] on would add a
    # hidden ~2s to every unfollow on top of the delay the user asked for.
    # None is instagrapi's own default and disables it in the private, graphql
    # and public request paths alike.
    client.delay_range = None

    save_settings(client)
    return client


# --------------------------------------------------------------------------- #
# fetch followees
# --------------------------------------------------------------------------- #
def fetch_following(client, user_id):
    """
    Page through the accounts the given user follows.

    The whole list is materialised before any unfollow happens on purpose:
    unfollowing while paging shifts the list underneath the cursor and silently
    skips accounts.
    """
    followees = client.user_following(user_id, use_cache=False, amount=0)
    return list(followees.values())


# --------------------------------------------------------------------------- #
# unfollow
# --------------------------------------------------------------------------- #
def already_unfollowed():
    """User ids successfully unfollowed in a previous run, for resume support."""
    done = set()
    if not LOG_PATH.exists():
        return done
    with LOG_PATH.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("ok") and record.get("user_id"):
                done.add(str(record["user_id"]))
    return done


def append_log(record):
    with LOG_PATH.open("a") as handle:
        handle.write(json.dumps(record) + "\n")
    LOG_PATH.chmod(0o600)


def unfollow_one(client, user):
    """
    Unfollow a single account.

    Returns True on success, False on a retryable failure, None when throttled
    (the caller backs off). Raises on anything that needs a human.
    """
    user_id = str(user.pk)
    username = user.username or user_id

    def record(ok, error):
        append_log({"ts": time.time(), "user_id": user_id, "username": username,
                    "ok": ok, "error": error})

    try:
        ok = client.user_unfollow(user_id)
    except HARD_STOPS:
        # Most of these are ClientError subclasses, so they must be re-raised
        # before the generic handler below or the run would plough on.
        raise
    except SOFT_FAILURES:
        record(False, "throttled")
        return None
    except (UserNotFound, PrivateAccount) as exc:
        # The account is gone or unreachable; retrying will not help.
        record(False, f"skipped: {type(exc).__name__}")
        return False
    except (ClientConnectionError, ClientError, OSError) as exc:
        record(False, f"{type(exc).__name__}: {exc}")
        return False

    if ok:
        record(True, None)
        return True
    record(False, "api returned False")
    return False


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def report_client_error(exc):
    """Turn a raw exception into something actionable."""
    if isinstance(exc, TwoFactorRequired):
        print(
            "  The verification code was rejected. Authenticator codes rotate\n"
            "  every 30s, so re-run with a fresh one:\n"
            "    export IG_VERIFICATION_CODE=<fresh code>",
            file=sys.stderr,
        )
    elif isinstance(exc, (ChallengeRequired, FeedbackRequired)):
        print(
            f"  Instagram is challenging this account: {type(exc).__name__}.\n"
            "  Open the Instagram app, clear the challenge, then re-run --relogin.",
            file=sys.stderr,
        )
    elif isinstance(exc, SentryBlock):
        print(
            "  Rate-limited hard (sentry block). Stop for several hours before\n"
            "  retrying, and raise --delay.",
            file=sys.stderr,
        )
    elif isinstance(exc, (LoginRequired, ClientLoginRequired)):
        print("  Session is no longer valid. Re-run with --relogin.", file=sys.stderr)
    elif isinstance(exc, BadPassword):
        print(f"  Instagram rejected the password: {exc}", file=sys.stderr)
    else:
        print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true",
                        help="actually unfollow (default is a dry run)")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="sole pacing knob: seconds between unfollows, jittered "
                             "0.5x-1.5x (default: 3). instagrapi's own per-request "
                             "delay is disabled so this is the whole cost.")
    parser.add_argument("--relogin", action="store_true",
                        help="discard the cached session and log in fresh")
    parser.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation prompt")
    return parser.parse_args()


def main():
    args = parse_args()

    print("→ Connecting to Instagram...")
    try:
        client = build_client(force_login=args.relogin)
    except HARD_STOPS as exc:
        print(f"! Cannot log in: {type(exc).__name__}", file=sys.stderr)
        report_client_error(exc)
        return 1
    except BadPassword as exc:
        print(f"! Login failed: {exc}", file=sys.stderr)
        return 1
    except (ClientError, ClientConnectionError, OSError) as exc:
        print(f"! Connection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    own_id = str(client.user_id)
    print(f"→ Logged in as @{client.username} (id {own_id})")

    print("→ Fetching accounts you follow...")
    try:
        followees = fetch_following(client, own_id)
    except HARD_STOPS as exc:
        print(f"! Stopped while reading your following list: {type(exc).__name__}",
              file=sys.stderr)
        report_client_error(exc)
        return 1
    except SOFT_FAILURES:
        print("! Rate-limited while reading your following list. Wait a few minutes "
              "and re-run.", file=sys.stderr)
        return 1
    except (ClientError, ClientConnectionError, OSError) as exc:
        print(f"! Could not read your following list: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    targets = [u for u in followees if str(u.pk) != own_id and u.pk]
    already = already_unfollowed() & {str(u.pk) for u in targets}
    targets = [u for u in targets if str(u.pk) not in already]

    print(f"\nFollowing:   {len(followees)}")
    if already:
        print(f"Already unfollowed (resumed): {len(already)}")
    print(f"Pending:     {len(targets)}")

    if not args.execute:
        print("\n--- DRY RUN, nothing was changed ---")
        for user in targets:
            flag = " [private]" if user.is_private else ""
            print(f"  would unfollow @{user.username}{flag}")
        print(f"\nRe-run with --execute to perform {len(targets)} unfollows.")
        return 0

    if not targets:
        print("\nNothing left to unfollow.")
        return 0

    if not args.yes and sys.stdin.isatty():
        answer = input(f"\nUnfollow {len(targets)} accounts? Type 'yes' to confirm: ").strip()
        if answer.lower() != "yes":
            print("Aborted.")
            return 0

    print(f"\nUnfollowing {len(targets)} accounts (~"
          f"{int(len(targets) * args.delay / 60)} min at {args.delay:.1f}s each)\n")

    throttle_streak = 0
    ok_count = fail_count = 0
    aborted = None

    for index, user in enumerate(targets, 1):
        try:
            outcome = unfollow_one(client, user)
        except HARD_STOPS as exc:
            # Session died or Instagram wants a human. Stop cleanly; the log
            # makes the next run pick up exactly where this left off.
            aborted = exc
            break

        if outcome is None:
            throttle_streak += 1
            backoff = min(300, 15 * (2 ** throttle_streak)) + random.uniform(0, 10)
            print(f"[{index}/{len(targets)}] throttled on @{user.username}, "
                  f"sleeping {backoff:.0f}s")
            time.sleep(backoff)
            continue

        throttle_streak = 0

        if outcome:
            ok_count += 1
        else:
            fail_count += 1
            print(f"[{index}/{len(targets)}] FAILED @{user.username}")

        if index % 10 == 0 or index == len(targets):
            print(f"[{index}/{len(targets)}] {ok_count} ok, {fail_count} failed")

        if index < len(targets):
            time.sleep(args.delay * random.uniform(0.5, 1.5))

    print(f"\nDone. {ok_count} unfollowed, {fail_count} failed.")

    if aborted is not None:
        print(f"! Stopped early: {type(aborted).__name__}", file=sys.stderr)
        report_client_error(aborted)

    print(f"Log: {LOG_PATH}")
    print("Run it again any time to pick up where it stopped.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run to resume.", file=sys.stderr)
        sys.exit(130)
