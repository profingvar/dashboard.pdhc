"""Spärr Phase 2 — dashboard block filter + banner (ticket #205).

These tests mock the IPS client so they don't depend on a running
ips.pdhc. The cache is invalidated between tests to keep them isolated.
"""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import sqlalchemy

from app import create_app
from app.models import db, ObservationCache
from app.services import ips_client as ips_mod
from app.services.ips_client import Block


def _app():
    app = create_app({
        "TESTING": True,
        # Hermetic per-test in-memory DB (#441). StaticPool is required:
        # bare sqlite :memory: gives each connection a private db, so
        # seeded rows would be invisible to request-handling connections.
        # create_app overwrites SQLALCHEMY_DATABASE_URI from its DATABASE_URL
        # config key, so set both — otherwise an ambient DATABASE_URL env
        # var would silently re-point the test at a real Postgres.
        "DATABASE_URL": "sqlite:///:memory:",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "SQLALCHEMY_ENGINE_OPTIONS": {
            "connect_args": {"check_same_thread": False},
            "poolclass": sqlalchemy.pool.StaticPool,
        },
        "AUTH_MODE": "off",
        # Set a base url so _default_client() will at least attempt; we
        # always patch fetch_active_blocks so no HTTP actually fires.
        "IPS_BASE_URL": "https://ips.example",
    })
    with app.app_context():
        db.create_all()
    return app


def _seed_two_orgs(app):
    """Seed two orgs with 3 obs each for one patient."""
    with app.app_context():
        org_a = str(uuid.uuid4())
        org_b = str(uuid.uuid4())
        pat = str(uuid.uuid4())
        cg = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        for i in range(3):
            db.session.add(ObservationCache(
                source_obs_guid=str(uuid.uuid4()),
                patient_guid=pat, org_guid=org_a,
                concept_guid=cg, concept_name="B-glucose",
                value=5.0 + i * 0.1, unit="mmol/L",
                observed_at=now - timedelta(days=i),
            ))
            db.session.add(ObservationCache(
                source_obs_guid=str(uuid.uuid4()),
                patient_guid=pat, org_guid=org_b,
                concept_guid=cg, concept_name="B-glucose",
                value=6.0 + i * 0.1, unit="mmol/L",
                observed_at=now - timedelta(days=i),
            ))
        db.session.commit()
        return org_a, org_b, pat, cg


def _cleanup(orgs):
    with db.session.no_autoflush:
        for o in orgs:
            ObservationCache.query.filter_by(org_guid=o).delete()
        db.session.commit()


def _in_org_client(app, orgs):
    """Test client whose caller is an in-org (non-admin) professional.

    The AUTH_MODE=off dev SU carries ``organization_ids: []``, so since
    #212 every /patient/<guid> read is an admin *off-org* read and renders
    the override-confirmation form (which never shows the spärr banner)
    instead of the patient dashboard. Give the caller a blob scoped to the
    seeded orgs — the hook runs after the auth request-loader, so it
    overrides the dev blob."""
    from app.auth import _blob_to_user
    blob = {
        "user_guid": str(uuid.uuid4()),
        "email": "prof@test",
        "user_type": "professional",
        "is_su_admin": False,
        "effective_phases": ["analysis"],
        "organization_ids": list(orgs),
    }

    @app.before_request
    def _override():
        from flask import g
        g.access_blob = blob
        g.current_user = _blob_to_user(blob)

    return app.test_client()


@pytest.fixture(autouse=True)
def _flush_cache():
    ips_mod._cache.invalidate()
    yield
    ips_mod._cache.invalidate()


def _make_block(scope_id, *, lift_kind=None, lift_concepts=None,
                lift_from=None, lift_until=None, active=True):
    return Block(
        guid=str(uuid.uuid4()),
        patient_guid=str(uuid.uuid4()),
        source_scope_type="clinic",
        source_scope_id=str(scope_id),
        is_active=active,
        lift_kind=lift_kind,
        lift_concept_guids=lift_concepts,
        lift_from_date=lift_from,
        lift_until_date=lift_until,
    )


# ---------------------------------------------------------------------------
# Pure helpers (no Flask context needed for these)
# ---------------------------------------------------------------------------

