"""Stash scenes play in Omnarr's own player (Private, unlocked); adult comics read but never go offline."""
import importlib
import json
import sqlite3
import sys

import httpx
import pytest
from fastapi.testclient import TestClient


class StashFake:
    def __init__(self):
        self.mutations = []
        self.files = {"61": {"basename": "a.mp4", "duration": 600, "format": "mp4", "video_codec": "h264"},
                      "62": {"basename": "b.mkv", "duration": 900, "format": "mkv", "video_codec": "hevc"}}

    def __call__(self, req):
        body = json.loads(req.content)
        q, v = body["query"], body.get("variables") or {}
        if "findScene" in q:
            f = self.files.get(v["id"])
            return httpx.Response(200, json={"data": {"findScene": {"id": v["id"], "title": "Scene " + v["id"], "resume_time": 120,
                                                                    "files": [f], "paths": {}} if f else None}})
        self.mutations.append((next((m for m in ("sceneSaveActivity", "sceneAddPlay") if m in q), q[:20]), v))
        return httpx.Response(200, json={"data": {"ok": True}})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    real_source = main.cfg.source
    monkeypatch.setattr(main.cfg, "source", lambda k: {"url": "http://stash", "api_key": "k"} if k == "stash" else real_source(k))
    fake = StashFake()
    real = httpx.Client
    monkeypatch.setattr(main.stash.httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(fake), **k))
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/setup/options", json={"adult_enabled": True})
    admin.patch(f"/api/accounts/{admin.get('/api/me').json()['id']}", json={"adult_allowed": True})
    admin.post("/api/adult/setup", json={"pin": "2468"})
    admin.post("/api/adult/unlock", json={"pin": "2468"})
    return main, admin, fake, tmp_path


def test_plays_directly_or_through_stash_conversion(env):
    main, admin, fake, _ = env
    a = admin.get("/api/play/video/stash:61").json()
    assert a["mode"] == "direct" and a["url"] == "api/stream/stash/scene/61/stream" and a["source"] == "stash"
    assert a["resume"] == 120 and a["item_id"] == "61"                     # the owner picks up Stash's place
    b = admin.get("/api/play/video/stash:62").json()
    assert b["mode"] == "hls" and b["url"].endswith("/scene/62/stream.m3u8")
    assert admin.get("/api/play/video/stash:99").status_code == 404


def test_locked_private_means_no_playback(env):
    main, admin, fake, _ = env
    admin.post("/api/adult/lock")
    assert admin.get("/api/play/video/stash:61").status_code == 404
    assert admin.get("/api/stream/stash/scene/61/stream").status_code == 404
    assert admin.post("/api/play/progress", json={"source": "stash", "item_id": "61", "position": 30, "duration": 600}).status_code == 404


def test_only_scene_streams_are_proxied(env):
    main, admin, fake, _ = env
    for path in ("graphql", "scene/61/screenshot", "settings", "scene/abc/stream"):
        assert admin.get(f"/api/stream/stash/{path}").status_code == 404


def test_progress_saved_in_omnarr_and_stash(env):
    main, admin, fake, _ = env
    admin.get("/api/play/video/stash:61")
    assert admin.post("/api/play/progress", json={"source": "stash", "item_id": "61", "position": 300, "duration": 600}).json()["ok"]
    assert fake.mutations[-1] == ("sceneSaveActivity", {"id": "61", "t": 300})
    admin.post("/api/play/progress", json={"source": "stash", "item_id": "61", "position": 599, "duration": 600, "finished": True})
    assert fake.mutations[-1][0] == "sceneAddPlay"
    rows = main.playstate.all_rows(str(main.STATE))
    assert rows[0]["source"] == "stash" and rows[0]["label"] == "Scene 61"


def test_playlist_links_to_stash_go_through_omnarr():
    from app import play
    text = "#EXTM3U\nhttp://stash:9999/scene/62/stream.m3u8/0.ts?apikey=SECRET&resolution=ORIGINAL\n"
    out = play.rewrite_playlist(text.replace("http://stash:9999", ""), "", "/api/stream/stash")
    assert "/api/stream/stash/scene/62/stream.m3u8/0.ts?resolution=ORIGINAL" in out and "SECRET" not in out


def test_adult_comics_never_offline(env):
    main, admin, fake, tmp = env
    con = sqlite3.connect(tmp / "index.db")
    con.executescript("""CREATE TABLE works (id TEXT PRIMARY KEY, title TEXT, adult INTEGER);
                         CREATE TABLE editions (work_id TEXT, unit_key TEXT, source TEXT, source_id TEXT, title TEXT, extra TEXT);
                         INSERT INTO works VALUES ('w1', 'Spicy Comic', 1);
                         INSERT INTO editions VALUES ('w1', 'komga:b1', 'komga', 'b1', 'Spicy Comic', '{}');""")
    con.commit()
    con.close()
    row = main.search.connect(str(main.INDEX)).execute(
        "SELECT w.adult FROM editions e JOIN works w ON w.id=e.work_id WHERE e.unit_key='komga:b1'").fetchone()
    assert row["adult"] == 1
    src = open(main.__file__, encoding="utf-8").read()
    assert 'not e["adult"] and accounts.allowed(sess["account"], "can_download")' in src
