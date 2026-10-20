"""Runtime switches for the silent authorization service.

The URL and public key are compiled into the distributed EXE.  In the frozen
executable the environment overrides are ignored, so the production policy
cannot be redirected or switched off from outside the process.  The overrides
remain available for source-level staging and acceptance tests, where the app
runs unfrozen.
"""

from __future__ import annotations

import os
import sys


_FROZEN = bool(getattr(sys, "frozen", False))


# Replace these two empty defaults with the production Vercel URL and the
# 32-byte Ed25519 public key before building the distributed EXE.  Colleagues
# do not need to configure anything on their Windows accounts.
DEFAULT_AUTH_API_URL = "https://license-api-steel.vercel.app"
DEFAULT_AUTH_PUBLIC_KEY_B64 = "1IHKz3c3n2QkKY6j/3Tf/yAUTythA7aiTCvLqTyFpDk="

# Additional public keys accepted alongside the primary one.  A lease verifies
# if it matches ANY key here, which lets the signing key be rotated: ship a
# release that trusts both the old and new key, move the server to the new key,
# then drop the old key in a later release.  Keep newest first.
DEFAULT_AUTH_PUBLIC_KEYS_B64: list[str] = []


def _override(name: str, default: str) -> str:
    # The frozen EXE never reads these; only source/test runs do.
    if _FROZEN:
        return default
    return os.getenv(name, default)


AUTH_API_URL = _override("PHOTOCOLLECT_AUTH_API_URL", DEFAULT_AUTH_API_URL).strip().rstrip("/")
AUTH_PUBLIC_KEY_B64 = _override("PHOTOCOLLECT_AUTH_PUBLIC_KEY", DEFAULT_AUTH_PUBLIC_KEY_B64).strip()

# De-duplicated list of every trusted key, primary first.
AUTH_PUBLIC_KEYS_B64: list[str] = []
for _key in [AUTH_PUBLIC_KEY_B64, *DEFAULT_AUTH_PUBLIC_KEYS_B64]:
    _key = str(_key or "").strip()
    if _key and _key not in AUTH_PUBLIC_KEYS_B64:
        AUTH_PUBLIC_KEYS_B64.append(_key)

_explicit_enabled = ("" if _FROZEN else os.getenv("PHOTOCOLLECT_AUTH_ENABLED", "")).strip().lower()
if _explicit_enabled in {"0", "false", "no", "off"}:
    AUTH_ENABLED = False
elif _explicit_enabled in {"1", "true", "yes", "on"}:
    AUTH_ENABLED = bool(AUTH_API_URL and AUTH_PUBLIC_KEYS_B64)
else:
    # Source-level tests and acceptance smoke runs should remain usable without
    # a network lease.  The distributed PyInstaller executable is frozen and
    # enables the production policy automatically.
    AUTH_ENABLED = bool(AUTH_API_URL and AUTH_PUBLIC_KEYS_B64 and _FROZEN)
AUTH_CHECK_INTERVAL_SECONDS = 2 * 60 * 60
AUTH_LEASE_SECONDS = 24 * 60 * 60
AUTH_REQUEST_TIMEOUT_SECONDS = 8.0
