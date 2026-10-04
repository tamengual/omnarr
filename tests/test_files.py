"""Save-to-device downloads and uploads, with their permission switches."""
import importlib
import json
import sqlite3
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
    # a Calibre library with one book (EPUB + MOBI)
    lib = tmp_path / "calibre"
    (lib / "Frank Herbert" / "Dune (1)").mkdir(parents=True)
    (lib / "Frank Herbert" / "Dune (1)" / "Dune - Frank Herbert.epub").write_bytes(b"EPUBDATA")
    (lib / "Frank Herbert" / "Dune (1)" / "Dune - Frank Herbert.mobi").write_bytes(b"MOBIDATA")
    db = sqlite3.connect(lib / "metadata.db")
    db.executescript("CREATE TABLE books (id INTEGER, title TEXT, path TEXT); CREATE TABLE data (book INTEGER, format TEXT, name TEXT);")
    db.execute("INSERT INTO books VALUES (1, 'Dune', 'Frank Herbert/Dune (1)')")
    db.executemany("INSERT INTO data VALUES (1, ?, 'Dune - Frank Herbert')", [("EPUB",), ("MOBI",)])
    db.commit(); db.close()
    main.cfg.save_connection("calibre", {"db": str(lib / "metadata.db"), "library": str(lib)})
    # an index with that edition
    from app import indexer
    idx = sqlite3.connect(tmp_path / "index.db")
    idx.executescript(indexer.SCHEMA)
    idx.execute("INSERT INTO works (id, kind, title, adult, hidden) VALUES ('w1', 'book', 'Dune', 0, 0)")
    idx.execute("INSERT INTO editions (work_id, unit_key, source, source_id, title, extra) VALUES ('w1','calibre:1','calibre','1','Dune','{}')")
    idx.commit(); idx.close()
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "travis", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "guest", "password": "guest-pass-1", "can_request": False})
    guest = TestClient(main.app)
    guest.post("/api/auth/login", json={"username": "guest", "password": "guest-pass-1"})
    return main, admin, guest, tmp_path


def test_download_needs_permission(env):
    main, admin, guest, tmp = env
    assert guest.get("/api/download/calibre:1").status_code == 403
    r = admin.get("/api/download/calibre:1")
    assert r.status_code == 200 and r.content == b"EPUBDATA" and "Dune.epub" in r.headers["content-disposition"]
    assert admin.get("/api/download/calibre:1", params={"format": "mobi"}).content == b"MOBIDATA"
    gid = next(a["id"] for a in admin.get("/api/accounts").json()["accounts"] if a["username"] == "guest")
    admin.patch(f"/api/accounts/{gid}", json={"can_download": True})
    assert guest.get("/api/download/calibre:1").status_code == 200
    assert guest.get("/api/download/nope:9").status_code == 404


def test_uploads(env):
    main, admin, guest, tmp = env
    books, audio = tmp / "ingest", tmp / "audiobooks"
    books.mkdir(); audio.mkdir()
    assert admin.post("/api/setup/options", json={"upload_dir": str(tmp / "missing")}).status_code == 400
    r = admin.post("/api/setup/options", json={"upload_dir": str(books), "upload_audio_dir": str(audio), "upload_max_mb": 1})
    assert r.status_code == 200
    assert guest.post("/api/upload", files={"files": ("a.epub", b"x")}).status_code == 403          # no switch yet
    r = admin.post("/api/upload", files=[("files", ("My Book.epub", b"book")), ("files", ("song.exe", b"bad")),
                                         ("files", ("Talk.m4b", b"audio")), ("files", ("../../evil.cbz", b"c"))])
    res = {x["file"]: x for x in r.json()["results"]}
    assert res["My Book.epub"]["ok"] and (books / "My Book.epub").read_bytes() == b"book"
    assert not res["song.exe"]["ok"]
    assert (audio / "Uploads" / "Talk" / "Talk.m4b").exists()
    assert not any(p.name == "evil.cbz" for p in tmp.parent.glob("*"))                            # no path escape
    big = admin.post("/api/upload", files={"files": ("Big.epub", b"0" * (2 * 1024 * 1024))}).json()["results"][0]
    assert not big["ok"] and not list(books.glob("Big*"))                                          # cut off, cleaned up
    acts = sqlite3.connect(tmp / "state.db").execute("SELECT payload FROM actions WHERE action='upload'").fetchall()
    assert any(json.loads(p[0])["by"] == "travis" for p in acts)
