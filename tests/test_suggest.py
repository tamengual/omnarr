"""Suggestions: per-person, explainable, never adult, owner's screens count as watched."""
import importlib
import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

RECENT = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400 * 10))


def _w(wid, kind, title, authors=(), series="", idx=None, genres=(), adult=0, universe=""):
    return (wid, kind, title, title.lower(), json.dumps(list(authors)), "[]", series, idx, 2020, "", json.dumps(list(genres)),
            "[]", "", None, "2026-01-01", None, "unread", None, 0, adult, None, "[]", "[]", "[]", universe, None,
            series.lower() if series else "", "{}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    from app import indexer
    idx = sqlite3.connect(tmp_path / "index.db")
    idx.executescript(indexer.SCHEMA)
    cols = [r[1] for r in idx.execute("PRAGMA table_info(works)")]
    rows = [
        _w("wool", "book", "Wool", ["Hugh Howey"], "Silo", 1, ["Science Fiction"]),
        _w("shift", "book", "Shift", ["Hugh Howey"], "Silo", 2, ["Science Fiction"]),
        _w("dust", "book", "Dust", ["Hugh Howey"], "Silo", 3, ["Science Fiction"]),
        _w("sand", "book", "Sand", ["Hugh Howey"], "", None, ["Science Fiction"]),
        _w("dune", "book", "Dune", ["Frank Herbert"], "", None, ["Science Fiction"]),
        _w("cook", "book", "Weeknight Dinners", ["A Chef"], "", None, ["Cooking"]),
        _w("arrival", "movie", "Arrival", [], "", None, ["Science Fiction"]),
        _w("spicy", "book", "Spicy", ["Hugh Howey"], "", None, ["Science Fiction"], adult=1),
    ]
    idx.executemany(f"INSERT INTO works ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    idx.execute("INSERT INTO editions (work_id, unit_key, source, source_id, title, extra) VALUES ('wool','calibre:1','calibre','1','Wool', ?)",
                (json.dumps({"my_rating": 10}),))
    idx.execute("INSERT INTO editions (work_id, unit_key, source, source_id, title, extra) VALUES ('arrival','radarr:1','radarr','1','Arrival', ?)",
                (json.dumps({"ids": {"tmdb": 329865}}),))
    idx.execute("INSERT INTO user_progress VALUES (1, 'wool', 1.0, 'finished', ?)", (RECENT,))
    idx.commit(); idx.close()
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "guest", "password": "guest-pass-1", "can_request": False})
    guest = TestClient(main.app)
    guest.post("/api/auth/login", json={"username": "guest", "password": "guest-pass-1"})
    # outside lookups: TMDB recommendations for Arrival, and Wikidata related works
    monkeypatch.setattr(main.requests_, "seerr_recommendations", lambda cfg, st, kind, tmdb: [
        {"kind": "movie", "tmdb": 157336, "title": "Interstellar", "year": 2014, "poster": "p", "status": "not_requested"},
        {"kind": "movie", "tmdb": 329865, "title": "Arrival", "year": 2016, "poster": "p", "status": "available"}])
    monkeypatch.setattr(main, "_related_for_row", lambda row: [
        {"kind": "tv", "tmdb": 125988, "label": "Silo", "year": 2023, "authors": [], "url": "u", "wikidata": "Q1"}] if row["id"] == "wool" else [])
    main.cfg.save_connection("seerr", {"url": "http://seerr", "api_key": "k"})
    return main, admin, guest


def _sections(client, main, aid):
    main._suggest_online(aid, aid == 1)                  # run the background part in-line
    return {s["key"]: s["items"] for s in client.get("/api/suggestions").json()["sections"]}


def test_owner_suggestions_are_explained(env):
    main, admin, guest = env
    s = _sections(admin, main, 1)
    up = s["up_next"]
    assert up[0]["work"]["id"] == "shift" and up[0]["reason"] == "Next in Silo after Wool"
    lib = {c["work"]["id"]: c["reason"] for c in s["library"]}
    assert lib["sand"] == "Because you read Wool"                       # same author
    assert "dune" in lib and "cook" not in lib                          # genre yes, unrelated no
    assert "arrival" not in lib and "spicy" not in lib                  # seen screens, adult: never
    screen = s["screen"]
    assert [x["external"]["title"] for x in screen] == ["Interstellar"]   # owned Arrival filtered out
    assert screen[0]["reason"] == "Because of Arrival"
    assert [x["external"]["title"] for x in s["worlds"]] == ["Silo"] and s["worlds"][0]["reason"] == "From the world of Wool"


def test_guests_get_their_own_suggestions(env):
    main, admin, guest = env
    s = _sections(guest, main, 2)
    assert "screen" not in s and "up_next" not in s                     # no history, and the owner's screens aren't theirs
