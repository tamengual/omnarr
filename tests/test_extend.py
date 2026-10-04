"""Bring-your-own apps: custom library feeds, request webhooks, plug-ins, and ReadMeABook."""
import importlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    shutil.copy(ROOT / "docs" / "plugins" / "example_folder_library.py", plugins / "folder.py")
    (plugins / "broken.py").write_text("PLUGIN = {'key': 'broken'}\nraise RuntimeError('boom')\n")
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "Summer_Trip.2019.mp4").write_bytes(b"x")
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    monkeypatch.setenv("OMNARR_PLUGINS", str(plugins))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    from app import indexer
    indexer.run(main.cfg)                                # an (empty) library index, as on a real server
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    return main, admin, tmp_path


def test_plugins_load_and_broken_ones_are_skipped(env):
    main, admin, tmp = env
    from app.connectors import registry
    assert "plugin_folder_videos" in registry.BY_KEY and "plugin_broken" not in registry.BY_KEY
    spec = registry.BY_KEY["plugin_folder_videos"]
    assert spec["label"] == "Folder library (plug-in)" and spec["requests"] == ["movie"]
    ok, msg = spec["test"]({"folder": str(tmp / "videos")})
    assert ok and msg == "Found 1 video file"


def test_custom_library_and_plugin_items_are_indexed(env, monkeypatch):
    main, admin, tmp = env
    from app import extend, indexer
    main.cfg.save_connection("custom_library", {"url": "http://feed"})
    main.cfg.save_connection("plugin_folder_videos", {"folder": str(tmp / "videos")})
    monkeypatch.setattr(extend, "custom_items", lambda s: [
        {"id": "a1", "kind": "book", "title": "Wool", "authors": ["Hugh Howey"], "format": "audiobook", "cover_url": "http://c/1.jpg"},
        {"id": "a2", "kind": "book", "title": "After Dark", "adult": True, "cover_url": "http://c/2.jpg"},
        {"id": "", "title": "no id"}, {"id": "x"}])
    indexer.run(main.cfg)
    con = sqlite3.connect(tmp / "index.db")
    rows = {r[0]: r[1:] for r in con.execute("SELECT e.unit_key, w.title, w.adult, w.kind FROM editions e JOIN works w ON w.id=e.work_id")}
    assert rows["custom:a1"] == ("Wool", 0, "book") and rows["custom:a2"][1] == 1
    assert rows["plugin_folder_videos:Summer_Trip.2019.mp4"] == ("Summer Trip 2019", 0, "movie")
    assert len([k for k in rows if k.startswith("custom:")]) == 2          # malformed items skipped
    # covers: fetched server-side; an adult item's cover stays behind the PIN
    monkeypatch.setattr(extend, "cover_bytes", lambda cfg, source, url: b"IMG" if url else None)
    monkeypatch.setattr(main, "_thumbnail", lambda data: data)
    assert admin.get("/api/cover/custom:a1").content == b"IMG"
    assert admin.get("/api/cover/custom:a2").status_code == 404


def test_requests_route_to_webhook_and_plugins(env, monkeypatch):
    main, admin, tmp = env
    from app import extend
    sent = []
    monkeypatch.setattr(extend, "send_webhook", lambda cfg, item: (sent.append(("webhook", item)), {"ok": True, "message": "Queued in MyApp"})[1])
    main.cfg.save_connection("custom_requests", {"url": "http://hook", "formats": ["tv", "comic"]})
    log = tmp / "requests.jsonl"
    main.cfg.save_connection("plugin_folder_videos", {"folder": str(tmp / "videos"), "request_log": str(log)})
    assert extend.requester(main.cfg, "movie") == ("plugin", "plugin_folder_videos")
    assert extend.requester(main.cfg, "tv") == ("webhook", None)
    assert extend.requester(main.cfg, "ebook") == ("builtin", None)
    r = admin.post("/api/request/screen", json={"kind": "tv", "tmdb": 125988, "title": "Silo"})
    assert r.json() == {"ok": True, "message": "Queued in MyApp"} and sent[0][1]["ids"] == {"tmdb": "125988"}
    r = admin.post("/api/request/screen", json={"kind": "movie", "tmdb": 1, "title": "Arrival"})
    assert r.json()["message"] == "Logged a request for Arrival"
    assert json.loads(log.read_text().splitlines()[0])["title"] == "Arrival"
    # a comic goes on the keep-looking list and is handed to the webhook once
    from app import wanted
    wid = wanted.add(main.STATE, "Bone", "Jeff Smith", "comic")
    wanted.tick(main.cfg, main.STATE, main.INDEX)
    row = next(r for r in wanted.list_all(main.STATE) if r["id"] == wid)
    assert row["status"] == "downloading" and "sent to your request URL" in row["note"]
    wanted.tick(main.cfg, main.STATE, main.INDEX)
    assert len([s for s in sent if s[1]["title"] == "Bone"]) == 1            # not sent again