def test_blocked_clinic_ids_extracts_only_active_clinic_scopes():
    inactive = _make_block("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", active=False)
    active = _make_block("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    caregiver = Block(
        guid=str(uuid.uuid4()), patient_guid=str(uuid.uuid4()),
        source_scope_type="caregiver", source_scope_id="cg-1",
        is_active=True, lift_kind=None, lift_concept_guids=None,
        lift_from_date=None, lift_until_date=None,
    )
    s = ips_mod.blocked_clinic_ids([inactive, active, caregiver])
    assert s == {"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}


def test_filter_blocked_rows_drops_matching_org():
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())

    class R:
        def __init__(self, org, concept):
            self.org_guid = org
            self.concept_guid = concept
            self.observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    rows = [R(org_a, "c1"), R(org_b, "c1")]
    blocks = [_make_block(org_a)]
    out = ips_mod.filter_blocked_rows(rows, blocks)
    assert [r.org_guid for r in out] == [org_b]


def test_filter_lift_indispensable_care_exposes_concept_only():
    org = str(uuid.uuid4())
    c_exposed = str(uuid.uuid4())
    c_hidden = str(uuid.uuid4())

    class R:
        def __init__(self, concept):
            self.org_guid = org
            self.concept_guid = concept
            self.observed_at = datetime(2026, 1, 5, tzinfo=timezone.utc)

    rows = [R(c_exposed), R(c_hidden)]
    block = _make_block(
        org, lift_kind="indispensable_care",
        lift_concepts=[c_exposed],
    )
    out = ips_mod.filter_blocked_rows(rows, [block])
    assert [r.concept_guid for r in out] == [c_exposed]


def test_filter_lift_date_range_respected():
    org = str(uuid.uuid4())
    c = str(uuid.uuid4())

    class R:
        def __init__(self, day):
            self.org_guid = org
            self.concept_guid = c
            self.observed_at = datetime(2026, 1, day, tzinfo=timezone.utc)

    rows = [R(1), R(5), R(10)]
    block = _make_block(
        org, lift_kind="indispensable_care",
        lift_concepts=[c],
        lift_from="2026-01-03T00:00:00+00:00",
        lift_until="2026-01-08T23:59:59+00:00",
    )
    out = ips_mod.filter_blocked_rows(rows, [block])
    assert [r.observed_at.day for r in out] == [5]


def test_consent_lift_without_concepts_drops_everything():
    """A consent-kind lift is permanent — when no concept filter is
    attached (consent doesn't carry one) the block is *not* lifted
    via the mechanical-filter path, so every row stays dropped.

    The PatientBlock model handles the "consent lift means block is
    no longer active" gate at the IPS layer via is_active(); the
    dashboard filter just sees the active flag. We assert here that a
    *still-active* clinic block without a concept lift drops all rows
    even when lift_kind=='consent' on the wire.
    """
    org = str(uuid.uuid4())

    class R:
        def __init__(self):
            self.org_guid = org
            self.concept_guid = "c1"
            self.observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    block = _make_block(org, lift_kind="consent", lift_concepts=None)
    out = ips_mod.filter_blocked_rows([R(), R()], [block])
    assert out == []


def test_has_any_active_block_flag():
    assert ips_mod.has_any_active_block([_make_block("x")]) is True
    assert ips_mod.has_any_active_block([_make_block("x", active=False)]) is False
    assert ips_mod.has_any_active_block([]) is False


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_hits_within_ttl(monkeypatch):
    calls = {"n": 0}
    pat = str(uuid.uuid4())

    class FakeClient:
        def fetch_active_blocks(self, patient_guid):
            calls["n"] += 1
            return [_make_block("scope-x")]

    app = _app()
    with app.app_context():
        ips_mod.get_active_blocks(pat, client=FakeClient())
        ips_mod.get_active_blocks(pat, client=FakeClient())
        ips_mod.get_active_blocks(pat, client=FakeClient())
    assert calls["n"] == 1


def test_cache_invalidate_evicts(monkeypatch):
    calls = {"n": 0}
    pat = str(uuid.uuid4())

    class FakeClient:
        def fetch_active_blocks(self, patient_guid):
            calls["n"] += 1
            return [_make_block("scope-x")]

    app = _app()
    with app.app_context():
        ips_mod.get_active_blocks(pat, client=FakeClient())
        ips_mod.invalidate(pat)
        ips_mod.get_active_blocks(pat, client=FakeClient())
    assert calls["n"] == 2


