"""Requesting books/comics that aren't in the library (from related works)."""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from app import wanted


@pytest.fixture()
def client(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    monkeypatch.setattr(main, "_wanted_tick", lambda: None)
    c = TestClient(main.app)
    c.post("/api/auth/setup", json={"password": "correct horse"})
    return c, main


def test_candidates_by_title_and_comic_maps_to_ebook(client, monkeypatch):
    c, main = client
    seen = {}
    monkeypatch.setattr(main.requests_, "book_candidates",
                        lambda cfg, q, t: seen.update(q=q, t=t) or [{"title": "The Promise", "authors": ["Gene Luen Yang"]}])
    r = c.get("/api/request/book/candidates", params={"title": "The Promise", "author": "Gene Luen Yang", "format": "comic"})
    assert r.status_code == 200 and r.json()["candidates"][0]["title"] == "The Promise"
    assert seen == {"q": "The Promise Gene Luen Yang", "t": "ebook"}
    assert c.get("/api/request/book/candidates", params={"format": "ebook"}).status_code == 400
    assert c.get("/api/request/book/candidates", params={"title": "x", "format": "vinyl"}).status_code == 400


def test_wanted_by_title(client):
    c, main = client
    r = c.post("/api/wanted", json={"title": "The Rise of Kyoshi", "author": "F. C. Yee", "format": "audiobook"})
    assert r.status_code == 200
    items = c.get("/api/wanted").json()["items"]
    assert any(i["title"] == "The Rise of Kyoshi" and i["format"] == "audiobook" for i in items)
    assert c.post("/api/wanted", json={"format": "ebook"}).status_code == 400


def test_comic_format_rules():
    assert wanted.shelfmark_type("comic") == "ebook" and wanted.shelfmark_type("audiobook") == "audiobook"
    ok, _ = wanted.acceptable({"title": "Avatar The Promise", "format": "cbz", "size": 80_000_000, "source_id": "a"},
                              "Avatar The Promise", "comic", set())
    assert ok
    bad, _ = wanted.acceptable({"title": "Avatar The Promise", "format": "mp3", "size": 80_000_000, "source_id": "b"},
                               "Avatar The Promise", "comic", set())
    assert not bad
    epub, _ = wanted.acceptable({"title": "Avatar The Promise", "format": "epub", "size": 80_000_000, "source_id": "c"},
                                "Avatar The Promise", "comic", set())
    assert not epub                                      # an epub would land in Calibre, not Komga


def test_finished_comic_goes_to_komga(tmp_path, monkeypatch):
    from app import requests_
    from app.connectors import komga
    state = str(tmp_path / "state.db")
    index = str(tmp_path / "index.db")
    import sqlite3
    sqlite3.connect(index).execute("CREATE TABLE works (id TEXT, kind TEXT, title TEXT, authors TEXT, formats TEXT)")
    wid = wanted.add(state, "Avatar: The Last Airbender – The Search", "Gene Luen Yang", "comic", current="rel-1")
    con = wanted._con(state)
    with con:
        con.execute("UPDATE wanted_books SET status='downloading', current='rel-1' WHERE id=?", (wid,))
    scans = []
    monkeypatch.setattr(requests_, "shelfmark_enabled", lambda cfg: True)
    monkeypatch.setattr(requests_, "shelfmark_activity", lambda cfg: {"status": {"complete": {"rel-1": {"id": "rel-1"}}}})
    monkeypatch.setattr(komga, "scan_all", lambda cfg, sp=None: scans.append(1) or True)
    wanted.tick(None, state, index)
    row = next(i for i in wanted.list_all(state) if i["id"] == wid)
    assert row["status"] == "done" and "Komga" in row["note"] and scans == [1]
