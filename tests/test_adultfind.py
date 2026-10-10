"""Private: find new scenes by what you mean, and follow studios/performers."""
import importlib
import json
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import adultfind as af

SCENE = {"id": "s1711", "title": "Dustin & Ryder: Bareback", "code": "1711", "date": "2013-12-22",
         "studio": "Sean Cody", "studio_id": "st-sc", "performers": ["Dustin", "Ryder"]}
GOOD = "[SeanCody.com] SC-1711 Dustin & Ryder: Bareback [2013, Anal Sex, Bareback, 1080p]"
JUNK = "[NewSensations.com] Ivy Wolfe, Dustin Daring - Tiny Ivy [2018, All Sex]"


def test_phrasings_spell_it_like_uploaders():
    assert af.phrasings("Sean Cody Dustin Ryder", None, {"Sean Cody"})[0] == "SeanCody Dustin Ryder"
    qs = af.phrasings("", SCENE)
    assert qs[:3] == ["SeanCody Dustin Ryder", "SeanCody 1711", "Dustin Ryder"] and "SeanCody 13.12.22" in qs
    assert len(qs) <= af.MAX_QUERIES
    assert af.studio_forms("Broke Straight Boys")[:2] == ["BrokeStraightBoys", "BrokeStraightBoys.com"]
    assert "BSB" in af.studio_forms("Broke Straight Boys")


def test_scoring_prefers_what_you_meant():
    good, why, hit = af.score(GOOD, "", SCENE)
    junk, _, junk_hit = af.score(JUNK, "", SCENE)
    assert good >= af.MIN_SCORE > junk and hit and not junk_hit and "Sean Cody" in why and "scene 1711" in why
    # you named a studio: another studio's release sharing its words ("broke", "straight") isn't it
    q, known = "broke straight boys beau", {"Broke Straight Boys"}
    bsb = af.score("[BrokeStraightBoys.com] Beau Takes Andy", q, None, known)
    brazzers = af.score("[Brazzers.com] Della - Ex-GF Goes For Broke, straight up", q, None, known)
    assert bsb[2] and bsb[0] >= 8 and not brazzers[2] and brazzers[0] <= 1
    bsb_scene = {**SCENE, "studio": "Broke Straight Boys", "performers": ["Beau", "Andy"], "code": ""}
    assert not af.score("Indecent Behavior III (Beau, Andy) [1995]", "", bsb_scene)[2]   # first names alone aren't it


class World:
    """Fake Prowlarr + Stash + StashDB."""
    def __init__(self):
        self.grabs, self.clients, self.searches, self.owned, self.stash_mutations = [], [{"id": 1}], [], set(), []

    def __call__(self, req):
        host, path = req.url.host, req.url.path
        body = json.loads(req.content) if req.content else {}
        if host == "prowlarr":
            if path.endswith("/tag"):
                return httpx.Response(200, json=[{"id": 3, "label": "xxx"}])
            if path.endswith("/indexer"):
                return httpx.Response(200, json=[{"id": 7, "name": "PornoLab", "enable": True, "tags": [3]},
                                                 {"id": 8, "name": "General", "enable": True, "tags": []}])
            if path.endswith("/downloadclient"):
                return httpx.Response(200, json=self.clients)
            if path.endswith("/search") and req.method == "GET":
                q = parse_qs(urlparse(str(req.url)).query)
                self.searches.append((q["query"][0], q.get("indexerIds")))
                hits = [{"guid": "g-good", "indexerId": 7, "indexer": "PornoLab", "title": GOOD, "size": 9.4e8, "seeders": 2}] \
                    if "SeanCody" in q["query"][0] or "Dustin Ryder" == q["query"][0] else []
                return httpx.Response(200, json=hits + [{"guid": "g-junk", "indexerId": 7, "indexer": "PornoLab", "title": JUNK,
                                                         "size": 3e9, "seeders": 9}])
            if path.endswith("/search") and req.method == "POST":
                self.grabs.append(body)
                return httpx.Response(200, json={})
        if host == "stash":
            q = body.get("query", "")
            if "stashBoxes" in q:
                return httpx.Response(200, json={"data": {"configuration": {"general": {"stashBoxes": [
                    {"endpoint": "https://stashdb.test/graphql", "name": "StashDB", "api_key": "k"}]}}}})
            if "scrapeSingleScene" in q:
                return httpx.Response(200, json={"data": {"scrapeSingleScene": [{
                    "title": SCENE["title"], "code": "1711", "date": SCENE["date"], "image": "https://img/x.jpg", "urls": [],
                    "remote_site_id": "s1711", "studio": {"name": "Sean Cody", "remote_site_id": "st-sc"},
                    "performers": [{"name": "Dustin"}, {"name": "Ryder"}]}]}})
            if "findScenes" in q:
                return httpx.Response(200, json={"data": {"findScenes": {"count": int(body["variables"]["id"] in self.owned)}}})
            self.stash_mutations += [m for m in ("metadataScan", "metadataIdentify") if m in q]
            return httpx.Response(200, json={"data": {}})
        if host == "stashdb.test":
            q = body.get("query", "")
            if "queryScenes" in q:
                return httpx.Response(200, json={"data": {"queryScenes": {"scenes": [
                    {"id": "new1", "title": "Dustin & Ryder: Bareback", "code": "1711", "date": time.strftime("%Y-%m-%d"),
                     "studio": {"name": "Sean Cody"}, "performers": [{"performer": {"name": "Dustin"}}, {"performer": {"name": "Ryder"}}]},
                    {"id": "have1", "title": "Old One", "code": "", "date": time.strftime("%Y-%m-%d"),
                     "studio": {"name": "Sean Cody"}, "performers": []},
                    {"id": "ancient", "title": "Ancient", "code": "", "date": "2001-01-01", "studio": {"name": "Sean Cody"}, "performers": []}]}}})
            if "searchStudio" in q:
                return httpx.Response(200, json={"data": {"searchStudio": [{"id": "st-sc", "name": "Sean Cody", "aliases": [], "parent": None}],
                                                          "searchPerformer": [{"id": "p1", "name": "Dustin Rhodes", "scene_count": 12}]}})
        return httpx.Response(404)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    sources = {"prowlarr": {"url": "http://prowlarr", "api_key": "k"},
               "stash": {"url": "http://stash", "api_key": "k", "downloads_path": "/downloads"}}
    real_source = main.cfg.source
    monkeypatch.setattr(main.cfg, "source", lambda key: sources.get(key) if key in sources else real_source(key))
    world = World()
    real = httpx.Client
    monkeypatch.setattr(af.httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(world), **k))
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/setup/options", json={"adult_enabled": True})
    me = admin.get("/api/me").json()
    admin.patch(f"/api/accounts/{me['id']}", json={"adult_allowed": True})
    admin.post("/api/adult/setup", json={"pin": "2468"})
    admin.post("/api/adult/unlock", json={"pin": "2468"})
    return main, admin, world


