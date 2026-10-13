"""Finished Private downloads get their real details in Stash and a proper file name (renamed by qBittorrent)."""
import json
import sqlite3
import time

import httpx

from app import adultfind as af
from app import adulttidy as tidy


def test_reads_release_names():
    m = tidy.parse_release("[OnlyFans.com] Gymnast Double Penetration (James Ash (james ashxxx), James Cassidy, Bryce Jax) "
                           "[2025 ., Bareback, Threesome, DAP, 1080p]")
    assert m == {"studio": "OnlyFans", "title": "Gymnast Double Penetration", "performers": ["James Ash", "James Cassidy", "Bryce Jax"],
                 "date": "2025", "tags": ["Bareback", "Threesome", "DAP"]}
    m = tidy.parse_release("[OnlyFans.com] Rhyheim Shabazz, Romeo J (RomeoGotDick), Nick Floyd [2026 ., Bareback, 720p]")
    assert m["performers"] == ["Rhyheim Shabazz", "Romeo J", "Nick Floyd"] and m["title"] == "Rhyheim Shabazz, Romeo J, Nick Floyd"
    m = tidy.parse_release("[BrazzersExxtra.com / Brazzers.com] Della Cate - Ex-GF Goes For Broke [2026-10-04, Blowjob, 1080p]")
    assert (m["studio"], m["title"], m["performers"], m["date"]) == ("BrazzersExxtra", "Ex-GF Goes For Broke", ["Della Cate"], "2026-10-04")
    m = tidy.parse_release("BiGuysFuck.20.12.11.Dustin.Hazel.Marcus.Vonryder.XXX.SD.MP4-KLEENEX")
    assert (m["studio"], m["date"]) == ("BiGuysFuck", "2020-12-11") and m["title"].startswith("Dustin Hazel")
    m = tidy.parse_release("Gymnast Double Penetration - James Ash, James Cassidy & Bryce Jax (2025-06-09).mp4")
    assert (m["title"], m["date"], m["performers"]) == ("Gymnast Double Penetration", "2025-06-09", ["James Ash", "James Cassidy", "Bryce Jax"])
    assert tidy.parse_release("[FraternityX.com] Pledge [2014 ., Group Sex]")["tags"] == ["Group Sex"]


def test_file_names():
    meta = {"studio": "OnlyFans", "date": "2025", "title": "Gymnast Double Penetration", "performers": ["James Ash", "Bryce Jax"]}
    assert tidy.file_name(meta, ".mp4") == "OnlyFans - 2025 - Gymnast Double Penetration (James Ash, Bryce Jax).mp4"
    assert tidy.file_name({"title": 'a/b: "c"?', "performers": []}, ".mkv") == "a b c.mkv"
    assert len(tidy.file_name({"title": "x" * 400}, ".mp4")) <= 180
    many = {"studio": "S", "title": "T", "performers": list("ABCDEF")}
    assert tidy.file_name(many, ".mp4") == "S - T (A, B, C, D & more).mp4"


class Cfg:
    def __init__(self, sources):
        self.sources = sources

    def source(self, name):
        return self.sources.get(name)


