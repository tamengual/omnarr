"""Big visual movies move to the 4K profile when added; everything else is left alone, each movie once."""
import json

import httpx

from app import auto4k


def movie(mid, title, year, genres, votes, popularity=5, added="2026-10-12T00:00:00Z", profile=4):
    return {"id": mid, "title": title, "year": year, "genres": genres, "ratings": {"tmdb": {"votes": votes}},
            "popularity": popularity, "added": added, "qualityProfileId": profile, "monitored": True}


def test_which_movies_qualify():
    q = lambda *a, **k: auto4k.qualifies(movie(1, *a, **k), this_year=2026)[0]
    assert q("Avatar", 2009, ["Action", "Adventure", "Science Fiction"], 34793)
    assert q("Barbie", 2023, ["Adventure", "Comedy", "Fantasy"], 11656)
    assert q("Dune: Part Three", 2026, ["Adventure", "Science Fiction"], 0, popularity=80)      # new, not voted on yet
    assert not q("Hocus Pocus", 1993, ["Comedy", "Family", "Fantasy"], 3389)                    # too old
    assert not q("Battlestar Galactica: The Plan", 2009, ["Action", "Science Fiction"], 503)     # not a big release
    assert not q("The Big Short", 2015, ["Comedy", "Drama"], 10156)                              # not a visual genre


class Radarr:
    def __init__(self, movies):
        self.movies, self.puts, self.commands = movies, [], []

    def __call__(self, req):
        p = req.url.path
        if p.endswith("/qualityprofile"):
            return httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}])
        if p.endswith("/movie") and req.method == "GET":
            return httpx.Response(200, json=self.movies)
        if "/movie/" in p and req.method == "PUT":
            self.puts.append(json.loads(req.content))
            return httpx.Response(202, json={})
        if p.endswith("/command"):
            self.commands.append(json.loads(req.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)


class Cfg:
    def __init__(self, s):
        self.s = s

    def source(self, name):
        return self.s if name == "radarr" else None


def test_run_moves_new_big_movies_once(tmp_path, monkeypatch):
    state = str(tmp_path / "state.db")
    fake = Radarr([movie(1, "Old Library Film", 2014, ["Science Fiction"], 9000, added="2020-01-01T00:00:00Z"),
                   movie(2, "Avatar 4", 2029, ["Science Fiction"], 50000, added="2099-01-01T00:00:00Z"),
                   movie(3, "Mean Girls 3", 2029, ["Comedy"], 50000, added="2099-01-01T00:00:00Z")])
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, transport=httpx.MockTransport(fake), **k))
    cfg = Cfg({"url": "http://radarr", "api_key": "k", "uhd_profile": "ultra-hd"})
    moved = auto4k.run(cfg, state)
    assert [m["title"] for m in moved] == ["Avatar 4"]                      # added before the rule: untouched
    assert fake.puts[0]["qualityProfileId"] == 5 and fake.commands == [{"name": "MoviesSearch", "movieIds": [2]}]
    assert auto4k.run(cfg, state) == []                                    # once per movie: moving it back by hand sticks
    assert auto4k.run(Cfg({"url": "http://radarr", "api_key": "k"}), state) == []      # off when no profile named
