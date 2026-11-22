"""CSRF token for state-changing routes (#1044).

The dashboard had no CSRF mechanism before this: its only protection on a POST
was the session cookie's `SameSite=Lax`, which a browser withholds on a
cross-site POST but which is a browser default, not a server-side check. The
EPS review routes record a clinical decision in a professional's name, so they
get a real one: a random per-session token, sent back as the hidden form field
`csrf_token` (or the header `X-CSRF-Token`), compared in constant time.

Used by `app/routes/eps_review.py` only. The older POST routes
(`/admin-read-attest`, `/api/v1/designs`) are unchanged.
"""
from __future__ import annotations

import hmac
import secrets

from flask import request, session

_KEY = "_csrf_token"


def csrf_token() -> str:
    """The session's token, minted on first use."""
    tok = session.get(_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        session[_KEY] = tok
    return tok


def csrf_valid() -> bool:
    """True only when the request carries the session's token. A session with
    no token minted has no valid token: an absent expectation never matches."""
    expected = session.get(_KEY)
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not sent:
        return False
    return hmac.compare_digest(str(expected), str(sent))