class Apps:
    def __init__(self):
        self.renames, self.updates, self.created, self.scans = [], [], [], 0
        self.scenes = {"OnlyFans Gymnast.mp4": {"id": "61", "title": "", "date": None, "organized": False, "studio": None,
                                                 "performers": [], "stash_ids": [], "files": [{"path": "/downloads/OnlyFans Gymnast.mp4"}]},
                       "Sketchy Sex - Breeding Ground 1080p.mp4": {
                           "id": "63", "title": "BREEDING GROUND", "date": "2026-10-08", "organized": False,
                           "studio": {"name": "Sketchy Sex"}, "performers": [{"name": "Blake Rivers"}], "stash_ids": [{"stash_id": "x"}],
                           "files": [{"path": "/downloads/Sketchy Sex - Breeding Ground 1080p.mp4"}]}}

    def __call__(self, req):
        host, path = req.url.host, req.url.path
        if host == "qbit":
            if path.endswith("/torrents/info"):
                return httpx.Response(200, json=[
                    {"hash": "h1", "name": "OnlyFans Gymnast.mp4", "progress": 1, "added_on": time.time() - 60},
                    {"hash": "h2", "name": "Sketchy Sex - Breeding Ground 1080p.mp4", "progress": 1, "added_on": time.time() - 60},
                    {"hash": "h3", "name": "Still going.mp4", "progress": 0.4, "added_on": time.time()}])
            if path.endswith("/torrents/files"):
                name = {"h1": "OnlyFans Gymnast.mp4", "h2": "Sketchy Sex - Breeding Ground 1080p.mp4"}[req.url.params["hash"]]
                return httpx.Response(200, json=[{"name": name, "size": 9}, {"name": "cover.jpg", "size": 1}])
            if path.endswith("/torrents/renameFile"):
                self.renames.append(dict(httpx.QueryParams(req.content.decode())))
                return httpx.Response(200)
        if host == "stash":
            body = json.loads(req.content)
            q, v = body["query"], body.get("variables") or {}
            if "stashBoxes" in q:
                return httpx.Response(200, json={"data": {"configuration": {"general": {"stashBoxes": [{"endpoint": "https://stashdb.test/graphql", "api_key": ""}]}}}})
            if "findScenes" in q:
                assert v["p"].startswith('"') and v["p"].endswith('"')      # exact-phrase search, not any-word
                s = self.scenes.get(v["p"].strip('"'))
                return httpx.Response(200, json={"data": {"findScenes": {"scenes": [s] if s else []}}})
            for kind in ("Studios", "Performers", "Tags"):
                if f"find{kind}" in q:
                    return httpx.Response(200, json={"data": {f"find{kind}": {kind.lower(): []}}})
            for kind in ("studio", "performer", "tag"):
                if f"{kind}Create" in q:
                    self.created.append((kind, v["i"]["name"], v["i"].get("gender")))
                    return httpx.Response(200, json={"data": {f"{kind}Create": {"id": f"{kind}-{len(self.created)}"}}})
            if "sceneUpdate" in q:
                self.updates.append(v["i"])
                return httpx.Response(200, json={"data": {"sceneUpdate": {"id": v["i"]["id"]}}})
            if "metadataScan" in q:
                self.scans += 1
            if "scrapeSingleScene" in q:
                return httpx.Response(200, json={"data": {"scrapeSingleScene": []}})
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)


def test_tidy_renames_and_fills_in_stash(tmp_path, monkeypatch):
    state = str(tmp_path / "state.db")
    apps = Apps()
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(apps), **k))
    monkeypatch.setattr(af, "studio_hints", lambda cfg, q: {})
    monkeypatch.setattr(tidy.time, "sleep", lambda s: None)
    cfg = Cfg({"qbittorrent": {"url": "http://qbit"}, "stash": {"url": "http://stash", "api_key": "k", "downloads_path": "/downloads"}})
    af.set_prefs(state, True)
    con = af.grabs_con(state)
    with con:
        con.execute("INSERT INTO adult_grabs (guid, title, account_id, at) VALUES ('g1', ?, 1, ?)",
                    ("[OnlyFans.com] Gymnast Double Penetration (James Ash, Bryce Jax) [2025 ., Bareback, DAP, 1080p]", time.time() - 120))
        con.execute("INSERT INTO adult_grabs (guid, title, account_id, at) VALUES ('g2', ?, 1, ?)",
                    ("[SketchySex.com] Breeding Ground (Blake Rivers, Derek Spears) [2026 ., Group, 1080p]", time.time() - 100))
        con.execute("INSERT INTO adult_grabs (guid, title, account_id, at) VALUES ('g3', 'Still going', 1, ?)", (time.time(),))
    con.close()
    refreshed = []
    done = tidy.tidy(cfg, state, lambda: refreshed.append(1))

    names = {d["file"]: (d["name"], d["how"]) for d in done}
    assert names["OnlyFans Gymnast.mp4"] == ("OnlyFans - 2025 - Gymnast Double Penetration (James Ash, Bryce Jax).mp4", "the release name")
    assert names["Sketchy Sex - Breeding Ground 1080p.mp4"] == ("Sketchy Sex - 2026-10-08 - BREEDING GROUND (Blake Rivers).mp4", "StashDB (fingerprint)")
    assert {(r["hash"], r["newPath"]) for r in apps.renames} >= {("h1", "OnlyFans - 2025 - Gymnast Double Penetration (James Ash, Bryce Jax).mp4")}
    gym = next(u for u in apps.updates if u["id"] == "61")
    assert gym["title"] == "Gymnast Double Penetration" and gym["date"] == "2025-01-01" and len(gym["performer_ids"]) == 2
    assert ("performer", "James Ash", "MALE") in apps.created and ("tag", "DAP", None) in apps.created
    assert apps.scans == 1 and refreshed == [1]
    assert tidy.tidy(cfg, state, lambda: refreshed.append(1)) == []          # done once; the unfinished one waits
    con = af.grabs_con(state)
    assert [r[0] for r in con.execute("SELECT guid FROM adult_grabs WHERE tidied IS NULL")] == ["g3"]


