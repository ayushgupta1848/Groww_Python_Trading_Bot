#!/usr/bin/env python3
"""
groww_token.py
==============
Shared, cross-process access-token cache for the Groww API.

WHY THIS EXISTS
---------------
Groww rate-limits the token endpoint (POST /v1/token/api/access).
Every bot in this repo used to call GrowwAPI.get_access_token() on startup,
so launching 2-3 bots in a row (or restarting one a few times) throws:

    growwapi.groww.exceptions.GrowwAPIRateLimitException

An access token is valid for hours, so there is no reason to mint a new one
per process. This module mints ONE token, caches it on disk, and every bot
reuses it until it is close to expiry.

USAGE (drop-in replacement)
---------------------------
    # before
    totp  = pyotp.TOTP(TOTP_SECRET).now()
    token = GrowwAPI.get_access_token(api_key=API_KEY, totp=totp)

    # after
    from groww_token import get_access_token
    token = get_access_token(API_KEY, TOTP_SECRET)

Cache file:  .groww_token.json  (next to this file — add it to .gitignore)
Force a new token:  python3 groww_token.py --refresh
"""

from __future__ import annotations

import base64
import json
import os
import time

import pyotp
from growwapi import GrowwAPI

_DIR         = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH   = os.path.join(_DIR, ".groww_token.json")
_LOCK_PATH   = CACHE_PATH + ".lock"

# Refresh this many seconds before the token actually expires.
SAFETY_MARGIN_SEC = 15 * 60
# Used when the token carries no readable `exp` claim.
FALLBACK_TTL_SEC  = 6 * 3600
# Backoff schedule (seconds) when the mint call is rate-limited.
_BACKOFF          = (20, 45, 90, 180)
# How long a stale lock file is honoured before being ignored.
_LOCK_STALE_SEC   = 120


# ─────────────────────────────────────────────────────────────
#  JWT helpers
# ─────────────────────────────────────────────────────────────
def _jwt_expiry(token: str) -> float:
    """Best-effort `exp` (epoch seconds) from an unverified JWT. 0.0 if unknown."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)          # restore base64 padding
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp else 0.0
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────
#  Cache read / write
# ─────────────────────────────────────────────────────────────
def _read_cache() -> str | None:
    """Return a cached token that is still comfortably valid, else None."""
    try:
        with open(CACHE_PATH) as fh:
            blob = json.load(fh)
        token = blob.get("token")
        expiry = float(blob.get("expiry", 0))
        if token and time.time() < expiry - SAFETY_MARGIN_SEC:
            return token
    except Exception:
        pass
    return None


def _write_cache(token: str) -> None:
    expiry = _jwt_expiry(token) or (time.time() + FALLBACK_TTL_SEC)
    tmp = CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({"token": token, "expiry": expiry,
                       "written_at": time.time()}, fh)
        os.replace(tmp, CACHE_PATH)                    # atomic
        os.chmod(CACHE_PATH, 0o600)                    # token == credential
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
#  Cross-process lock
#  Stops two bots launched simultaneously from both minting a token.
# ─────────────────────────────────────────────────────────────
def _acquire_lock() -> int | None:
    try:
        age = time.time() - os.path.getmtime(_LOCK_PATH)
        if age > _LOCK_STALE_SEC:
            os.unlink(_LOCK_PATH)                      # previous run died
    except OSError:
        pass
    try:
        return os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return None
    except OSError:
        return None


def _release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
        os.unlink(_LOCK_PATH)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────
def get_access_token(api_key: str, totp_secret: str,
                     force_refresh: bool = False,
                     verbose: bool = True) -> str:
    """
    Return a valid Groww access token, minting one only when necessary.

    Raises the underlying GrowwAPI exception if every attempt fails.
    """
    if not force_refresh:
        cached = _read_cache()
        if cached:
            if verbose:
                print("🔑 Groww token: reusing cached token "
                      f"(valid ~{int((_jwt_expiry(cached) - time.time()) / 60)} min)")
            return cached

    lock_fd = _acquire_lock()
    if lock_fd is None:
        # Another process is minting right now — wait for it to publish.
        for _ in range(30):
            time.sleep(2)
            cached = _read_cache()
            if cached:
                if verbose:
                    print("🔑 Groww token: picked up token minted by another bot")
                return cached
            if not os.path.exists(_LOCK_PATH):
                break
        lock_fd = _acquire_lock()                       # take over and mint

    try:
        last_err: Exception | None = None
        for attempt, wait in enumerate((0,) + _BACKOFF):
            if wait:
                if verbose:
                    print(f"⏳ Groww token rate-limited — retrying in {wait}s "
                          f"(attempt {attempt + 1}/{len(_BACKOFF) + 1})")
                time.sleep(wait)
            try:
                totp  = pyotp.TOTP(totp_secret).now()
                token = GrowwAPI.get_access_token(api_key=api_key, totp=totp)
                _write_cache(token)
                if verbose:
                    print("🔑 Groww token: minted a fresh token and cached it")
                return token
            except Exception as exc:
                last_err = exc
                if "RateLimit" not in type(exc).__name__:
                    raise
                # A sibling bot may have succeeded while we were sleeping.
                cached = _read_cache()
                if cached:
                    return cached

        raise last_err                                  # type: ignore[misc]
    finally:
        _release_lock(lock_fd)


def init_client(api_key: str, totp_secret: str,
                force_refresh: bool = False,
                verbose: bool = True) -> tuple:
    """Convenience: returns (GrowwAPI client, access_token)."""
    token = get_access_token(api_key, totp_secret,
                             force_refresh=force_refresh, verbose=verbose)
    return GrowwAPI(token), token


def clear_cache() -> None:
    for path in (CACHE_PATH, _LOCK_PATH):
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    import sys

    if "--clear" in sys.argv:
        clear_cache()
        print("🗑️  Token cache cleared.")
        sys.exit(0)

    if "--refresh" in sys.argv:
        # Credentials live in the bots; borrow them from CHART_LEVEL_ANALYZER.
        import CHART_LEVEL_ANALYZER as _cla
        get_access_token(_cla.API_KEY, _cla.TOTP_SECRET, force_refresh=True)
        sys.exit(0)

    cached = _read_cache()
    if cached:
        mins = int((_jwt_expiry(cached) - time.time()) / 60)
        print(f"✅ Cached token valid for ~{mins} more min  ({CACHE_PATH})")
    else:
        print("❌ No valid cached token — the next bot you start will mint one.")
        print("   Run with --refresh to mint it now, --clear to wipe the cache.")
