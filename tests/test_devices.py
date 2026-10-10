"""The "Use on your devices" page settings, and requesting books/comics from search."""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    from app import indexer
    indexer.run(main.cfg)
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    token = admin.post("/api/accounts/invites", json={"link_base": "https://media.example/", "can_download": True}).json()["url"].split("#invite=")[1]
    member = TestClient(main.app)
    member.post(f"/api/auth/invite/{token}", json={"username": "kim", "password": "member-pass-1"})
    return main, admin, member


def test_devices_page_shows_only_what_the_admin_filled_in(env):
    main, admin, member = env
    empty = member.get("/api/devices").json()
    assert empty["apps"] == {} and "contact" not in empty
    r = admin.post("/api/setup/devices", json={
        "contact": "Sam", "login_note": "Text me for a login.", "network_note": "Ask for a Tailscale invite.",
        "jellyfin_url": "https://tv.example", "jellyfin_private": True,
        "abs_url": "", "abs_private": True,                       # a flag without an address is dropped
        "opds_url": "https://books.example/opds"})
    assert r.status_code == 200
    seen = member.get("/api/devices").json()
    assert seen["contact"] == "Sam" and seen["login_note"] == "Text me for a login."
    assert seen["apps"] == {"jellyfin": {"url": "https://tv.example", "private": True},
                            "opds": {"url": "https://books.example/opds", "private": False}}
    assert admin.get("/api/setup/devices").json()["abs_private"] is False


def test_devices_settings_are_admin_only_and_validated(env):
    main, admin, member = env
    assert member.post("/api/setup/devices", json={"contact": "x"}).status_code == 403
    assert member.get("/api/setup/devices").status_code == 403
    assert admin.post("/api/setup/devices", json={"jellyfin_url": "tv.example"}).status_code == 400
    assert admin.post("/api/setup/devices", json={"komga_url": "https://a b"}).status_code == 400
    assert admin.post("/api/setup/devices", json={"login_note": "x" * 501}).status_code == 400
    assert TestClient(main.app).get("/api/devices").status_code == 401


def test_book_search_lists_candidates_and_marks_owned(env, monkeypatch):
    main, admin, member = env
    assert member.get("/api/request/book/search", params={"q": "dune"}).json()["enabled"] is False
    from app import requests_
    monkeypatch.setattr(requests_, "shelfmark_enabled", lambda cfg: True)
    calls = []

    def fake(cfg, query, kind):
        calls.append(kind)
        return [{"title": "Dune", "authors": ["Frank Herbert"], "year": "1965", "cover": "http://c/1"},
                {"title": "Summary of Dune", "authors": ["Quick Reads"]},
                {"title": "Dune", "authors": ["Frank Herbert"], "year": "2005", "cover": "http://c/dup"},
                {"title": "Dune: House Atreides", "authors": ["Brian Herbert"], "year": "2020", "cover": "http://c/2"}]
    monkeypatch.setattr(requests_, "book_candidates", fake)
    monkeypatch.setattr(main, "_library_index", lambda: ({}, {}))
    data = member.get("/api/request/book/search", params={"q": "dune"}).json()
    assert data["enabled"] and data["manual_pick"] and calls == ["ebook"]     # comics are searched as ebooks
    assert [r["title"] for r in data["results"]] == ["Dune", "Dune: House Atreides"]   # junk + duplicate dropped
    first = data["results"][0]
    assert first["status"] == "not_requested" and first["poster"] == "http://c/1" and first["any_format"] is True
    monkeypatch.setattr(main, "_match_library", lambda item, a, b: "w1" if item["title"] == "Dune" else None)
    owned = member.get("/api/request/book/search", params={"q": "dune"}).json()["results"][0]
    assert owned["status"] == "available" and owned["in_library"] == "w1"
