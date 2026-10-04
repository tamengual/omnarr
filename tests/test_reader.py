"""In-app reading: comic pages from Komga and EPUBs, with per-person positions."""
import importlib
import sqlite3
import sys

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app import playstate


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    lib = tmp_path / "calibre"
    (lib / "A" / "Dune (1)").mkdir(parents=True)
    (lib / "A" / "Dune (1)" / "Dune - A.epub").write_bytes(b"EPUBDATA")
    db = sqlite3.connect(lib / "metadata.db")
    db.executescript("CREATE TABLE books (id INTEGER, title TEXT, path TEXT); CREATE TABLE data (book INTEGER, format TEXT, name TEXT);")
    db.execute("INSERT INTO books VALUES (1, 'Dune', 'A/Dune (1)')")
    db.execute("INSERT INTO data VALUES (1, 'EPUB', 'Dune - A')")
    db.commit(); db.close()
    main.cfg.save_connection("calibre", {"db": str(lib / "metadata.db"), "library": str(lib)})
    main.cfg.save_connection("komga", {"url": "http://komga", "api_key": "k"})
    from app import indexer
    idx = sqlite3.connect(tmp_path / "index.db")
    idx.executescript(indexer.SCHEMA)
    idx.executemany("INSERT INTO works (id, kind, title, adult, hidden) VALUES (?, ?, ?, ?, 0)",
                    [("w1", "book", "Dune", 0), ("w2", "comic", "Korra", 0), ("w3", "comic", "Spicy", 1)])
    idx.executemany("INSERT INTO editions (work_id, unit_key, source, source_id, title, extra) VALUES (?,?,?,?,?,?)",
                    [("w1", "calibre:1", "calibre", "1", "Dune", '{"formats": ["EPUB"]}'),
                     ("w2", "komga:b1", "komga", "b1", "Korra", "{}"),
                     ("w3", "komga:b9", "komga", "b9", "Spicy", "{}")])
    idx.commit(); idx.close()
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "guest", "password": "guest-pass-1", "can_request": False})
    guest = TestClient(main.app)
    guest.post("/api/auth/login", json={"username": "guest", "password": "guest-pass-1"})
    return main, admin, guest, tmp_path


class FakeKomga:
    """Komga's book, pages, next/previous and read-progress endpoints."""
    patches = []

    def __init__(self, read_progress=None):
        self.rp = read_progress

    def __call__(self, s, timeout=60):
        return self

    def __enter__(self): return self
    def __exit__(self, *a): pass

    def get(self, path, params=None):
        class R:
            def __init__(r, code, data): r.status_code, r._d = code, data
            def json(r): return r._d
        if path.endswith("/pages"):
            return R(200, [{"number": n, "width": 100, "height": 150} for n in range(1, 11)])
        if path.endswith("/next"):
            return R(200, {"id": "b2"})
        if path.endswith("/previous"):
            return R(404, {})
        return R(200, {"id": "b1", "seriesTitle": "Korra", "media": {"pagesCount": 10, "mediaProfile": "DIVINA"},
                       "readProgress": self.rp})

    def patch(self, path, json=None):
        FakeKomga.patches.append((path, json))


def test_reading_needs_no_download_switch(env):
    main, admin, guest, tmp = env
    assert guest.get("/api/download/calibre:1").status_code == 403         # can't save the file…
    info = guest.get("/api/read/info/calibre:1").json()                      # …but can read it
    assert info["mode"] == "epub" and info["resume"]["locator"] is None
    r = guest.get(info["file_url"].replace("api/", "/api/", 1))
    assert r.status_code == 200 and r.content == b"EPUBDATA" and "attachment" not in r.headers.get("content-disposition", "")


def test_ebook_position_is_per_person(env):
    main, admin, guest, tmp = env
    guest.post("/api/read/progress", json={"unit_key": "calibre:1", "fraction": 0.4, "locator": "epubcfi(/6/4!/4/2)"})
    assert guest.get("/api/read/info/calibre:1").json()["resume"] == {"locator": "epubcfi(/6/4!/4/2)", "xpath": None, "fraction": 0.4,
                                                                  "finished": False, "from": "omnarr"}
    assert admin.get("/api/read/info/calibre:1").json()["resume"]["locator"] is None


def test_adult_comic_pages_are_hidden(env, monkeypatch):
    main, admin, guest, tmp = env
    monkeypatch.setattr(main.komga, "client", FakeKomga())
    monkeypatch.setattr(main, "_proxy", lambda url, request, headers: _ok())
    assert guest.get("/api/read/info/komga:b9").status_code == 404
    assert guest.get("/api/read/page/b9/1").status_code == 404
    assert guest.get("/api/read/page/b1/1").status_code == 200
    assert guest.get("/api/read/page/nope/1").status_code == 404


async def _ok():
    return Response(b"IMG", media_type="image/jpeg")


def test_comic_progress_writes_komga_for_admins_only(env, monkeypatch):
    main, admin, guest, tmp = env
    monkeypatch.setattr(main.komga, "client", FakeKomga())
    FakeKomga.patches.clear()
    info = guest.get("/api/read/info/komga:b1").json()
    assert info["mode"] == "pages" and len(info["pages"]) == 10 and info["next"] == "komga:b2" and not info["app_sync"]
    assert guest.post("/api/read/progress", json={"unit_key": "komga:b1", "page": 4, "pages": 10}).json()["app_sync"] is False
    assert FakeKomga.patches == []                                                  # a guest never moves the owner's place
    assert guest.get("/api/read/info/komga:b1").json()["resume"] == {"page": 4, "finished": False}
    assert admin.post("/api/read/progress", json={"unit_key": "komga:b1", "page": 10, "pages": 10}).json()["app_sync"]
    assert FakeKomga.patches == [("/api/v1/books/b1/read-progress", {"page": 10, "completed": True})]


