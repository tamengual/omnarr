"""Plain-English Activity: why a download is where it is, when it arrives, what's coming, app health."""
import datetime

import httpx

from app import insight


def test_arrival_wording():
    assert insight._seconds("1.02:03:04") == 93784 and insight._seconds("00:40:00") == 2400 and insight._seconds(None) is None
    assert insight.arrives(30) == "any moment now"
    assert insight.arrives(2400) == "in about 40 min"
    assert insight.arrives(3 * 3600) == "in about 3 hours"
    assert insight.arrives(4 * 86400) == "in about 4 days"
    assert insight.arrives(None) == "" and insight.arrives(100 * 86400) == ""


def test_explanations():
    e = insight.explain
    note, stuck = e({"state": "importBlocked", "messages": ["Unable to determine if file is a sample"]}, "Sonarr")
    assert stuck and "couldn't add it to the library: Unable to determine if file is a sample" in note
    done = e({"state": "importBlocked", "messages": ["Episode file already imported at 10/11/2026 00:25:57"]}, "Sonarr")
    assert done == ("Already in your library. This is a leftover queue entry that can be cleared.", False)
    assert e({"state": "failedPending"}, "Radarr")[0].startswith("The download failed")
    assert e({"status": "downloadClientUnavailable"}, "Sonarr")[1]
    assert "delay setting" in e({"status": "delay"}, "Sonarr")[0]
    queued = e({"status": "queued"}, "Sonarr", {"state": "queuedDL", "line_place": 240, "line_length": 246})
    assert "240 of 246" in queued[0] and queued[1]
    dead = e({"status": "downloading"}, "Radarr", {"state": "stalledDL", "num_seeds": 0, "num_complete": 0})
    assert dead[1] and "nobody is sharing" in dead[0]
    ok = e({"status": "downloading"}, "Sonarr", {"state": "downloading", "num_seeds": 9, "num_complete": 23, "dlspeed": 2_000_000})
    assert ok == ("Downloading from 9 people.", False)
    assert e({"status": "downloading"}, "Sonarr") == ("Downloading.", False)


class Cfg:
    def __init__(self, sources):
        self.sources = sources

    def source(self, name):
        return self.sources.get(name)


def test_annotate_uses_qbittorrent(monkeypatch):
    monkeypatch.setattr(insight, "qbit_torrents", lambda cfg: {
        "AAA": {"state": "downloading", "num_seeds": 3, "num_complete": 5, "dlspeed": 500_000, "eta": 1800},
        "BBB": {"state": "queuedDL", "line_place": 3, "line_length": 10}})
    out = {"sonarr": {"records": [{"download_id": "aaa", "status": "downloading", "eta": "05:00:00"},
                                  {"download_id": "bbb", "status": "queued", "eta": "01:00:00"}]}}
    insight.annotate(Cfg({}), out)
    a, b = out["sonarr"]["records"]
    assert a["arrives"] == "in about 30 min" and not a["stuck"]                 # qBittorrent's live estimate wins
    assert "3 of 10" in b["note"] and b["arrives"] == ""                         # no estimate while waiting in line


def test_coming_up_and_health(monkeypatch):
    today = datetime.date.today()
    soon = (today + datetime.timedelta(days=2)).isoformat()

    def handler(req):
        if req.url.path.endswith("/calendar") and req.url.host == "sonarr":
            return httpx.Response(200, json=[
                {"airDateUtc": soon + "T01:00:00Z", "seasonNumber": 2, "episodeNumber": 3, "title": "The One", "hasFile": False,
                 "monitored": True, "series": {"title": "Severance"}},
                {"airDateUtc": soon + "T01:00:00Z", "seasonNumber": 2, "episodeNumber": 2, "title": "Had", "hasFile": True,
                 "monitored": True, "series": {"title": "Severance"}}])
        if req.url.path.endswith("/calendar"):
            return httpx.Response(200, json=[{"title": "Dune Part Three", "digitalRelease": today.isoformat() + "T00:00:00Z",
                                              "hasFile": False, "monitored": True}])
        if req.url.path.endswith("/health") and req.url.host == "sonarr":
            return httpx.Response(200, json=[{"type": "warning", "message": "Indexers unavailable", "wikiUrl": "https://wiki"}])
        if req.url.path.endswith("/health") and req.url.host == "prowlarr":
            return httpx.Response(200, json=[{"type": "error", "message": "All indexers are unavailable"}])
        return httpx.Response(200, json=[])
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    cfg = Cfg({"sonarr": {"url": "http://sonarr", "api_key": "k"}, "radarr": {"url": "http://radarr", "api_key": "k"},
               "prowlarr": {"url": "http://prowlarr", "api_key": "k"}})
    coming = insight.coming_up(cfg)
    assert [(c["title"], c["detail"]) for c in coming] == [("Dune Part Three", "streaming / digital"), ("Severance", "S02E03 · The One")]
    h = insight.health(cfg)
    assert h[0] == {"app": "Prowlarr", "level": "error", "message": "All indexers are unavailable", "link": ""}
    assert h[1]["app"] == "Sonarr" and h[1]["link"] == "https://wiki"
