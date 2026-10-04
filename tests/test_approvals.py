"""Request approvals ("can ask"), notifications, and headers on every response."""
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
    monkeypatch.setattr(main, "_wanted_tick", lambda: None)
    sent = []
    monkeypatch.setattr(main.notify, "send", lambda cfg, sp, event, title, msg, to=(), to_admins=False, url="":
                        sent.append({"event": event, "title": title, "to": list(to), "admins": to_admins}))
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "kid", "password": "kid-pass-12", "can_request": False, "can_ask": True})
    admin.post("/api/accounts", json={"username": "guest", "password": "guest-pass-1", "can_request": False})
    kid, guest = TestClient(main.app), TestClient(main.app)
    kid.post("/api/auth/login", json={"username": "kid", "password": "kid-pass-12"})
    guest.post("/api/auth/login", json={"username": "guest", "password": "guest-pass-1"})
    return main, admin, kid, guest, sent


def test_ask_goes_to_queue_and_approval_runs_it(env, monkeypatch):
    main, admin, kid, guest, sent = env
    calls = []
    monkeypatch.setattr(main.requests_, "seerr_request", lambda cfg, kind, tmdb: calls.append((kind, tmdb)) or {"ok": True})
    r = kid.post("/api/request/screen", json={"kind": "movie", "tmdb": "693134", "title": "Dune: Part Two"}).json()
    assert r["queued"] and calls == []                                       # nothing downloaded yet
    assert sent[-1]["event"] == "approval_needed" and sent[-1]["admins"]
    assert guest.post("/api/request/screen", json={"kind": "movie", "tmdb": "1"}).status_code == 403   # guests can't ask
    assert kid.post("/api/action", json={"action": "search_season"}).status_code == 403              # not queueable
    mine = kid.get("/api/requests").json()
    assert mine["requests"][0]["title"] == "Dune: Part Two" and mine["requests"][0]["status"] == "pending"
    assert kid.post(f"/api/requests/{r['id']}/approve").status_code == 403    # can't approve your own
    done = admin.post(f"/api/requests/{r['id']}/approve").json()
    assert done["status"] == "approved" and calls == [("movie", "693134")]
    assert sent[-1]["event"] == "request_decided" and sent[-1]["to"]
    assert admin.post(f"/api/requests/{r['id']}/approve").status_code == 404  # only once


def test_deny_and_withdraw(env):
    main, admin, kid, guest, sent = env
    a = kid.post("/api/wanted", json={"title": "The Rise of Kyoshi", "author": "F. C. Yee", "format": "ebook"}).json()
    b = kid.post("/api/request/game", json={"game": "Avatar", "platform": "gba"}).json()
    assert a["queued"] and b["queued"]
    assert admin.post(f"/api/requests/{a['id']}/deny", json={"note": "already have it"}).json()["status"] == "denied"
    assert kid.delete(f"/api/requests/{b['id']}").status_code == 200
    assert not [i for i in admin.get("/api/wanted").json()["items"] if i["title"] == "The Rise of Kyoshi"]
    statuses = {x["title"]: x["status"] for x in admin.get("/api/requests").json()["requests"]}
    assert statuses == {"The Rise of Kyoshi (ebook)": "denied"}


def test_direct_requesters_skip_the_queue(env, monkeypatch):
    main, admin, kid, guest, sent = env
    monkeypatch.setattr(main.requests_, "seerr_request", lambda cfg, kind, tmdb: {"ok": True, "direct": True})
    assert admin.post("/api/request/screen", json={"kind": "tv", "tmdb": "246"}).json() == {"ok": True, "direct": True}


def test_headers_on_every_reply_and_email_prefs(env):
    main, admin, kid, guest, sent = env
    r = TestClient(main.app).get("/api/search")
    assert r.status_code == 401 and r.headers["x-content-type-options"] == "nosniff"
    assert kid.post("/api/me", json={"email": "not-an-email"}).status_code == 400
    me = kid.post("/api/me", json={"email": "kid@example.com", "notify_email": False}).json()["account"]
    assert me["email"] == "kid@example.com" and me["notify_email"] is False


def test_webhook_formats(monkeypatch):
    from app import notify
    posts = []

    class C:
        def __init__(self, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, **k):
            posts.append((url, k))
            class R:
                def raise_for_status(self): pass
            return R()
    monkeypatch.setattr(notify.httpx, "Client", C)
    notify._webhook({"url": "https://ntfy.sh/t", "format": "ntfy"}, "request_ready", "Ready", "Dune is ready", "")
    notify._webhook({"url": "https://discord/x", "format": "discord"}, "request_ready", "Ready", "Dune is ready", "")
    notify._webhook({"url": "http://ha/api/webhook/x"}, "request_ready", "Ready", "Dune is ready", "")
    assert posts[0][1]["headers"]["Title"] == "Ready" and posts[0][1]["content"] == b"Dune is ready"
    assert "Dune is ready" in posts[1][1]["json"]["content"]
    assert posts[2][1]["json"] == {"event": "request_ready", "title": "Ready", "message": "Dune is ready", "url": ""}
