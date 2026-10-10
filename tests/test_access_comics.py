"""Comic EPUB uploads land in the comics folder as CBZ; private access through Tailscale invites."""
import importlib
import io
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient


def make_epub(pages=24, fixed=True, title="Test Omnibus", text=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        items, spine = [], []
        for n in range(1, pages + 1):
            body = (f"<p>{'word ' * 300}</p>" if text else f'<img src="../images/p{n:03d}.jpg"/>')
            z.writestr(f"OEBPS/xhtml/p{n}.xhtml", f"<html><head><title>{n}</title></head><body>{body}</body></html>")
            if not text:
                z.writestr(f"OEBPS/images/p{n:03d}.jpg", bytes([n]) * 50)
            items.append(f'<item id="p{n}" href="xhtml/p{n}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="p{n}"/>')
        layout = '<meta property="rendition:layout">pre-paginated</meta>' if fixed else ""
        z.writestr("OEBPS/content.opf", f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title>{layout}</metadata>'
                   f'<manifest>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>')
    return buf.getvalue()


def test_comicpack_detects_and_converts(tmp_path):
    from app import comicpack
    comic, novel = tmp_path / "c.epub", tmp_path / "n.epub"
    comic.write_bytes(make_epub())
    novel.write_bytes(make_epub(pages=30, fixed=False, text=True))
    assert comicpack.inspect(str(comic)) == {"comic": True, "pages": 24, "title": "Test Omnibus"}
    assert comicpack.inspect(str(novel))["comic"] is False
    assert comicpack.inspect(str(tmp_path / "missing.epub"))["comic"] is False
    out = tmp_path / "c.cbz"
    assert comicpack.to_cbz(str(comic), str(out)) == 24
    names = zipfile.ZipFile(out).namelist()
    assert names[0] == "01.jpg" and names[-1] == "24.jpg"
    assert zipfile.ZipFile(out).read("03.jpg") == bytes([3]) * 50            # same bytes, same order


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    token = admin.post("/api/accounts/invites", json={"link_base": "https://media.example/", "can_upload": True}).json()["url"].split("#invite=")[1]
    member = TestClient(main.app)
    member.post(f"/api/auth/invite/{token}", json={"username": "kim", "password": "member-pass-1"})
    return main, admin, member, tmp_path


def test_comic_uploads_go_to_the_comics_folder(env):
    main, admin, member, tmp = env
    books, comics = tmp / "ingest", tmp / "comics"
    books.mkdir(); comics.mkdir()
    assert admin.post("/api/setup/options", json={"upload_dir": str(books), "upload_comics_dir": str(comics)}).status_code == 200
    r = member.post("/api/upload", files=[("files", ("turf-wars.epub", make_epub(title="Turf Wars"), "application/epub+zip")),
                                          ("files", ("novel.epub", make_epub(pages=30, fixed=False, text=True), "application/epub+zip")),
                                          ("files", ("issue 1.cbz", b"PK\x05\x06" + b"\0" * 18, "application/zip"))])
    assert r.status_code == 200 and r.json()["ok"], r.json()
    assert (comics / "Turf Wars" / "Turf Wars.cbz").exists()
    assert (comics / "issue 1" / "issue 1.cbz").exists()
    assert (books / "novel.epub").exists() and not (books / "turf-wars.epub").exists()
    assert not list(books.glob("*.part")) and not list(comics.rglob("*.part"))
    assert member.get("/api/upload").json()["comics_enabled"] is True


def test_private_access_ask_approve_and_remove(env, monkeypatch):
    main, admin, member, tmp = env
    from app import tailnet
    assert member.get("/api/private-access").json() == {"enabled": False}
    main.cfg.save_connection("tailscale", {"api_key": "tskey-api-kX-y", "device": "media-box"})
    invites = {}
    monkeypatch.setattr(tailnet, "create_invite", lambda s, email=None: invites.setdefault("i1", {"id": "i1", "url": "https://login.tailscale.com/admin/invite/abc"}))
    status = {"accepted": False, "by": ""}
    monkeypatch.setattr(tailnet, "invite_status", lambda s, iid: dict(status) if iid in invites else None)
    deleted = []
    monkeypatch.setattr(tailnet, "delete_invite", lambda s, iid: deleted.append(iid) or True)

    assert member.get("/api/private-access").json()["state"] == "none"
    asked = member.post("/api/private-access").json()
    assert asked["queued"] is True
    assert member.get("/api/private-access").json()["state"] == "pending"
    assert member.post("/api/private-access").json()["state"] == "pending"           # asking twice doesn't queue twice
    pending = [r for r in admin.get("/api/requests").json()["requests"] if r["kind"] == "private_access"]
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    assert admin.post(f"/api/requests/{pending[0]['id']}/approve").json()["status"] == "approved"
    seen = member.get("/api/private-access").json()
    assert seen == {"enabled": True, "state": "invited", "url": "https://login.tailscale.com/admin/invite/abc"}
    status.update(accepted=True, by="kim@example.com")
    assert member.get("/api/private-access").json() == {"enabled": True, "state": "connected", "as": "kim@example.com"}

    kim = next(a for a in admin.get("/api/accounts").json()["accounts"] if a["username"] == "kim")
    out = admin.delete(f"/api/accounts/{kim['id']}").json()
    assert "kim@example.com" in out["message"] and "media-box" in out["message"] and deleted == []   # accepted: remove by hand


def test_private_access_unused_invite_is_cancelled_and_denials_shown(env, monkeypatch):
    main, admin, member, tmp = env
    from app import tailnet
    main.cfg.save_connection("tailscale", {"api_key": "tskey-api-kX-y", "device": "media-box"})
    monkeypatch.setattr(tailnet, "create_invite", lambda s, email=None: {"id": "i2", "url": "https://login.tailscale.com/admin/invite/x"})
    monkeypatch.setattr(tailnet, "invite_status", lambda s, iid: {"accepted": False, "by": ""})
    deleted = []
    monkeypatch.setattr(tailnet, "delete_invite", lambda s, iid: deleted.append(iid) or True)
    member.post("/api/private-access")
    rid = admin.get("/api/requests").json()["requests"][0]["id"]
    admin.post(f"/api/requests/{rid}/deny", json={"note": "next month"})
    assert member.get("/api/private-access").json() == {"enabled": True, "state": "denied", "note": "next month"}
    member.post("/api/private-access")                                                       # asks again
    rid = [r for r in admin.get("/api/requests").json()["requests"] if r["status"] == "pending"][0]["id"]
    admin.post(f"/api/requests/{rid}/approve")
    kim = next(a for a in admin.get("/api/accounts").json()["accounts"] if a["username"] == "kim")
    assert "message" not in admin.delete(f"/api/accounts/{kim['id']}").json() and deleted == ["i2"]


def test_tailnet_client_shapes(monkeypatch):
    from app import tailnet
    calls = []

    class FakeResponse:
        def __init__(self, code, data): self.status_code, self._d, self.text = code, data, ""
        def json(self): return self._d
        def raise_for_status(self): pass

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, path): calls.append(("GET", path)); return FakeResponse(200, {"devices": [{"nodeId": "n1", "name": "media-box.tail.ts.net", "hostname": "Media-Box"}]})
        def post(self, path, json): calls.append(("POST", path, json)); return FakeResponse(200, [{"id": 7, "inviteUrl": "https://login.tailscale.com/admin/invite/q"}])
    monkeypatch.setattr(tailnet.httpx, "Client", FakeClient)
    inv = tailnet.create_invite({"api_key": "k", "device": "media-box"}, email="kim@example.com")
    assert inv == {"id": "7", "url": "https://login.tailscale.com/admin/invite/q"}
    assert calls[0] == ("GET", "/tailnet/-/devices")
    assert calls[1] == ("POST", "/device/n1/device-invites", [{"multiUse": False, "allowExitNode": False, "email": "kim@example.com"}])
    with pytest.raises(LookupError):
        tailnet.find_device({"api_key": "k", "device": "nope"})
