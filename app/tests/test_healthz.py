from app import create_app


def test_healthz():
    app = create_app({"TESTING": True})
    client = app.test_client()
    r = client.get("/healthz")
    # Status code is 200 or 503 depending on whether the DB probe succeeds.
    # In-memory sqlite from TESTING config should succeed, so expect 200.
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok"
    assert data["service"] == "dashboard.pdhc"
    assert data["database"] in ("connected", "unavailable")