def test_needs_admin_and_unlocked(env):
    main, admin, world = env
    assert admin.get("/api/adult/status").json()["unlocked"]
    admin.post("/api/adult/lock")
    assert admin.post("/api/private/find", json={"q": "sean cody"}).status_code == 403
    assert admin.get("/api/private/follows").status_code == 403


def test_find_understands_and_ranks(env):
    main, admin, world = env
    scenes = admin.get("/api/private/find/understand", params={"q": "sean cody dustin"}).json()["scenes"]
    assert scenes[0]["studio_id"] == "st-sc" and scenes[0]["image"] == "https://img/x.jpg"
    r = admin.post("/api/private/find", json={"q": "Sean Cody Dustin Ryder", "scene": scenes[0]}).json()
    assert r["results"][0]["guid"] == "g-good" and r["results"][0]["match"]
    assert not next(x for x in r["results"] if x["guid"] == "g-junk")["match"]
    assert all(ids == ["7"] for _, ids in world.searches)          # only the adult indexers
    assert admin.post("/api/private/find/grab", json={"guid": "g-good", "indexer_id": 7, "title": GOOD}).json()["stash_scan"]
    assert world.grabs == [{"guid": "g-good", "indexerId": 7}]


def test_grab_without_a_download_client_says_what_to_do(env):
    main, admin, world = env
    world.clients = []
    r = admin.post("/api/private/find/grab", json={"guid": "g-good", "indexer_id": 7, "title": GOOD})
    assert r.status_code == 400 and "Download Clients" in r.json()["detail"]


def test_follow_downloads_new_scenes_once(env):
    main, admin, world = env
    st = str(main.STATE)
    assert admin.get("/api/private/follows/lookup", params={"q": "sean cody"}).json()["studios"][0]["id"] == "st-sc"
    af.follow(st, "studio", "st-sc", "Sean Cody")
    world.owned.add("have1")
    res = af.check_follows(main.cfg, st)
    assert res["new"] == 2 and res["grabbed"] == 1                 # 'ancient' predates the follow window
    status = {w["title"]: w["status"] for w in af.wanted_list(st)}
    assert status == {"Dustin & Ryder: Bareback": "downloading", "Old One": "have"}
    assert world.grabs == [{"guid": "g-good", "indexerId": 7}]
    af.check_follows(main.cfg, st)
    assert len(world.grabs) == 1                                   # never downloaded twice
    assert af.pending(st) == 1 and af.scan_and_identify(main.cfg)
    assert {"metadataScan", "metadataIdentify"} <= set(world.stash_mutations)
    listed = admin.get("/api/private/follows").json()
    assert listed["follows"][0]["name"] == "Sean Cody" and len(listed["wanted"]) == 2
    admin.delete("/api/private/follows/studio/st-sc")
    assert admin.get("/api/private/follows").json()["follows"] == []