class FakeRMAB:
    """ReadMeABook's token API: search, create, status."""
    def __init__(self):
        self.created, self.state = [], "downloading"

    def __call__(self, s, timeout=30):
        return self

    def __enter__(self): return self
    def __exit__(self, *a): pass

    def get(self, path, params=None):
        class R:
            def __init__(r, code, data): r.status_code, r._d = code, data
            def json(r): return r._d
            def raise_for_status(r): pass
        if path == "/api/auth/me":
            return R(200, {"user": {"username": "admin"}})
        if path == "/api/audiobooks/search":
            return R(200, {"results": [
                {"asin": "B0STUDY", "title": "Wool: A Study Guide", "author": "Some Writer", "language": "english"},
                {"asin": "B00WOOL", "title": "Wool", "author": "Hugh Howey", "language": "english"},
                {"asin": "B00LANG", "title": "Wool", "author": "Hugh Howey", "language": "german"}]})
        if path.startswith("/api/requests/"):
            return R(200, {"request": {"id": path.rsplit("/", 1)[1], "status": self.state, "progress": 40, "errorMessage": "no seeders"}})
        return R(404, {})

    def post(self, path, json=None):
        class R:
            status_code = 201
            text = ""
            def json(r): return {"success": True, "request": {"id": "req-1", "status": "pending"}}
        self.created.append(json)
        return R()


def test_readmeabook_takes_audiobook_requests(env, monkeypatch):
    main, admin, tmp = env
    from app import extend, wanted
    from app.connectors import readmeabook
    fake = FakeRMAB()
    monkeypatch.setattr(readmeabook, "_client", fake)
    main.cfg.save_connection("readmeabook", {"url": "http://rmab:3030", "api_key": "rmab_x"})
    assert readmeabook.test({"url": "u", "api_key": "k"}) == (True, "Connected as admin")
    assert extend.requester(main.cfg, "audiobook") == ("readmeabook", None)
    assert extend.requester(main.cfg, "ebook") == ("builtin", None)
    wid = wanted.add(main.STATE, "Wool", "Hugh Howey", "audiobook")
    wanted.tick(main.cfg, main.STATE, main.INDEX)
    row = next(r for r in wanted.list_all(main.STATE) if r["id"] == wid)
    assert fake.created[0]["audiobook"]["asin"] == "B00WOOL"                 # the real book, in English
    assert row["status"] == "downloading" and row["current"] == "rmab:req-1"
    wanted.tick(main.cfg, main.STATE, main.INDEX)
    row = next(r for r in wanted.list_all(main.STATE) if r["id"] == wid)
    assert row["note"] == "ReadMeABook: downloading (40%)"
    fake.state = "failed"
    wanted.tick(main.cfg, main.STATE, main.INDEX)
    row = next(r for r in wanted.list_all(main.STATE) if r["id"] == wid)
    assert row["status"] == "searching" and "no seeders" in row["note"]
    # "no" keeps it connected but sends audiobooks back to Shelfmark
    main.cfg.save_connection("readmeabook", {"url": "http://rmab:3030", "api_key": "rmab_x", "audiobooks": "no"})
    assert extend.requester(main.cfg, "audiobook") == ("builtin", None)