def test_unfinished_download_is_not_swapped_for_a_finished_neighbour(tmp_path, monkeypatch):
    """Two grabs sent together: the one still downloading must wait, not take the other's finished file."""
    state = str(tmp_path / "state.db")
    apps = Apps()

    def handler(req):
        if req.url.host == "qbit" and req.url.path.endswith("/torrents/info"):
            return httpx.Response(200, json=[
                {"hash": "h1", "name": "Gymnast Double Penetration - James Ash, James Cassidy & Bryce Jax (2025-06-09).mp4",
                 "progress": 1, "added_on": time.time() - 60},
                {"hash": "h9", "name": "Rhyheim Shabazz, Romeo J, Nick Floyd.mp4", "progress": 0.5, "added_on": time.time() - 60}])
        return apps(req)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    cfg = Cfg({"qbittorrent": {"url": "http://qbit"}, "stash": {"url": "http://stash", "api_key": "k", "downloads_path": "/downloads"}})
    con = af.grabs_con(state)
    with con:
        con.execute("INSERT INTO adult_grabs (guid, title, account_id, at) VALUES ('g9', ?, 1, ?)",
                    ("[OnlyFans.com] Rhyheim Shabazz, Romeo J (RomeoGotDick), Nick Floyd [2026 ., Bareback, 720p]", time.time() - 60))
    con.close()
    assert tidy.tidy(cfg, state) == [] and apps.renames == [] and apps.updates == []


def test_qbittorrent_login_old_and_new_replies(monkeypatch):
    """A good login is 200 "Ok." on qBittorrent 4.x and an empty 204 on 5.x; a wrong one is 200 "Fails." or 401."""
    from app.connectors.registry import _test_qbittorrent
    real = httpx.Client

    def fake(login):
        def handler(req):
            p = req.url.path
            if p.endswith("/auth/login"):
                return login
            if p.endswith("/app/version"):
                return httpx.Response(200, text="v5.2.4")
            if p.endswith("/torrents/info"):
                return httpx.Response(200, json=[])
            return httpx.Response(404)
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))

    s = {"url": "http://qbit", "username": "admin", "password": "pw"}
    for good in (httpx.Response(204), httpx.Response(200, text="Ok.")):
        fake(good)
        ok, msg = _test_qbittorrent(s)
        assert ok and "v5.2.4" in msg
        with tidy.qbit(Cfg({"qbittorrent": s})) as c:
            assert c is not None
    for bad in (httpx.Response(401, text="Unauthorized"), httpx.Response(200, text="Fails.")):
        fake(bad)
        assert _test_qbittorrent(s) == (False, "qBittorrent didn't accept that login")
        try:
            with tidy.qbit(Cfg({"qbittorrent": s})):
                raise AssertionError("bad login accepted")
        except RuntimeError:
            pass
