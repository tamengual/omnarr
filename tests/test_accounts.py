"""Accounts: migration from the single password, roles, per-person private section and identity."""
import importlib
import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import accounts, identity


def _load(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    return main


def test_old_single_password_becomes_admin_and_sessions_survive(tmp_path, monkeypatch):
    # an install from before accounts existed: one password, one PIN, one signed-in browser
    db = sqlite3.connect(tmp_path / "state.db")
    db.executescript("""
      CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE sessions (token TEXT PRIMARY KEY, created REAL, expires REAL, adult_until REAL);
      CREATE TABLE connections (app TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, settings TEXT, updated REAL);
      CREATE TABLE actions (ts REAL, action TEXT, payload TEXT, result TEXT);
      CREATE TABLE wanted_books (id INTEGER PRIMARY KEY, work_id TEXT, title TEXT, author TEXT, format TEXT,
        provider TEXT, book_id TEXT, status TEXT, attempts INTEGER DEFAULT 0, tried TEXT DEFAULT '[]',
        current TEXT, current_title TEXT, created REAL, last_search REAL, next_search REAL, done_at REAL, note TEXT);
      INSERT INTO wanted_books (title, format, status) VALUES ('Old request', 'ebook', 'searching');""")
    salt = "00" * 16
    db.execute("INSERT INTO settings VALUES ('password_salt', ?)", (salt,))
    db.execute("INSERT INTO settings VALUES ('password_hash', ?)", (accounts.hash_secret("old-password", salt),))
    db.execute("INSERT INTO settings VALUES ('adult_pin_salt', ?)", (salt,))
    db.execute("INSERT INTO settings VALUES ('adult_pin_hash', ?)", (accounts.hash_secret("2468", salt),))
    db.execute("INSERT INTO sessions VALUES ('old-cookie', ?, ?, 0)", (time.time(), time.time() + 86400))
    db.execute("INSERT INTO connections VALUES ('jellyfin', 1, ?, 0)", (json.dumps({"url": "http://jf", "api_key": "k", "user": "travis"}),))
    db.commit()
    db.close()
    main = _load(tmp_path, monkeypatch)
    c = TestClient(main.app)
    c.cookies.set(main.COOKIE, "old-cookie")
    st = c.get("/api/auth/status").json()
    assert st["logged_in"] and st["role"] == "admin" and st["user"]["username"] == "admin"
    assert st["user"]["jellyfin_user"] == "travis" and st["user"]["pin_set"]
    fresh = TestClient(main.app)
    assert fresh.post("/api/auth/login", json={"password": "old-password"}).status_code == 200       # old client
    assert TestClient(main.app).post("/api/auth/login", json={"username": "admin", "password": "old-password"}).status_code == 200
    # the pre-existing audit log and wanted list keep working after the upgrade
    main.live._audit(main.STATE, "test.after_upgrade", {"x": 1}, "ok")
    con = sqlite3.connect(tmp_path / "state.db")
    assert con.execute("SELECT count(*) FROM actions WHERE action='test.after_upgrade'").fetchone()[0] == 1
    con.close()
    items = c.get("/api/wanted").json()["items"]
    assert items[0]["title"] == "Old request" and items[0]["requested_by"] is None
    assert c.post("/api/wanted", json={"title": "New one", "format": "ebook"}).status_code == 200
    assert any(i["title"] == "New one" and i["requested_by"] == "admin" for i in c.get("/api/wanted").json()["items"])


@pytest.fixture()
def app2(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    admin = TestClient(main.app)
    assert admin.post("/api/auth/setup", json={"username": "travis", "password": "admin-pass-1"}).status_code == 200
    r = admin.post("/api/accounts", json={"username": "sam", "password": "member-pass-1", "role": "member"})
    assert r.status_code == 200
    member = TestClient(main.app)
    assert member.post("/api/auth/login", json={"username": "sam", "password": "member-pass-1"}).status_code == 200
    return main, admin, member, r.json()["account"]["id"]


def test_member_cannot_do_admin_things(app2):
    main, admin, member, sam = app2
    for method, path in (("get", "/api/setup/apps"), ("post", "/api/reindex"), ("get", "/api/accounts"),
                         ("post", "/api/override"), ("delete", f"/api/accounts/{sam}")):
        assert getattr(member, method)(path).status_code == 403, path
    assert member.get("/api/auth/status").json()["role"] == "member"
    assert member.get("/api/me").json()["username"] == "sam"
    assert admin.get("/api/accounts").status_code == 200


def test_last_admin_is_protected(app2):
    main, admin, member, sam = app2
    me = admin.get("/api/me").json()["id"]
    assert admin.patch(f"/api/accounts/{me}", json={"role": "member"}).status_code == 400
    assert admin.delete(f"/api/accounts/{me}").status_code == 400
    assert admin.patch(f"/api/accounts/{sam}", json={"role": "admin"}).status_code == 200
    assert admin.patch(f"/api/accounts/{me}", json={"role": "member"}).status_code == 200      # now two admins


def test_private_section_is_per_person(app2):
    main, admin, member, sam = app2
    admin.post("/api/setup/options", json={"adult_enabled": True})
    assert admin.post("/api/adult/setup", json={"pin": "1357"}).status_code == 200
    assert admin.get("/api/adult/status").json()["unlocked"]
    assert member.post("/api/adult/setup", json={"pin": "1111"}).status_code == 403             # not allowed yet
    admin.patch(f"/api/accounts/{sam}", json={"adult_allowed": True})
    assert member.post("/api/adult/setup", json={"pin": "1111"}).status_code == 200              # own PIN
    assert member.post("/api/adult/unlock", json={"pin": "1357"}).status_code == 401             # not the admin's


def test_password_reset_signs_member_out(app2):
    main, admin, member, sam = app2
    assert admin.patch(f"/api/accounts/{sam}", json={"password": "new-member-pass"}).status_code == 200
    assert member.get("/api/me").status_code == 401
    assert TestClient(main.app).post("/api/auth/login", json={"username": "sam", "password": "new-member-pass"}).status_code == 200


def test_guest_cannot_trigger_downloads(app2):
    main, admin, member, sam = app2
    admin.patch(f"/api/accounts/{sam}", json={"can_request": False, "can_download": True})
    for path, body in (("/api/request/screen", {"kind": "movie", "tmdb": "1"}), ("/api/wanted", {"title": "x", "format": "ebook"}),
                       ("/api/action", {"action": "search_season"})):
        r = member.post(path, json=body)
        assert r.status_code == 403 and "not request" in r.json()["detail"], path
    perms = member.get("/api/auth/status").json()["permissions"]
    assert perms == {"can_request": False, "can_download": True, "can_upload": False, "adult_allowed": False}
    assert admin.get("/api/auth/status").json()["permissions"]["can_request"] is True


def test_invite_link_creates_account_once(app2, monkeypatch):
    main, admin, member, sam = app2
    r = admin.post("/api/accounts/invites", json={"link_base": "https://media.example/omnarr/", "can_request": False,
                                                   "can_download": True, "note": "Alex"})
    assert r.status_code == 200
    url = r.json()["url"]
    token = url.split("#invite=")[1]
    assert url.startswith("https://media.example/omnarr/#invite=")
    guest = TestClient(main.app)
    assert guest.get(f"/api/auth/invite/{token}").json()["invited_by"] == "travis"
    assert guest.post(f"/api/auth/invite/{token}", json={"username": "alex", "password": "alex-pass-1"}).status_code == 200
    st = guest.get("/api/auth/status").json()
    assert st["user"]["username"] == "alex" and st["role"] == "member"
    assert st["permissions"]["can_request"] is False and st["permissions"]["can_download"] is True
    again = TestClient(main.app).post(f"/api/auth/invite/{token}", json={"username": "eve", "password": "eve-pass-12"})
    assert again.status_code == 400                                                           # single use
    assert member.post("/api/accounts/invites", json={"link_base": "https://x/"}).status_code == 403
    assert [i["status"] for i in admin.get("/api/accounts/invites").json()["invites"]] == ["used"]


def test_public_address_and_https_hardening(app2):
    main, admin, member, sam = app2
    assert admin.post("/api/setup/options", json={"public_url": "ftp://nope"}).status_code == 400
    assert admin.post("/api/setup/options", json={"public_url": "https://media.example.com"}).status_code == 200
    r = admin.post("/api/accounts/invites", json={"link_base": "http://192.168.1.5:8765/"})
    assert r.json()["url"].startswith("https://media.example.com/#invite=")          # the public address wins
    h = admin.get("/api/health").headers
    assert h["x-content-type-options"] == "nosniff" and "noindex" in h["x-robots-tag"]
    secure = TestClient(main.app, base_url="https://media.example.com")
    login = secure.post("/api/auth/login", json={"username": "sam", "password": "member-pass-1"})
    assert "secure" in login.headers["set-cookie"].lower()
    plain = TestClient(main.app).post("/api/auth/login", json={"username": "sam", "password": "member-pass-1"})
    assert "secure" not in plain.headers["set-cookie"].lower()


def test_invite_email(app2, monkeypatch):
    main, admin, member, sam = app2
    r = admin.post("/api/accounts/invites", json={"link_base": "https://m.example/", "email": "a@example.com"})
    assert r.json()["emailed"] is False and "Email isn't set up" in r.json()["message"]
    main.cfg.save_connection("email", {"host": "smtp.example", "port": "587"})
    sent = []
    from app import mailer
    monkeypatch.setattr(mailer, "send", lambda s, to, subj, body: sent.append((to, subj, body)))
    r = admin.post("/api/accounts/invites", json={"link_base": "https://m.example/", "email": "b@example.com"})
    assert r.json()["emailed"] is True and sent[0][0] == "b@example.com" and r.json()["url"] in sent[0][2]


def test_identity_reaches_endpoints(app2, monkeypatch):
    """The per-request identity must survive the middleware into threadpool endpoints:
    an unmapped member never gets the server's ABS key (no progress written as the owner)."""
    main, admin, member, sam = app2
    main.cfg.save_connection("abs", {"url": "http://abs.example", "api_key": "OWNER-KEY"})
    monkeypatch.setattr(main.play, "audio_info", lambda cfg, item_id: {"type": "audio"})
    monkeypatch.setattr(main, "_wanted_tick", lambda: None, raising=False)
    m, a = member.get("/api/play/audio/x").json(), admin.get("/api/play/audio/x").json()
    assert m["progress_sync"] is True and m["app_sync"] is False          # kept in Omnarr only
    assert a["progress_sync"] is True and a["app_sync"] is True
    seen = []
    monkeypatch.setattr(main.play, "audio_progress", lambda cfg, *a: seen.append(main.play.abs_token(cfg)) or True)
    member.post("/api/play/progress", json={"source": "abs", "item_id": "x", "position": 300, "duration": 1000})
    admin.post("/api/play/progress", json={"source": "abs", "item_id": "x", "position": 5, "duration": 10})
    assert seen == ["OWNER-KEY"]                                             # the member never writes as the owner
    assert member.get("/api/play/audio/x").json()["resume"] == 300          # …but resumes from Omnarr's record
    from app import playstate
    assert playstate.get(main.STATE, sam, "abs", "x") == (300, False)
    admin.patch(f"/api/accounts/{sam}", json={})
    member.post("/api/me", json={"abs_api_key": ""})              # nothing linked still means no key


def test_identity_rules():
    identity.set_for({"role": "admin", "jellyfin_user": None, "abs_api_key": None})
    assert identity.jellyfin_user.get() == identity.OWNER and identity.abs_key.get() == identity.OWNER
    identity.set_for({"role": "member", "jellyfin_user": None, "abs_api_key": None})
    assert identity.jellyfin_user.get() is None and identity.abs_key.get() is None             # never the owner's
    identity.set_for({"role": "member", "jellyfin_user": "sam", "abs_api_key": "k"})
    assert identity.jellyfin_user.get() == "sam" and identity.abs_key.get() == "k"


def test_ha_users_become_accounts(tmp_path, monkeypatch):
    main = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "HA_INGRESS", True)
    monkeypatch.setattr(main, "INGRESS_PROXY", "testclient")
    c = TestClient(main.app)
    first = c.get("/api/auth/status", headers={"X-Ingress-Path": "/x", "X-Remote-User-Id": "u1", "X-Remote-User-Display-Name": "Travis"}).json()
    second = c.get("/api/auth/status", headers={"X-Ingress-Path": "/x", "X-Remote-User-Id": "u2", "X-Remote-User-Display-Name": "Travis"}).json()
    assert first["role"] == "admin" and first["user"]["username"] == "Travis"
    assert second["role"] == "member" and second["user"]["username"] == "Travis-2"
