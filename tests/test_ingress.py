"""Path-prefix + Home Assistant ingress: HA's login is trusted ONLY from HA's ingress proxy."""
import importlib
import sys

import pytest
from fastapi.testclient import TestClient

from app.play import rewrite_playlist

HDR = {"X-Ingress-Path": "/api/hassio_ingress/abc123", "X-Remote-User-Id": "u1"}


@pytest.fixture()
def main(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    m = importlib.import_module("app.main")
    monkeypatch.setattr(m, "_reindex", lambda: None)
    return m


def test_ingress_trusted_only_when_enabled_and_from_proxy(main, monkeypatch):
    c = TestClient(main.app)                                  # TestClient's client host is "testclient"
    assert c.get("/api/setup/apps", headers=HDR).status_code == 401      # add-on mode off
    monkeypatch.setattr(main, "HA_INGRESS", True)
    monkeypatch.setattr(main, "INGRESS_PROXY", "10.9.9.9")
    assert c.get("/api/setup/apps", headers=HDR).status_code == 401      # not from HA's proxy
    monkeypatch.setattr(main, "INGRESS_PROXY", "testclient")
    assert c.get("/api/setup/apps").status_code == 401                   # from proxy but no ingress header
    assert c.get("/api/setup/apps", headers=HDR).status_code == 200
    st = c.get("/api/auth/status", headers=HDR).json()
    assert st["logged_in"] and st["ha_ingress"]


def test_ingress_session_is_not_a_usable_cookie(main, monkeypatch):
    monkeypatch.setattr(main, "HA_INGRESS", True)
    monkeypatch.setattr(main, "INGRESS_PROXY", "testclient")
    c = TestClient(main.app)
    c.get("/api/auth/status", headers=HDR)                                # creates the "ha:u1" session row
    c.cookies.set(main.COOKIE, "ha:u1")
    assert c.get("/api/setup/apps").status_code == 401                   # forging the cookie gets nothing


def test_addon_direct_visitor_cannot_claim_first_password(main, monkeypatch):
    monkeypatch.setattr(main, "HA_INGRESS", True)
    monkeypatch.setattr(main, "INGRESS_PROXY", "10.9.9.9")              # direct visitor, not HA
    c = TestClient(main.app)
    assert c.get("/api/auth/status").json()["setup_needed"] is False
    assert c.post("/api/auth/setup", json={"password": "attacker-pass"}).status_code == 403
    assert c.post("/api/auth/login", json={"password": "anything"}).status_code == 403
    # the owner sets it from inside HA, then direct sign-in works
    monkeypatch.setattr(main, "INGRESS_PROXY", "testclient")
    assert c.post("/api/auth/password", json={"new": "owner-pass-1"}, headers=HDR).status_code == 200
    monkeypatch.setattr(main, "INGRESS_PROXY", "10.9.9.9")
    assert c.post("/api/auth/login", json={"password": "owner-pass-1"}).status_code == 200
    assert c.get("/api/setup/apps").status_code == 200


def test_password_change_needs_current_password(main):
    c = TestClient(main.app)
    c.post("/api/auth/setup", json={"password": "first-pass-1"})
    assert c.post("/api/auth/password", json={"current": "wrong", "new": "second-pass"}).status_code == 403
    assert c.post("/api/auth/password", json={"current": "first-pass-1", "new": "second-pass"}).status_code == 200
    assert TestClient(main.app).post("/api/auth/login", json={"password": "second-pass"}).status_code == 200
    assert TestClient(main.app).post("/api/auth/password", json={"new": "x" * 10}).status_code == 403   # not signed in


def test_playlist_rewrite_uses_base_path():
    pl = "#EXTM3U\n/videos/1/hls1/main/0.ts?api_key=SECRET&x=1\n"
    out = rewrite_playlist(pl, "/api/hassio_ingress/abc123")
    assert "/api/hassio_ingress/abc123/api/stream/jf/videos/1/hls1/main/0.ts?x=1" in out
    assert "SECRET" not in out
    assert "\n/api/stream/jf/videos/" in rewrite_playlist(pl)            # site root unchanged
