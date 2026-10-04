"""Read-alongs: the EPUB served as a folder (audio streamed by range), places synced through
BookBridge, and per-person BookBridge logins."""
import importlib
import sqlite3
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "tests")
import readalong_fixture  # noqa: E402

UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    lib = tmp_path / "library" / "Sea Book"
    lib.mkdir(parents=True)
    epub = readalong_fixture.build(lib / "Sea Book (readaloud).epub")
    st = sqlite3.connect(tmp_path / "storyteller.db")
    st.executescript("CREATE TABLE book (uuid TEXT, title TEXT); CREATE TABLE audiobook (book_uuid TEXT, filepath TEXT);")
    st.execute("INSERT INTO book VALUES (?, 'Sea Book')", (UUID,))
    st.execute("INSERT INTO audiobook VALUES (?, '/library/Sea Book/Sea Book.m4b')", (UUID,))
    st.commit(); st.close()
    main.cfg.save_connection("storyteller", {"db": str(tmp_path / "storyteller.db"), "library": str(tmp_path / "library")})
    from app import indexer
    idx = sqlite3.connect(tmp_path / "index.db")
    idx.executescript(indexer.SCHEMA)
    idx.execute("INSERT INTO works (id, kind, title, adult, hidden) VALUES ('w1', 'book', 'Sea Book', 0, 0)")
    idx.execute("INSERT INTO editions (work_id, unit_key, source, source_id, title, extra) VALUES ('w1', ?, 'storyteller', ?, 'Sea Book', '{}')",
                (f"storyteller:{UUID}", UUID))
    idx.commit(); idx.close()
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "guest", "password": "guest-pass-1", "can_request": False})
    guest = TestClient(main.app)
    guest.post("/api/auth/login", json={"username": "guest", "password": "guest-pass-1"})
    return main, admin, guest, tmp_path, epub


def part(client, member, **headers):
    return client.get(f"/api/read/part/storyteller/{UUID}/{member}", headers=headers)


def test_info_and_folder_view(env):
    main, admin, guest, tmp, epub = env
    info = guest.get(f"/api/read/info/storyteller:{UUID}").json()
    assert info["mode"] == "readalong" and info["root_url"] == f"api/read/part/storyteller/{UUID}/"
    assert info["offline_allowed"] is False
    r = part(guest, "META-INF/container.xml")
    assert r.status_code == 200 and b"content.opf" in r.content
    assert part(guest, "OEBPS/MediaOverlays/c1.smil").headers["content-type"].startswith("application/smil+xml")
    assert part(guest, "../../storyteller.db").status_code == 404                # only names inside the EPUB
    assert part(guest, "OEBPS%2F..%2F..%2Fstoryteller.db").status_code == 404
    assert part(guest, "nope.xhtml").status_code == 404


def test_audio_streams_by_byte_range(env):
    main, admin, guest, tmp, epub = env
    whole = zipfile.ZipFile(epub).read("OEBPS/Audio/00001.wav")
    r = part(guest, "OEBPS/Audio/00001.wav")
    assert r.status_code == 200 and r.content == whole and r.headers["accept-ranges"] == "bytes"
    r = part(guest, "OEBPS/Audio/00001.wav", Range="bytes=1000-2999")
    assert r.status_code == 206 and r.content == whole[1000:3000]
    assert r.headers["content-range"] == f"bytes 1000-2999/{len(whole)}"
    assert part(guest, "OEBPS/Audio/00001.wav", Range="bytes=-100").content == whole[-100:]
    assert part(guest, "OEBPS/Audio/00001.wav", Range=f"bytes={len(whole) + 5}-").status_code == 416


def _bridge(main, tmp, monkeypatch, theirs):
    db = tmp / "bb.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE books (abs_id TEXT, ebook_source TEXT, ebook_source_id TEXT, storyteller_uuid TEXT, kosync_doc_id TEXT)")
    con.execute("INSERT INTO books VALUES ('a1', 'CWA', '7', ?, 'seadoc')", (UUID,))
    con.commit(); con.close()
    main.cfg.save_connection("bookbridge", {"db": str(db), "sync_url": "http://bb", "kosync_user": "owner", "kosync_key": "pw"})
    calls = []
    monkeypatch.setattr(main.bookbridge_sync, "get_position", lambda s, doc: (calls.append(("get", s["kosync_user"], doc)), theirs)[1])
    monkeypatch.setattr(main.bookbridge_sync, "put_position",
                        lambda s, doc, frac, xpath, device_id=None: calls.append(("put", s["kosync_user"], doc, frac, xpath, device_id)))
    monkeypatch.setattr(main.bookbridge_sync, "check", lambda s: (s["kosync_key"] == "right", "ok" if s["kosync_key"] == "right" else "KOSync login rejected"))
    return calls


def test_readalong_place_syncs_through_bookbridge(env, monkeypatch):
    main, admin, guest, tmp, epub = env
    calls = _bridge(main, tmp, monkeypatch, {"fraction": 0.5, "xpath": "/body/DocFragment[2]/body/p[1].0", "updated": 4e9, "device": "abs-kosync-bridge"})
    info = admin.get(f"/api/read/info/storyteller:{UUID}").json()
    assert info["app_sync"] and info["resume"]["xpath"] == "/body/DocFragment[2]/body/p[1].0"
    admin.post("/api/read/progress", json={"unit_key": f"storyteller:{UUID}", "fraction": 0.6, "locator": "epubcfi(/6/4!/4/4)",
                                           "xpath": "/body/DocFragment[2]/body/p[2].0"})
    put = [c for c in calls if c[0] == "put"]
    assert put == [("put", "owner", "seadoc", 0.6, "/body/DocFragment[2]/body/p[2].0", "omnarr-1")]
    # a guest without their own login never syncs (and never uses the owner's)
    assert guest.get(f"/api/read/info/storyteller:{UUID}").json()["app_sync"] is False


def test_people_can_link_their_own_bookbridge_login(env, monkeypatch):
    main, admin, guest, tmp, epub = env
    calls = _bridge(main, tmp, monkeypatch, None)
    assert guest.post("/api/me", json={"kosync_user": "guestreader", "kosync_key": "wrong"}).status_code == 400
    r = guest.post("/api/me", json={"kosync_user": "guestreader", "kosync_key": "right"})
    assert r.status_code == 200 and r.json()["account"]["kosync_key"] == "••••"
    assert "right" not in r.text                                                   # the password never comes back
    assert guest.get("/api/me").json()["kosync_user"] == "guestreader"
    assert guest.get(f"/api/read/info/storyteller:{UUID}").json()["app_sync"] is True
    guest.post("/api/read/progress", json={"unit_key": f"storyteller:{UUID}", "fraction": 0.2, "xpath": "/body/DocFragment[1]/body/p[2].0"})
    put = [c for c in calls if c[0] == "put"][-1]
    assert put[1] == "guestreader" and put[5].startswith("omnarr-") and put[5] != "omnarr-1"
    # saving other settings with the masked key keeps the stored one
    assert guest.post("/api/me", json={"kosync_user": "guestreader", "kosync_key": "••••", "notify_email": False}).status_code == 200
    assert guest.get(f"/api/read/info/storyteller:{UUID}").json()["app_sync"] is True
