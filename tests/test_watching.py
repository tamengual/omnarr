"""Who's playing: admins see what people play and read in Omnarr; members never do."""
import importlib
import sqlite3
import sys
import time

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
    monkeypatch.setattr(main.live, "activity", lambda cfg: {})
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    token = admin.post("/api/accounts/invites", json={"link_base": "https://media.example/"}).json()["url"].split("#invite=")[1]
    member = TestClient(main.app)
    member.post(f"/api/auth/invite/{token}", json={"username": "kim", "password": "member-pass-1"})
    ids = {a["username"]: a["id"] for a in admin.get("/api/accounts").json()["accounts"]}
    con = sqlite3.connect(tmp_path / "index.db")
    con.executescript("""CREATE TABLE works (id TEXT PRIMARY KEY, kind TEXT, title TEXT, cover TEXT);
                         CREATE TABLE editions (work_id TEXT, source TEXT, source_id TEXT, format TEXT);
                         INSERT INTO works VALUES ('w-show', 'show', 'The Bear', 'jellyfin:series1'),
                                                  ('w-book', 'book', 'Dune', 'calibre:7');
                         INSERT INTO editions VALUES ('w-show', 'jellyfin', 'series1', 'video'),
                                                     ('w-book', 'abs', 'li-9', 'audiobook'),
                                                     ('w-book', 'calibre', '7', 'ebook');""")
    con.commit()
    con.close()
    return main, admin, member, ids


def test_admin_sees_now_and_history(env):
    main, admin, member, ids = env
    st = str(main.STATE)
    main.playstate.record(st, ids["kim"], "jellyfin", "ep5", 600, 1800, False, parent_id="series1",
                          label="The Bear — S02E05 · Fishes")
    main.playstate.record(st, ids["kim"], "abs", "li-9", 3600, 7200, False)
    main.playstate.record(st, ids["owner"], "calibre", "7", 0.4, 1.0, False)
    con = sqlite3.connect(st)                     # the audiobook was last played an hour ago
    con.execute("UPDATE play_progress SET updated=? WHERE item_id='li-9'", (time.time() - 3600,))
    con.commit()
    con.close()

    w = admin.get("/api/activity").json()["watching"]
    now = {(e["username"], e["title"]) for e in w["now"]}
    assert now == {("kim", "The Bear"), ("owner", "Dune")}
    show = next(e for e in w["now"] if e["username"] == "kim")
    assert show["detail"].endswith("S02E05 · Fishes") and show["percent"] == 33 and show["verb"] == "watching"
    kim = next(p for p in w["people"] if p["username"] == "kim")
    assert [e["title"] for e in kim["recent"]] == ["The Bear", "Dune"]
    assert kim["recent"][1]["verb"] == "listening to" and kim["recent"][1]["live"] is False


def test_members_never_get_it(env):
    main, admin, member, ids = env
    main.playstate.record(str(main.STATE), ids["owner"], "jellyfin", "ep5", 60, 1800, False)
    assert "watching" not in member.get("/api/activity").json()


def test_finished_and_unknown_items(env):
    main, admin, member, ids = env
    st = str(main.STATE)
    main.playstate.record(st, ids["kim"], "jellyfin", "movie-x", 1790, 1800, False, label="Some Movie")
    main.playstate.record(st, ids["kim"], "jellyfin", "gone", 10, 1800, False)
    w = admin.get("/api/activity").json()["watching"]
    titles = {e["title"]: e for e in w["people"][0]["recent"]}
    assert titles["Some Movie"]["finished"] and not titles["Some Movie"]["live"]
    assert "Something not in the library index" in titles and titles["Something not in the library index"]["live"]
    assert [e["title"] for e in w["now"]] == ["Something not in the library index"]