def test_admin_resumes_from_komga_when_it_is_newer(env, monkeypatch):
    main, admin, guest, tmp = env
    monkeypatch.setattr(main.komga, "client", FakeKomga({"page": 7, "completed": False, "lastModified": "2999-01-01T00:00:00Z"}))
    admin.post("/api/read/progress", json={"unit_key": "komga:b1", "page": 3, "pages": 10})
    assert admin.get("/api/read/info/komga:b1").json()["resume"]["page"] == 7


def test_finished_rules(tmp_path):
    st = str(tmp_path / "s.db")
    playstate.record(st, 1, "komga", "b", 9, 10, False)
    playstate.record(st, 1, "calibre", "e", 0.5, 1.0, False)
    playstate.record(st, 1, "abs", "a", 3590, 3600, False)                  # audio keeps its 30 s grace
    rows = {r["item_id"]: bool(r["finished"]) for r in playstate.all_rows(st)}
    assert rows == {"b": False, "e": False, "a": True}


def test_old_progress_table_gets_a_locator(tmp_path):
    st = tmp_path / "s.db"
    con = sqlite3.connect(st)
    con.execute("""CREATE TABLE play_progress (account_id INTEGER, source TEXT, item_id TEXT, parent_id TEXT,
                   position REAL, duration REAL, finished INTEGER, updated REAL, PRIMARY KEY (account_id, source, item_id))""")
    con.execute("INSERT INTO play_progress VALUES (1, 'jellyfin', 'm', NULL, 60, 6000, 0, 1)")
    con.commit(); con.close()
    playstate.record(str(st), 1, "calibre", "e", 0.2, 1.0, False, locator="epubcfi(/6/2)")
    assert playstate.get_place(str(st), 1, "calibre", "e")["locator"] == "epubcfi(/6/2)"
    assert playstate.get(str(st), 1, "jellyfin", "m") == (60, False)


def test_iso_time_and_generic_titles():
    from app.connectors.base import iso_time
    from app.connectors.komga import display_title
    assert iso_time(1791068417855) == iso_time("1791068417") == "2026-10-03T23:00:17"
    assert iso_time("2026-10-04T04:45:46Z") == "2026-10-04T04:45:46" and iso_time(None) == ""
    assert display_title("Part 01", "The Legend of Korra: Ruins of the Empire") == "The Legend of Korra: Ruins of the Empire #1"
    assert display_title("The Hedge Knight", "Tales of Dunk and Egg") == "The Hedge Knight"


def _bridge(main, tmp, monkeypatch, theirs):
    db = tmp / "bb.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE books (abs_id TEXT, ebook_source TEXT, ebook_source_id TEXT, kosync_doc_id TEXT)")
    con.execute("INSERT INTO books VALUES ('a1', 'CWA', '1', 'abc123')")
    con.commit(); con.close()
    main.cfg.save_connection("bookbridge", {"db": str(db), "sync_url": "http://bb", "kosync_user": "me", "kosync_key": "pw"})
    puts = []
    monkeypatch.setattr(main.bookbridge_sync, "get_position", lambda s, doc: theirs if doc == "abc123" else None)
    monkeypatch.setattr(main.bookbridge_sync, "put_position", lambda s, doc, frac, xpath, device_id=None: puts.append((doc, frac, xpath)))
    return puts


def test_owner_ebook_place_syncs_through_bookbridge(env, monkeypatch):
    main, admin, guest, tmp = env
    puts = _bridge(main, tmp, monkeypatch, {"fraction": 0.3, "xpath": "/body/DocFragment[4]/body/p[9].0",
                                             "updated": 4e9, "device": "Kobo"})
    info = admin.get("/api/read/info/calibre:1").json()
    assert info["app_sync"] and info["resume"]["xpath"] == "/body/DocFragment[4]/body/p[9].0"
    assert info["resume"]["fraction"] == 0.3 and info["resume"]["from"] == "Kobo"
    r = admin.post("/api/read/progress", json={"unit_key": "calibre:1", "fraction": 0.31, "locator": "epubcfi(/6/8)",
                                               "xpath": "/body/DocFragment[4]/body/p[10].0"}).json()
    assert r["app_sync"] and puts == [("abc123", 0.31, "/body/DocFragment[4]/body/p[10].0")]


def test_own_newer_place_wins_and_members_never_sync(env, monkeypatch):
    main, admin, guest, tmp = env
    puts = _bridge(main, tmp, monkeypatch, {"fraction": 0.3, "xpath": "/body/DocFragment[4]/body/p[9].0",
                                             "updated": 1.0, "device": "Kobo"})
    admin.post("/api/read/progress", json={"unit_key": "calibre:1", "fraction": 0.5, "locator": "epubcfi(/6/10)", "xpath": "junk"})
    assert puts == [("abc123", 0.5, "")]                                        # a malformed XPath is never sent
    assert admin.get("/api/read/info/calibre:1").json()["resume"]["locator"] == "epubcfi(/6/10)"
    guest.post("/api/read/progress", json={"unit_key": "calibre:1", "fraction": 0.7, "locator": "epubcfi(/6/12)"})
    info = guest.get("/api/read/info/calibre:1").json()
    assert not info["app_sync"] and info["resume"]["xpath"] is None and len(puts) == 1


def test_komga_file_name_titles_become_series_numbers():
    from app.connectors.komga import display_title
    t = "The_Legend_of_Korra_-_Ruins_of_the_Empire_Part_02_(2019)_(digital)"
    assert display_title(t, "The Legend of Korra: Ruins of the Empire", 2.0) == "The Legend of Korra: Ruins of the Empire #2"
    assert display_title("Spidertales 1", "", 1.0) == "Spidertales 1"
