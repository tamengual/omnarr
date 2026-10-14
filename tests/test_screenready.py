"""Movie/TV requests: the person who asked hears once it's watchable (in Jellyfin/Plex), not before."""
import importlib
import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from app import screenready


def make_index(path, works=()):
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE works (id TEXT PRIMARY KEY, kind TEXT, title TEXT, sources TEXT, info TEXT);
                         CREATE TABLE editions (work_id TEXT, source TEXT, extra TEXT);""")
    for w in works:
        add_work(con, *w)
    con.commit()
    con.close()


def add_work(con, wid, kind, title, sources, tmdb, availability="in_library"):
    con.execute("INSERT OR REPLACE INTO works VALUES (?,?,?,?,?)",
                (wid, kind, title, json.dumps(sources), json.dumps({"availability": availability})))
    for s in sources:
        con.execute("INSERT INTO editions VALUES (?,?,?)", (wid, s, json.dumps({"ids": {"tmdb": tmdb}})))


@pytest.fixture()
def sent(monkeypatch):
    out = []
    from app import notify
    monkeypatch.setattr(notify, "send", lambda cfg, sp, event, title, msg, to=(), to_admins=False, url="":
                        out.append({"event": event, "title": title, "msg": msg, "to": list(to), "url": url}))
    return out


def test_ready_only_once_watchable_and_only_once(tmp_path, sent):
    state, index = str(tmp_path / "state.db"), str(tmp_path / "index.db")
    make_index(index)
    screenready.record(state, 7, "movie", 603, "The Matrix")
    screenready.record(state, 7, "movie", "603", "The Matrix")           # asking twice keeps one
    assert len(screenready.mine(state, 7)) == 1
    assert screenready.tick(None, state, index) == [] and sent == []     # nothing anywhere yet
    con = sqlite3.connect(index)
    add_work(con, "w1", "movie", "The Matrix", ["radarr"], "603")
    con.commit()
    assert screenready.tick(None, state, index) == [] and sent == []     # Radarr has it, but it isn't playable yet
    con.execute("DELETE FROM editions")
    add_work(con, "w1", "movie", "The Matrix", ["radarr", "jellyfin"], "603")
    con.commit()
    con.close()
    assert screenready.tick(None, state, index, "https://media.example.com/") == ["The Matrix"]
    assert sent == [{"event": "request_ready", "title": "Ready to watch: The Matrix",
                     "msg": "The Matrix is ready. It's in your library and can be played now.", "to": [7],
                     "url": "https://media.example.com/#work=w1"}]
    assert screenready.tick(None, state, index) == [] and len(sent) == 1   # told once
    row = screenready.mine(state, 7)[0]
    assert row["status"] == "ready" and row["work_id"] == "w1"
    assert screenready.mine(state, 8) == []                               # other people don't see it


def test_shows_say_when_only_some_episodes_are_in(tmp_path, sent):
    state, index = str(tmp_path / "state.db"), str(tmp_path / "index.db")
    make_index(index, [("s1", "show", "Severance", ["sonarr", "plex"], "95396", "partial"),
                       ("m1", "movie", "Severance (movie with the same id)", ["jellyfin"], "95396")])
    screenready.record(state, 3, "tv", 95396, "Severance")
    assert screenready.tick(None, state, index) == ["Severance"]           # the show, not the movie
    assert "first episodes are in your library" in sent[0]["msg"].lower() and sent[0]["url"] == ""
    assert screenready.mine(state, 3)[0]["partial"] == 1


def test_gives_up_after_a_long_wait(tmp_path, sent):
    state, index = str(tmp_path / "state.db"), str(tmp_path / "index.db")
    make_index(index)
    screenready.record(state, 1, "movie", 1, "Never Released")
    con = sqlite3.connect(state)
    con.execute("UPDATE screen_requests SET created=?", (time.time() - (screenready.GIVE_UP_DAYS + 1) * 86400,))
    con.commit()
    con.close()
    screenready.tick(None, state, index)
    assert screenready.mine(state, 1)[0]["status"] == "expired" and sent == []


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    monkeypatch.setattr(main, "_wanted_tick", lambda: None)
    monkeypatch.setattr(main.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(main.requests_, "seerr_request", lambda cfg, kind, tmdb: {"ok": True})
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "kid", "password": "kid-pass-12", "can_request": False, "can_ask": True})
    admin.post("/api/accounts", json={"username": "sam", "password": "sam-pass-123", "can_request": True})
    kid, sam = TestClient(main.app), TestClient(main.app)
    kid.post("/api/auth/login", json={"username": "kid", "password": "kid-pass-12"})
    sam.post("/api/auth/login", json={"username": "sam", "password": "sam-pass-123"})
    return main, admin, kid, sam


def test_requests_remember_who_asked(env):
    main, admin, kid, sam = env
    assert sam.post("/api/request/screen", json={"kind": "movie", "tmdb": "603", "title": "The Matrix"}).json()["ok"]
    queued = kid.post("/api/request/screen", json={"kind": "tv", "tmdb": "95396", "title": "Severance"}).json()
    assert kid.get("/api/requests").json()["screens"] == []               # not asked for until it's approved
    admin.post(f"/api/requests/{queued['id']}/approve")
    assert [s["title"] for s in kid.get("/api/requests").json()["screens"]] == ["Severance"]
    assert [(s["title"], s["status"]) for s in sam.get("/api/requests").json()["screens"]] == [("The Matrix", "waiting")]
    assert {s["title"] for s in admin.get("/api/requests").json()["screens"]} == {"The Matrix", "Severance"}
