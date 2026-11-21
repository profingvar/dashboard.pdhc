"""#1019 (epic #1015): ips.pdhc is being renamed eps.pdhc.

Every read of an `IPS_*` environment variable goes through `eps_env`, which
reads the new `EPS_*` name first and the old `IPS_*` name as the deprecated
fallback, so the operator can add `EPS_*` to `.env` before or after deploying
this code and nothing breaks in between.

Resolution, for `eps_env("BASE_URL")`:

1. `EPS_BASE_URL`, if it is set to something other than blank. Blank counts as
   unset, because docker-compose `${EPS_BASE_URL:-}` hands an absent key to the
   container as an empty string.
2. Otherwise `IPS_BASE_URL`, exactly as it was read before (including an empty
   value, which is how a deployment says "not configured").
3. Otherwise `default` — whatever the call site passed BEFORE this helper
   existed. Never a new one: unset must still read as unset (#833).
"""
from __future__ import annotations

import os
from typing import Optional


def eps_env(suffix: str, default: Optional[str] = "") -> Optional[str]:
    new = os.environ.get("EPS_" + suffix)
    if new is not None and new.strip():
        return new
    old = os.environ.get("IPS_" + suffix)
    if old is not None:
        return old
    if new is not None:
        return new
    return default
