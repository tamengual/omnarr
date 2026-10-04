"""Plex: library + ids, direct play vs transcode, the proxy only serves media, progress rules."""
import importlib
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient


class FakePlex:
    """Enough of Plex's API for the connector and the player."""
    progress = []

    def __init__(self, s=None, timeout=60):
        pass

    def __enter__(self): return self
    def __exit__(self, *a): pass

    def get(self, path, params=None, headers=None):
        class R:
            def __init__(r, data, code=200): r._d, r.status_code = data, code
            def json(r): return r._d
            def raise_for_status(r): pass
        mc = lambda **k: R({"MediaContainer": k})
        if path == "/identity":
            return mc(machineIdentifier="m1")
        if path == "/":
            return mc(friendlyName="Test Plex")
        if path == "/library/sections":
            return mc(Directory=[{"key": "1", "type": "movie", "title": "Movies"}, {"key": "2", "type": "show", "title": "TV"},
                                 {"key": "3", "type": "artist", "title": "Music"}])
        if path == "/library/sections/1/all":
            return mc(totalSize=1, Metadata=[{"ratingKey": 10, "title": "Big Buck Bunny", "year": 2008, "thumb": "/t/10",
                                              "duration": 90000, "viewOffset": 45000, "addedAt": 1791000000,
                                              "Guid": [{"id": "tmdb://10378"}, {"id": "imdb://tt1254207"}], "Genre": [{"tag": "Animation"}]}])
        if path == "/library/sections/2/all":
            return mc(totalSize=1, Metadata=[{"ratingKey": 20, "title": "Silo", "year": 2023, "leafCount": 3, "viewedLeafCount": 3,
                                              "Guid": [{"id": "tmdb://125988"}]}])
        if path.startswith("/library/metadata/"):
            rk = path.rsplit("/", 1)[1]
            media = {"10": {"container": "mp4", "videoCodec": "h264", "audioCodec": "aac", "Part": [{"key": "/library/parts/5/1/file.mp4"}]},
                     "11": {"container": "mkv", "videoCodec": "hevc", "audioCodec": "ac3", "Part": [{"key": "/library/parts/6/1/file.mkv"}]}}[rk]
            return mc(Metadata=[{"ratingKey": rk, "title": "X", "type": "movie", "duration": 90000, "viewOffset": 30000, "Media": [media]}])
        return R({}, 404)

    def post(self, path, params=None):
        FakePlex.progress.append(("timeline", params))
        return type("R", (), {"status_code": 200})()

    def put(self, path, params=None):
        FakePlex.progress.append(("scrobble", params))
        return type("R", (), {"status_code": 200})()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    from app.connectors import plex
    monkeypatch.setattr(plex, "client", FakePlex)
    FakePlex.progress.clear()
    main.cfg.save_connection("plex", {"url": "http://plex:32400", "api_key": "tok", "browser_url": "http://plex.example"})
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "guest", "password": "guest-pass-1", "can_request": False})
    guest = TestClient(main.app)
    guest.post("/api/auth/login", json={"username": "guest", "password": "guest-pass-1"})
    return main, admin, guest, tmp_path


def test_library_ids_and_watched(env):
    main, admin, guest, tmp = env
    from app.connectors import plex
    units = {u.title: u for u in plex.read(main.cfg)}
    m, s = units["Big Buck Bunny"], units["Silo"]
    assert m.kind == "movie" and m.ids == {"tmdb": "10378", "imdb": "tt1254207"} and m.progress == 0.5 and m.genres == ["Animation"]
    assert m.url == "http://plex.example/web/index.html#!/server/m1/details?key=%2Flibrary%2Fmetadata%2F10"
    assert s.kind == "show" and s.finished and s.extra["episodes"] == 3
    assert "Music" not in {u.library for u in units.values()}
    assert plex.test(main.cfg.source("plex")) == (True, "Connected to Test Plex: 2 movie/TV libraries")


def test_direct_play_or_transcode(env):
    main, admin, guest, tmp = env
    d = admin.get("/api/play/video/plex:10").json()
    assert d["mode"] == "direct" and d["url"] == "api/stream/plex/library/parts/5/1/file.mp4" and d["source"] == "plex"
    assert d["resume"] == 30 and d["app_sync"] is True                      # admin: Plex's own place
    h = admin.get("/api/play/video/plex:11").json()
    assert h["mode"] == "hls" and h["url"].startswith("api/stream/plex/video/:/transcode/universal/start.m3u8?")
    assert "copyts=0" in h["url"] and "tok" not in h["url"]                 # never the token
    g = guest.get("/api/play/video/plex:10").json()
    assert g["resume"] == 0 and g["app_sync"] is False                      # a guest gets their own place, not the owner's


def test_proxy_only_serves_media(env):
    main, admin, guest, tmp = env
    assert guest.get("/api/stream/plex/library/sections").status_code == 404
    assert guest.get("/api/stream/plex/:/prefs").status_code == 404


def test_progress_goes_to_plex_for_admins_only(env):
    main, admin, guest, tmp = env
    guest.post("/api/play/progress", json={"source": "plex", "item_id": "plex:10", "position": 20, "duration": 90})
    assert FakePlex.progress == []
    admin.post("/api/play/progress", json={"source": "plex", "item_id": "plex:10", "position": 40, "duration": 90})
    admin.post("/api/play/progress", json={"source": "plex", "item_id": "plex:10", "position": 89, "duration": 90, "finished": True})
    assert [p[0] for p in FakePlex.progress] == ["timeline", "scrobble"]
    assert FakePlex.progress[0][1]["time"] == 40000
    con = sqlite3.connect(tmp / "state.db")
    assert con.execute("SELECT account_id, position FROM play_progress WHERE source='plex' AND item_id='10' ORDER BY account_id").fetchall() == [(1, 89.0), (2, 20.0)]


def test_web_app_address_is_accepted():
    from app.connectors import plex
    for url in ("http://plex:32400/web", "http://plex:32400/web/", "http://plex:32400/web/index.html#!/settings", "http://plex:32400"):
        assert plex.base({"url": url}) == "http://plex:32400"
