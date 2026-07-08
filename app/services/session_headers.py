"""Operator-session propagation (X2, tickets #408 / #423).

Per-repo copy of the gateway.pdhc reference helper (there is deliberately no
shared library across the PDHC services). Forwards the operator's SSO session
id (the JWT ``sid`` claim, ticket #191) on every synchronous onward call this
service makes while serving an operator request, so one session_id threads the
whole request -> gateway -> cdr chain end-to-end for PDL kontroller.

``outbound_session_headers()`` returns ``{'X-Operator-Session-Id': <sid>}`` when a
session id is available and ``{}`` otherwise — it never forwards an empty header,
so legacy / machine-to-machine calls stay header-less.

NOTE for the federated fanout: resolve this in the REQUEST thread (the fanout's
main body) and pass the result into the per-CDR worker threads' headers — the
worker threads have no Flask request context.
"""
from flask import request, session


def current_session_id():
    """Resolve the operator SSO session id for the current request.

    Order (mirrors gateway.pdhc #222): forwarded ``X-Operator-Session-Id``
    header, then the ``session_id`` (JWT ``sid``, #191) on the validated SSO
    blob, else None.
    """
    try:
        header_val = request.headers.get("X-Operator-Session-Id")
    except RuntimeError:
        header_val = None
    if header_val:
        return header_val[:128]
    blob = session.get("access_blob") if session else None
    if isinstance(blob, dict):
        sid = blob.get("session_id")
        if sid:
            return str(sid)[:128]
    return None


def outbound_session_headers(session_id=None):
    """Headers to attach to an onward operator-context service call (X2 #408).

    Pass ``session_id`` explicitly for calls made outside a request context.
    """
    sid = session_id if session_id is not None else current_session_id()
    if sid:
        return {"X-Operator-Session-Id": str(sid)[:128]}
    return {}