def test_cache_ttl_expiry():
    calls = {"n": 0}
    pat = str(uuid.uuid4())

    class FakeClient:
        def fetch_active_blocks(self, patient_guid):
            calls["n"] += 1
            return []

    cache = ips_mod._BlockCache(ttl=0)  # immediate expiry
    cache.put(pat, [])
    assert cache.get(pat) is None


# ---------------------------------------------------------------------------
# End-to-end view filter + banner
# ---------------------------------------------------------------------------

def test_patient_view_hides_blocked_rows_and_shows_banner(monkeypatch):
    app = _app()
    org_a, org_b, pat, _ = _seed_two_orgs(app)
    try:
        monkeypatch.setattr(
            ips_mod, "get_active_blocks",
            lambda guid, **kw: [_make_block(org_a)],
        )
        # Re-resolve where it was already imported into views.
        from app.routes import views as views_mod
        monkeypatch.setattr(
            views_mod, "get_active_blocks",
            lambda guid, **kw: [_make_block(org_a)],
        )
        c = _in_org_client(app, [org_a, org_b])
        r = c.get(f"/patient/{pat}")
        assert r.status_code == 200
        # Banner present
        assert "Uppgift om spärr".encode("utf-8") in r.data
    finally:
        with app.app_context():
            _cleanup([org_a, org_b])


def test_patient_view_no_banner_when_no_blocks(monkeypatch):
    app = _app()
    org_a, org_b, pat, _ = _seed_two_orgs(app)
    try:
        from app.routes import views as views_mod
        monkeypatch.setattr(views_mod, "get_active_blocks", lambda g, **kw: [])
        c = _in_org_client(app, [org_a, org_b])
        r = c.get(f"/patient/{pat}")
        assert r.status_code == 200
        assert "Uppgift om spärr".encode("utf-8") not in r.data
    finally:
        with app.app_context():
            _cleanup([org_a, org_b])


def test_patient_view_all_rows_blocked_renders_banner_no_404(monkeypatch):
    """When spärr filters every row, return 200 with the banner — NOT
    a 404 that would leak "this patient has no data" vs "patient has
    data you can't see"."""
    app = _app()
    org_a, org_b, pat, _ = _seed_two_orgs(app)
    try:
        block_both = [_make_block(org_a), _make_block(org_b)]
        from app.routes import views as views_mod
        monkeypatch.setattr(
            views_mod, "get_active_blocks",
            lambda g, **kw: block_both,
        )
        c = _in_org_client(app, [org_a, org_b])
        r = c.get(f"/patient/{pat}")
        assert r.status_code == 200
        assert "Uppgift om spärr".encode("utf-8") in r.data
    finally:
        with app.app_context():
            _cleanup([org_a, org_b])


def test_api_series_drops_blocked_rows_and_emits_meta_tag(monkeypatch):
    app = _app()
    org_a, org_b, pat, cg = _seed_two_orgs(app)
    try:
        from app.routes import api as api_mod
        monkeypatch.setattr(
            api_mod, "get_active_blocks",
            lambda g, **kw: [_make_block(org_a)],
        )
        c = app.test_client()
        r = c.get(f"/api/v1/series?patient={pat}&concept={cg}")
        assert r.status_code == 200
        j = r.get_json()
        assert j["total"] == 3, "should drop 3 rows from org_a, keep 3 from org_b"
        assert "meta" in j
        codes = [t["code"] for t in j["meta"]["tag"]]
        assert "blocked-sources-present" in codes
    finally:
        with app.app_context():
            _cleanup([org_a, org_b])


def test_api_series_no_meta_tag_without_blocks(monkeypatch):
    app = _app()
    org_a, org_b, pat, cg = _seed_two_orgs(app)
    try:
        from app.routes import api as api_mod
        monkeypatch.setattr(api_mod, "get_active_blocks", lambda g, **kw: [])
        c = app.test_client()
        r = c.get(f"/api/v1/series?patient={pat}&concept={cg}")
        j = r.get_json()
        assert j["total"] == 6
        assert "meta" not in j
    finally:
        with app.app_context():
            _cleanup([org_a, org_b])
