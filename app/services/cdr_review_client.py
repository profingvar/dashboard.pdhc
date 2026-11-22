"""Client for cdr1's EPS review API (#1044, epic #1039).

Contract: `plans/patient_app_contract.md` s4.2 (workspace repo), and s7.4
for the card versions of v2 (#1049):

    GET  /api/v1/reviews/cards?status=pending
    POST /api/v1/reviews/cards/<card_version>/approve  {"amend": {guid: value}?, "note"?}
    POST /api/v1/reviews/cards/<card_version>/reject   {"reason"}

    GET  /api/v1/reviews?status=pending[&patient_guid=]
    POST /api/v1/reviews/<guid>/approve   {"value": <optional>, "note": <optional>}
    POST /api/v1/reviews/<guid>/reject    {"reason": <required>}

AUTH IS THE OPERATOR'S OWN BEARER, AND NOTHING ELSE. `Cdr1Client` sends the
session bearer AND dashboard's service key, which is right for its
care-delivery reads. Here the key is deliberately left out: the person
deciding must be the person sso authenticated, and no machine identity may
decide. cdr1's review routes do not look at a service key at all; on cdr1's
ordinary routes a key is evaluated BEFORE a bearer, so a call carrying both
would be the service's call. Not sending it keeps this client correct under
either, and sends no credential the call does not need.

AS BUILT, cdr1 answers the SAME flat 403 for an original that does not exist
and one outside the caller's care units (so probing learns nothing), and adds
`can_decide`, `limit` and `truncated` to the list. A JSON 404 is still handled
as the contract words it.

EVERY FAILURE IS A NAMED EXCEPTION. Nothing here returns an empty list on an
error: "nothing to review" and "you were refused" are different statements,
and a refusal read as an empty queue is a queue that looks finished while
patients' answers wait.
"""
from __future__ import annotations

import requests
from flask import current_app, session

DEFAULT_TIMEOUT = 8.0

#: "no amended value". None cannot serve: None is a JSON value.
UNCHANGED = object()


class ReviewError(Exception):
    """Base. `status_code` is cdr1's HTTP status when there was one;
    `cdr_error` is the `error` code of cdr1's JSON body when there was one."""

    def __init__(self, message: str = "", *, status_code: int | None = None,
                 cdr_error: str | None = None, cdr_code: str | None = None):
        super().__init__(message or self.__class__.__name__)
        self.status_code = status_code
        self.cdr_error = cdr_error
        self.cdr_code = cdr_code


class ReviewNotConfigured(ReviewError):
    """CDR1_BASE_URL is unset. Refused by name; nothing is fabricated."""


class ReviewNoBearer(ReviewError):
    """No operator bearer in the session (AUTH_MODE=off, or no login). The
    review API has no other credential by design, so the call is not made."""


class ReviewUnreachable(ReviewError):
    """Asked, and no usable answer: network, 5xx, 401, non-JSON, wrong shape."""


class ReviewApiMissing(ReviewError):
    """cdr1 has no such route: it predates the review API. Told apart from
    cdr1's own JSON 404 ("no such original in your scope")."""


class ReviewForbidden(ReviewError):
    """cdr1 answered 403: this professional may not do this."""


class ReviewNotFound(ReviewError):
    """cdr1 answered 404 as JSON: no such original in the caller's scope."""


class ReviewAlreadyDecided(ReviewError):
    """cdr1 answered 409: someone has already decided this original."""


class ReviewSuperseded(ReviewAlreadyDecided):
    """cdr1 answered 409 `superseded` (#1049, contract s7.4): the patient
    sent a newer version of that card section, so this version can no longer
    be decided. A subclass of ReviewAlreadyDecided so that a caller handling
    only "409" still treats it as "not recorded"; the card routes catch it
    FIRST to say which of the two happened."""


class ReviewInvalid(ReviewError):
    """cdr1 answered 422 (or 400): it refused the content of the decision."""


def _error_fields(r) -> tuple[bool, str | None, str | None]:
    """(is_json_object, `error`, `code`) of an error response. cdr1's 422 is
    `{"error": "validation", "code": "wrong_type|out_of_range|required|..."}`.
    Its English `message` is not used: the user is answered in Swedish."""
    try:
        body = r.json()
    except ValueError:
        return False, None, None
    if not isinstance(body, dict):
        return False, None, None
    err = body.get("error")
    code = body.get("code")
    return True, (err if isinstance(err, str) else None), \
        (code if isinstance(code, str) else None)


class CdrReviewClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- transport --------------------------------------------------------
    def _headers(self) -> dict:
        h = {"Accept": "application/json",
             "Authorization": f"Bearer {self.token}"}
        # X2: forward the operator session id, as every other client does.
        try:
            from app.services.session_headers import outbound_session_headers
            h.update(outbound_session_headers())
        except Exception:  # noqa: BLE001 — session headers are best-effort
            pass
        return h

    def _call(self, method: str, path: str, *, params=None, json_body=None,
              ok: tuple[int, ...] = (200,)) -> dict:
        if not self.base_url:
            raise ReviewNotConfigured("CDR1_BASE_URL is not set")
        if not self.token:
            raise ReviewNoBearer("no operator bearer in the session")
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                r = requests.get(url, headers=self._headers(), params=params,
                                 timeout=self.timeout)
            else:
                r = requests.post(url, headers=self._headers(), json=json_body,
                                  timeout=self.timeout)
        except requests.RequestException as exc:
            raise ReviewUnreachable(
                f"cdr1 network error: {type(exc).__name__}") from exc

        st = r.status_code
        if st in ok:
            try:
                body = r.json()
            except ValueError as exc:
                raise ReviewUnreachable("cdr1 answered with a non-JSON body",
                                        status_code=st) from exc
            if not isinstance(body, dict):
                raise ReviewUnreachable("cdr1 answered with a non-object body",
                                        status_code=st)
            return body

        is_json, err, code = _error_fields(r)
        kw = {"status_code": st, "cdr_error": err, "cdr_code": code}
        if st == 403:
            raise ReviewForbidden("cdr1 refused (403)", **kw)
        if st == 404:
            # Flask's default 404 is HTML: the route does not exist on this
            # cdr1. cdr1's own "not in your scope" is a JSON object.
            if is_json:
                raise ReviewNotFound("cdr1: no such original in scope", **kw)
            raise ReviewApiMissing("cdr1 has no review API (404, not JSON)", **kw)
        if st == 409:
            if err == "superseded":
                raise ReviewSuperseded("cdr1: superseded by a newer version", **kw)
            raise ReviewAlreadyDecided("cdr1: already decided", **kw)
        if st in (400, 422):
            raise ReviewInvalid(f"cdr1 refused the decision ({st})", **kw)
        raise ReviewUnreachable(f"cdr1 answered {st}", **kw)

    # -- the three calls ----------------------------------------------------
    def list_pending(self, patient_guid: str | None = None) -> dict:
        """Pending originals in the caller's scope: `{"items": [...], "total": N}`.

        An empty `items` is an ANSWER. Anything that is not an answer raises.
        A body without an `items` list raises too: a cdr1 that answered 200
        with some other shape has not said "nothing to review".
        """
        params = [("status", "pending")]
        if patient_guid:
            params.append(("patient_guid", patient_guid))
        body = self._call("GET", "/api/v1/reviews", params=params)
        items = body.get("items")
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise ReviewUnreachable("cdr1 review list carried no items list",
                                    status_code=200)
        total = body.get("total")
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(items)
        return {"items": items, "total": total,
                "truncated": body.get("truncated") is True}

    def approve(self, review_guid: str, *, value=UNCHANGED,
                note: str | None = None) -> dict:
        """Approve; with `value`, amend and approve. Returns cdr1's
        `{"endorsement_guid", "amended"}`."""
        body: dict = {}
        if value is not UNCHANGED:
            body["value"] = value
        if note:
            body["note"] = note
        return self._call("POST", f"/api/v1/reviews/{review_guid}/approve",
                          json_body=body, ok=(200, 201))

    def reject(self, review_guid: str, reason: str) -> dict:
        return self._call("POST", f"/api/v1/reviews/{review_guid}/reject",
                          json_body={"reason": reason}, ok=(200,))


    # -- card versions (#1049, contract s7.4) -------------------------------
    def list_cards(self) -> dict:
        """Pending card versions in the caller's scope:
        `{"items": [{"card_version", "patient_guid", "section", "submitted_at",
        "rows": [...], "endorsed": {...} | null}]}`.

        Same rule as `list_pending`: an empty `items` is an answer, anything
        else that is not the contract's shape raises. A card whose `rows` is
        not a list, or whose `endorsed` is neither null nor an object with a
        `rows` list, makes the WHOLE answer unusable rather than being dropped:
        a dropped card is a card nobody sees.
        """
        body = self._call("GET", "/api/v1/reviews/cards",
                          params=[("status", "pending")])
        items = body.get("items")
        if not isinstance(items, list):
            raise ReviewUnreachable("cdr1 card list carried no items list",
                                    status_code=200)
        for it in items:
            if not isinstance(it, dict) or not it.get("card_version"):
                raise ReviewUnreachable("cdr1 card list: an item without card_version",
                                        status_code=200)
            rows = it.get("rows")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ReviewUnreachable("cdr1 card list: an item without a rows list",
                                        status_code=200)
            end = it.get("endorsed")
            if end is not None and not (
                    isinstance(end, dict) and isinstance(end.get("rows"), list)
                    and all(isinstance(r, dict) for r in end["rows"])):
                raise ReviewUnreachable("cdr1 card list: endorsed is not null or a version",
                                        status_code=200)
        return {"items": items, "truncated": body.get("truncated") is True}

    def approve_card(self, card_version: str, *, amend: dict | None = None,
                     note: str | None = None) -> dict:
        """Approve a card version; `amend` maps row guid -> amended value.
        Returns cdr1's `{"endorsed": N, "amended": M}`."""
        body: dict = {}
        if amend:
            body["amend"] = amend
        if note:
            body["note"] = note
        return self._call("POST", f"/api/v1/reviews/cards/{card_version}/approve",
                          json_body=body, ok=(200, 201))

    def reject_card(self, card_version: str, reason: str) -> dict:
        return self._call("POST", f"/api/v1/reviews/cards/{card_version}/reject",
                          json_body={"reason": reason}, ok=(200,))


def build_review_client() -> CdrReviewClient:
    """From app config + the operator's bearer in the session. Deliberately
    NOT given DASHBOARD_PDHC_SERVICE_KEY; see the module docstring."""
    token = None
    try:
        token = session.get("sso_token")
    except RuntimeError:  # no request context
        token = None
    return CdrReviewClient(
        base_url=current_app.config.get("CDR1_BASE_URL") or "",
        token=token,
    )
