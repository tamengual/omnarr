"""Set up my apps: people make their own logins in the server's apps, linked to their Omnarr account."""
import importlib
import json
import sqlite3
import sys
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

SOURCES = {
    "jellyfin": {"url": "http://jf", "api_key": "k", "user": "owner", "adult_libraries": ["Private"]},
    "abs": {"url": "http://abs", "api_key": "k"},
    "komga": {"url": "http://komga", "api_key": "k"},
    "romm": {"url": "http://romm", "api_key": "k"},
    "storyteller": {"db": "/x", "url": "http://st", "admin_user": "admin", "admin_password": "pw"},
    "calibre": {"db": "/x", "library": "/x", "browser_url": "https://books.example:10000", "web_url": "http://cwa",
                "admin_user": "admin", "admin_password": "pw"},
    "bookbridge": {"db": "", "sync_url": "http://bb", "admin_user": "admin", "admin_password": "pw"},
}


class Apps:
    """Just enough of each app's API / admin pages to create and delete users."""
    def __init__(self, bb_db):
        self.calls, self.bb_db, self.romm_forbidden = [], bb_db, False

    def __call__(self, req):
        host, path, m = req.url.host, req.url.path, req.method
        body = json.loads(req.content) if req.headers.get("content-type", "").startswith("application/json") and req.content else {}
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()} if b"=" in (req.content or b"") and not body else {}
        self.calls.append((host, m, path, body or form))
        if host == "jf":
            if path == "/Users" and m == "GET":
                return httpx.Response(200, json=[{"Name": "owner"}])
            if path == "/Users/New":
                return httpx.Response(200, json={"Id": "jf1", "Policy": {"EnableAllFolders": True}})
            if path == "/Library/MediaFolders":
                return httpx.Response(200, json={"Items": [{"Id": "m", "Name": "Movies"}, {"Id": "p", "Name": "Private"}]})
            return httpx.Response(204)
        if host == "abs":
            if path == "/api/users" and m == "POST":
                return httpx.Response(200, json={"user": {"id": "abs1"}})
            if path == "/api/api-keys":
                return httpx.Response(200, json={"apiKey": {"id": "x", "apiKey": "abs-key-for-kim"}})
            return httpx.Response(200, json={})
        if host == "komga":
            return httpx.Response(200, json={"id": "ko1"}) if m == "POST" else httpx.Response(204)
        if host == "romm":
            if self.romm_forbidden:
                return httpx.Response(403, json={"detail": "Forbidden"})
            return httpx.Response(201, json={"id": 7}) if m == "POST" else httpx.Response(200, json={})
        if host == "st":
            if path == "/api/v2/token":
                return httpx.Response(200, json={"access_token": "t"})
            if path == "/api/v2/invites":
                return httpx.Response(200, json={"email": body["email"], "key": "inv"})
            if path == "/api/v2/users" and m == "POST":
                return httpx.Response(200, json={"access_token": "u"})
            if path == "/api/v2/users":
                return httpx.Response(200, json=[{"id": "st1", "username": "kim"}])
            return httpx.Response(204)
        if host == "cwa":
            page = '<form><input type="hidden" name="csrf_token" value="tok123"><input type="checkbox" name="show_2" checked></form>'
            if path == "/login" and m == "POST":
                return httpx.Response(302, headers={"location": "/"})
            if path == "/ajax/listusers":
                return httpx.Response(200, json={"rows": [{"id": 3, "name": "kim"}]})
            if path.startswith("/kobo_auth/generate_auth_token/"):
                return httpx.Response(200, text="api_endpoint=http://cwa/kobo/abcdef123456")
            return httpx.Response(200, text=page)
        if host == "bb":
            if path == "/login" and m == "POST":
                return httpx.Response(302, headers={"location": "/"})
            if path == "/admin/users" and m == "POST" and form.get("action") == "create":
                con = sqlite3.connect(self.bb_db)
                con.execute("INSERT INTO users (username) VALUES (?)", (form["username"],))
                con.commit()
                con.close()
            return httpx.Response(200, text='<script>(function(){\n  var t = "bbtoken1234567890abc";\n  function addField(form){}})();</script>')
        return httpx.Response(404)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    bb_db = tmp_path / "bb.db"
    con = sqlite3.connect(bb_db)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    con.execute("INSERT INTO users (username) VALUES ('owner')")
    con.commit()
    con.close()
    sources = {k: dict(v) for k, v in SOURCES.items()}
    sources["bookbridge"]["db"] = str(bb_db)
    monkeypatch.setattr(main.cfg, "source", lambda key: sources.get(key))
    apps = Apps(str(bb_db))
    real = httpx.Client
    monkeypatch.setattr(main.provision.httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(apps), **k))
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    token = admin.post("/api/accounts/invites", json={"link_base": "https://media.example/"}).json()["url"].split("#invite=")[1]
    member = TestClient(main.app)
    member.post(f"/api/auth/invite/{token}", json={"username": "kim", "password": "member-pass-1"})
    return main, admin, member, apps


