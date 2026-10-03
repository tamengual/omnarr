"""Setup wizard API: secrets are masked, kept on re-save, and tests run before saving."""
import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    c = TestClient(main.app)
    assert c.post("/api/auth/setup", json={"password": "correct horse"}).status_code == 200
    return c, main


def test_requires_login(tmp_path, monkeypatch, client):
    c, main = client
    assert TestClient(main.app).get("/api/setup/apps").status_code == 401


def test_save_masks_and_keeps_secret(client, monkeypatch):
    c, main = client
    assert c.get("/api/auth/status").json()["connections_needed"] is True
    seen = []
    monkeypatch.setitem(main.registry.BY_KEY["sonarr"], "test", lambda s: (seen.append(dict(s)), (True, "ok"))[1])
    r = c.post("/api/setup/apps/sonarr", json={"values": {"url": "http://sonarr:8989", "api_key": "abcdef123456"}})
    assert r.json()["ok"]
    apps = {a["key"]: a for a in c.get("/api/setup/apps").json()["apps"]}
    assert apps["sonarr"]["values"]["api_key"] == "••••3456"
    assert "abcdef" not in c.get("/api/setup/apps").text
    # re-save with the masked value: the stored key is kept
    c.post("/api/setup/apps/sonarr", json={"values": {"url": "http://sonarr:8990", "api_key": "••••3456"}})
    assert main.cfg.source("sonarr")["api_key"] == "abcdef123456"
    assert main.cfg.source("sonarr")["url"] == "http://sonarr:8990"
    assert seen[-1]["api_key"] == "abcdef123456"


def test_failed_test_is_not_saved(client, monkeypatch):
    c, main = client
    monkeypatch.setitem(main.registry.BY_KEY["radarr"], "test", lambda s: (False, "API key rejected"))
    r = c.post("/api/setup/apps/radarr", json={"values": {"url": "http://radarr:7878", "api_key": "x"}})
    assert r.status_code == 400 and main.cfg.source("radarr") is None
    r = c.post("/api/setup/apps/radarr", json={"values": {"url": "http://radarr:7878"}})
    assert "Missing" in r.json()["message"]


def test_adult_off_by_default(client):
    c, main = client
    assert c.get("/api/auth/status").json()["adult_enabled"] is False
    assert c.post("/api/adult/unlock", json={"pin": "1234"}).status_code == 404
    c.post("/api/setup/options", json={"adult_enabled": True})
    assert c.get("/api/auth/status").json()["adult_enabled"] is True
