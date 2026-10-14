"""Private downloads that stop moving are replaced; moving, waiting and finished ones are left alone."""
import json
import time

import httpx

from app import adultfind as af
from app import stalls

H = 3600


class Cfg:
    def __init__(self, sources):
        self.sources = sources

    def source(self, name):
        return self.sources.get(name)


class Qbit:
    def __init__(self, torrents):
        self.torrents, self.deleted = torrents, []

    def __call__(self, req):
        if req.url.path.endswith("/torrents/info"):
            return httpx.Response(200, json=[t for t in self.torrents if t["hash"] not in {d["hashes"] for d in self.deleted}])
        if req.url.path.endswith("/torrents/delete"):
            self.deleted.append(dict(httpx.QueryParams(req.content.decode())))
            return httpx.Response(200)
        return httpx.Response(404)


def setup(tmp_path, monkeypatch, torrents):
    state = str(tmp_path / "state.db")
    q = Qbit(torrents)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(q), **k))
    monkeypatch.setattr(af, "work_wanted", lambda *a, **k: {"grabbed": 0, "searched": 0})
    return state, q, Cfg({"qbittorrent": {"url": "http://qbit", "category": "stash"}})


def test_stalled_ones_are_replaced_and_the_rest_left_alone(tmp_path, monkeypatch):
    now = time.time()
    torrents = [
        {"hash": "dead", "name": "[GAY] [sketchysex] deep_breed", "state": "metaDL", "progress": 0, "downloaded": 0, "added_on": now - 7 * H},
        {"hash": "moving", "name": "Moving.mp4", "state": "downloading", "progress": 0.2, "downloaded": 100, "added_on": now - 8 * H},
        {"hash": "done", "name": "Finished.mp4", "state": "stalledUP", "progress": 1, "downloaded": 900, "added_on": now - 30 * H},
        {"hash": "line", "name": "In line.mp4", "state": "queuedDL", "progress": 0, "downloaded": 0, "added_on": now - 30 * H},
        {"hash": "slow", "name": "Half there.mp4", "state": "stalledDL", "progress": 0.5, "downloaded": 500, "added_on": now - 30 * H},
    ]
    state, q, cfg = setup(tmp_path, monkeypatch, torrents)
    scene = {"id": "sc1", "title": "Deep Breed", "studio": "SketchySex", "performers": ["A"], "date": "2026-10-01"}
    con = af.grabs_con(state)
    with con:
        con.execute("INSERT INTO adult_grabs (guid, title, account_id, at, scene) VALUES ('g1', ?, 1, ?, ?)",
                    ("[GAY] [sketchysex] deep_breed", now - 7 * H, json.dumps(scene)))
    con.close()

    first = stalls.tick(cfg, state, now=now)
    assert [d["name"] for d in first] == ["[GAY] [sketchysex] deep_breed"]          # never got a byte in 7 h
    assert q.deleted == [{"hashes": "dead", "deleteFiles": "true"}]                # partial files go too
    assert "looking for another copy" in first[0]["action"]
    wanted = {w["scene_id"]: w for w in af.wanted_list(state)}
    assert wanted["sc1"]["status"] == "waiting" and wanted["sc1"]["stalled"] == 1   # back on the list
    grab = af.grabs_con(state).execute("SELECT hash, tidied, tidy_note FROM adult_grabs").fetchone()
    assert grab["hash"] == "dead" and grab["tidied"] and grab["tidy_note"].startswith("stalled")

    torrents[1]["downloaded"] = 400                                                # still moving
    later = stalls.tick(cfg, state, now=now + 7 * H)
    assert [d["name"] for d in later] == ["Half there.mp4"]                        # 7 h with nothing new
    assert {d["hashes"] for d in q.deleted} == {"dead", "slow"}                    # never "done" or "line"
    torrents[1]["downloaded"] = 700
    assert stalls.tick(cfg, state, now=now + 20 * H) == []                         # "moving" keeps growing; "line" waits
    assert [d["name"] for d in stalls.tick(cfg, state, now=now + 27 * H)] == ["Moving.mp4"]   # until it stops


def test_the_dead_copy_is_not_picked_again(tmp_path):
    found = {"results": [
        {"guid": "g1", "title": "[GAY] [sketchysex] deep_breed", "match": True, "score": 12, "seeders": 3},
        {"guid": "g2", "title": "[GAY] [SketchySex] Deep_Breed", "match": True, "score": 12, "seeders": 9},   # same release elsewhere
        {"guid": "g3", "title": "[SketchySex.com] Deep Breed [2026, Group, 1080p]", "match": True, "score": 11, "seeders": 2}]}
    assert af.best_release(found)["guid"] == "g2"
    assert af.best_release(found, [{"guid": "g1", "title": "[GAY] [sketchysex] deep_breed"}])["guid"] == "g3"


def test_gives_up_after_three_dead_copies(tmp_path):
    state = str(tmp_path / "state.db")
    scene = {"id": "sc9", "title": "Thirsty 4 Loads", "studio": "SketchySex"}
    assert [af.retry_scene(state, scene, f"g{i}", f"copy {i}") for i in range(3)] == [1, 2, 3]
    assert {w["scene_id"]: w["status"] for w in af.wanted_list(state)}["sc9"] == "not_found"