ALL = ["jellyfin", "abs", "komga", "calibre_web", "storyteller", "romm", "bookbridge"]


def test_makes_every_login_and_links_them(env):
    main, admin, member, apps = env
    listed = member.get("/api/my-apps").json()
    assert listed["enabled"] and [a["key"] for a in listed["apps"]] == ALL and not any(a["has"] for a in listed["apps"])
    r = member.post("/api/my-apps", json={"apps": ALL, "password": "kims-apps-pw"})
    assert r.status_code == 200 and all(x["ok"] for x in r.json()["results"]), r.json()

    me = member.get("/api/me").json()
    assert me["jellyfin_user"] == "kim" and me["kosync_user"] == "kim" and me["abs_api_key"]
    after = {a["key"]: a for a in member.get("/api/my-apps").json()["apps"]}
    assert all(a["has"] for a in after.values())
    assert after["calibre_web"]["kobo_endpoint"] == "https://books.example:10000/kobo/abcdef123456"
    assert after["komga"]["login"] == "kim@omnarr.local"

    calls = {(h, p): b for h, m, p, b in apps.calls if m == "POST"}
    policy = calls[("jf", "/Users/jf1/Policy")]
    assert policy["EnableAllFolders"] is False and policy["EnabledFolders"] == ["m"] and not policy["IsAdministrator"]
    assert calls[("abs", "/api/users")]["permissions"]["accessExplicitContent"] is False
    assert calls[("cwa", "/admin/user/new")]["download_role"] == "on" and calls[("cwa", "/admin/user/new")]["show_2"] == "on"
    bb = calls[("bb", "/admin/users/2/integrations")]
    assert bb["ABS_KEY"] == "abs-key-for-kim" and bb["CWA_SYNC_TOKEN"] == "abcdef123456" and bb["KOSYNC_USER"] == "kim"
    assert bb["STORYTELLER_USER"] == "kim" and bb["csrf_token"] == "bbtoken1234567890abc"
    assert bb["KOSYNC_KEY"] != "kims-apps-pw"
    assert b"kims-apps-pw" not in open(main.STATE, "rb").read()      # the password the person picked is never stored

    again = member.post("/api/my-apps", json={"apps": ALL, "password": "kims-apps-pw"}).json()
    assert again["results"] == []                 # nothing made twice


def test_one_app_failing_doesnt_stop_the_rest(env):
    main, admin, member, apps = env
    apps.romm_forbidden = True
    res = {x["app"]: x for x in member.post("/api/my-apps", json={"apps": ["abs", "romm"], "password": "kims-apps-pw"}).json()["results"]}
    assert res["abs"]["ok"] and not res["romm"]["ok"] and "users.write" in res["romm"]["message"]


def test_rules(env):
    main, admin, member, apps = env
    assert member.post("/api/my-apps", json={"apps": ["abs"], "password": "short"}).status_code == 400
    assert member.post("/api/my-apps", json={"apps": [], "password": "kims-apps-pw"}).status_code == 400
    assert member.post("/api/my-apps", json={"apps": ["stash"], "password": "kims-apps-pw"}).status_code == 400
    assert admin.post("/api/setup/devices", json={"app_signup": False}).json()["settings"]["app_signup"] is False
    assert admin.get("/api/setup/devices").json()["app_signup"] is False
    assert member.post("/api/my-apps", json={"apps": ["abs"], "password": "kims-apps-pw"}).status_code == 403
    assert not any(a["available"] for a in member.get("/api/my-apps").json()["apps"])


def test_deleting_the_account_removes_its_logins(env):
    main, admin, member, apps = env
    member.post("/api/my-apps", json={"apps": ["jellyfin", "abs", "komga", "storyteller", "romm", "bookbridge"], "password": "kims-apps-pw"})
    kim = next(a["id"] for a in admin.get("/api/accounts").json()["accounts"] if a["username"] == "kim")
    r = admin.delete(f"/api/accounts/{kim}").json()
    assert "app logins were removed" in r["message"] and "by hand" not in r["message"]
    deletes = {(h, p) for h, m, p, b in apps.calls if m == "DELETE"}
    assert {("jf", "/Users/jf1"), ("abs", "/api/users/abs1"), ("komga", "/api/v2/users/ko1"), ("romm", "/api/users/7"),
            ("st", "/api/v2/users/st1")} <= deletes
    assert any(b.get("action") == "delete" for h, m, p, b in apps.calls if h == "bb" and m == "POST")
    assert main.provision.mine(str(main.STATE), kim) == {}
