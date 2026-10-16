"""Suggestions: per-person, explainable, never adult, owner's screens count as watched."""
import importlib
import json
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

RECENT = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400 * 10))


def _w(wid, kind, title, authors=(), series="", idx=None, genres=(), adult=0, universe=""):
    return (wid, kind, title, title.lower(), json.dumps(list(authors)), "[]", series, idx, 2020, "", json.dumps(list(genres)),
            "[]", "", None, "2026-01-01", None, "unread", None, 0, adult, None, "[]", "[]", "[]", universe, None,
            series.lower() if series else "", "{}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    conf = tmp_path / "config.yml"
    conf.write_text(f"index:\n  path: {tmp_path / 'index.db'}\n  state: {tmp_path / 'state.db'}\n".replace("\\", "/"))
    monkeypatch.setenv("OMNARR_CONFIG", str(conf))
    sys.modules.pop("app.main", None)
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "_reindex", lambda: None)
    from app import indexer
    idx = sqlite3.connect(tmp_path / "index.db")
    idx.executescript(indexer.SCHEMA)
    cols = [r[1] for r in idx.execute("PRAGMA table_info(works)")]
    rows = [
        _w("wool", "book", "Wool", ["Hugh Howey"], "Silo", 1, ["Science Fiction"]),
        _w("shift", "book", "Shift", ["Hugh Howey"], "Silo", 2, ["Science Fiction"]),
        _w("dust", "book", "Dust", ["Hugh Howey"], "Silo", 3, ["Science Fiction"]),
        _w("sand", "book", "Sand", ["Hugh Howey"], "", None, ["Science Fiction"]),
        _w("dune", "book", "Dune", ["Frank Herbert"], "", None, ["Science Fiction"]),
        _w("cook", "book", "Weeknight Dinners", ["A Chef"], "", None, ["Cooking"]),
        _w("arrival", "movie", "Arrival", [], "", None, ["Science Fiction"]),
        _w("spicy", "book", "Spicy", ["Hugh Howey"], "", None, ["Science Fiction"], adult=1),
    ]
    idx.executemany(f"INSERT INTO works ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    idx.execute("INSERT INTO editions (work_id, unit_key, source, source_id, title, extra) VALUES ('wool','calibre:1','calibre','1','Wool', ?)",
                (json.dumps({"my_rating": 10}),))
    idx.execute("INSERT INTO editions (work_id, unit_key, source, source_id, title, extra) VALUES ('arrival','radarr:1','radarr','1','Arrival', ?)",
                (json.dumps({"ids": {"tmdb": 329865}}),))
    idx.execute("INSERT INTO user_progress VALUES (1, 'wool', 1.0, 'finished', ?)", (RECENT,))
    idx.commit(); idx.close()
    admin = TestClient(main.app)
    admin.post("/api/auth/setup", json={"username": "owner", "password": "admin-pass-1"})
    admin.post("/api/accounts", json={"username": "guest", "password": "guest-pass-1", "can_request": False})
    guest = TestClient(main.app)
    guest.post("/api/auth/login", json={"username": "guest", "password": "guest-pass-1"})
    # outside lookups: TMDB recommendations for Arrival, and Wikidata related works
    monkeypatch.setattr(main.requests_, "seerr_recommendations", lambda cfg, st, kind, tmdb: [
        {"kind": "movie", "tmdb": 157336, "title": "Interstellar", "year": 2014, "poster": "p", "status": "not_requested"},
        {"kind": "movie", "tmdb": 329865, "title": "Arrival", "year": 2016, "poster": "p", "status": "available"}])
    monkeypatch.setattr(main, "_related_for_row", lambda row: [
        {"kind": "tv", "tmdb": 125988, "label": "Silo", "year": 2023, "authors": [], "url": "u", "wikidata": "Q1"}] if row["id"] == "wool" else [])
    main.cfg.save_connection("seerr", {"url": "http://seerr", "api_key": "k"})
    # Open Library: subjects per book, an author's best-known works, books on a subject
    from app import taste
    monkeypatch.setattr(taste, "PAUSE", 0)
    subjects = {"Wool": ["Dystopias", "Fiction, dystopian", "Totalitarianism"], "Shift": ["Dystopias", "Totalitarianism"],
                "Dust": ["Dystopias", "Science fiction"], "Sand": ["Dystopias"], "Dune": ["Science fiction", "Messiahs"],
                "Weeknight Dinners": ["Cooking"]}
    def ol(path, params, timeout=15):
        if "title" in params:
            return {"docs": [{"title": params["title"], "author_name": [params.get("author") or "Someone"],
                              "subject": subjects.get(params["title"], [])},
                             {"title": params["title"], "author_name": ["Somebody Else"], "subject": ["Cooking"]}]}   # same title, other author
        if params.get("author") == "Hugh Howey":
            return {"docs": [{"title": "Beacon 23", "author_name": ["Hugh Howey"], "first_publish_year": 2015, "edition_count": 9,
                              "subject": ["Science fiction", "Space"], "key": "/works/OL1W"},
                             {"title": "Wool Omnibus", "author_name": ["Hugh Howey"], "first_publish_year": 2012, "edition_count": 30},
                             {"title": "Wool", "author_name": ["Hugh Howey"], "first_publish_year": 2011, "edition_count": 40},
                             {"title": "Machine Learning", "author_name": ["Hugh Howey"], "first_publish_year": 2030, "edition_count": 5}]}
        if params.get("subject") in ("dystopias", "totalitarianism"):
            return {"docs": [{"title": "Nineteen Eighty-Four", "author_name": ["George Orwell"], "first_publish_year": 1949, "edition_count": 900,
                              "subject": ["Totalitarianism", "Dystopias"], "key": "/works/OL2W", "cover_i": 7},
                             {"title": "Divergent", "author_name": ["Veronica Roth"], "first_publish_year": 2011, "edition_count": 200,
                              "subject": ["Dystopias", "Chosen one"]}]}
        return {"docs": []}
    monkeypatch.setattr(taste, "_ol", ol)
    return main, admin, guest


def _sections(client, main, aid):
    main._suggest_online(aid, aid == 1)                  # run the background part in-line
    return {s["key"]: s["items"] for s in client.get("/api/suggestions").json()["sections"]}


def test_owner_suggestions_are_explained(env):
    main, admin, guest = env
    s = _sections(admin, main, 1)
    up = s["up_next"]
    assert up[0]["work"]["id"] == "shift" and up[0]["reason"] == "Next in Silo after Wool"
    lib = {c["work"]["id"]: c["reason"] for c in s["library"]}
    assert lib["sand"] == "Because you read Wool"                       # same author
    assert "dune" in lib and "cook" not in lib                          # genre yes, unrelated no
    assert "arrival" not in lib and "spicy" not in lib                  # seen screens, adult: never
    screen = s["screen"]
    assert [x["external"]["title"] for x in screen] == ["Interstellar"]   # owned Arrival filtered out
    assert screen[0]["reason"] == "Because of Arrival"
    assert [x["external"]["title"] for x in s["worlds"]] == ["Silo"] and s["worlds"][0]["reason"] == "From the world of Wool"


def test_taste_library_is_read_authors_and_themes(env):
    main, admin, guest = env
    assert admin.post("/api/me/taste", json={"library_is_read": True, "more": "totalitarian", "less": "chosen one"}).json()["library_is_read"]
    s = _sections(admin, main, 1)
    assert "library" not in s                                           # everything owned now counts as read
    authors = s["authors"]
    assert [a["external"]["title"] for a in authors] == ["Beacon 23"]   # not owned Wool, not the omnibus, not unreleased
    assert authors[0]["reason"].startswith("More from Hugh Howey: you have 4 of theirs")
    themes = {t["external"]["title"]: t["reason"] for t in s["themes"]}
    assert "Nineteen Eighty-Four" in themes and "Divergent" not in themes   # "less of: chosen one" drops it
    assert themes["Nineteen Eighty-Four"].startswith(("About dystopia, like", "About totalitarianism, like"))
    assert "Silo" in themes["Nineteen Eighty-Four"]                      # a series is one example, by its name
    assert "cooking" not in {t["term"] for t in admin.get("/api/me/taste").json()["profile"]["themes"]}   # one cookbook isn't a theme
    me = admin.get("/api/me/taste").json()
    assert me["more"] == "totalitarian" and me["profile"]["known"] >= 4
    assert "authors" not in _sections(guest, main, 2)                   # guests: their own taste, nothing yet


def test_taste_scoring_and_notes():
    from app import taste
    assert taste.clean(["Fiction, dystopian", "nyt:hardcover-fiction=2011", "Large type books", "Political fiction",
                        "Accessible book", "Human-alien encounters", "Voyages dans le temps"]) == ["dystopia", "political", "first contact"]
    assert taste.QUERY["first contact"] == "human-alien encounters"     # searched by Open Library's own name
    seeds = {"a": (1.0, "Wool", ["dystopias", "space"]), "b": (1.0, "Silo", ["dystopias"]), "c": (1.0, "Dune", ["messiahs", "space", "dystopias"])}
    lib = [ts for _, _, ts in seeds.values()] + [["cooking"]] * 3
    prof = taste.profile(seeds, lib)
    assert set(prof["weights"]) == {"dystopias", "space"}                # in at least two of yours
    fit = taste.scorer(prof, {"more": "messiah", "less": "chosen one\nromance"})
    assert fit(["dystopias"])[0] > fit(["space"])[0] > fit(["cooking"])[0] == 0
    assert fit(["messiahs"])[0] > 0                                      # "more of" lifts it
    assert fit(["Chosen One", "dystopias"]) is None and fit(["paranormal romance"]) is None
    assert fit(["roman empire"]) is not None and fit(["romans"]) is not None   # "romance" doesn't catch Rome


def test_open_library_outage_fails_fast(monkeypatch):
    from app import taste
    calls = []
    class Down:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k):
            calls.append(1)
            raise taste.httpx.ConnectTimeout("down")
    monkeypatch.setattr(taste.httpx, "Client", Down)
    monkeypatch.setattr(taste, "PAUSE", 0)
    monkeypatch.setattr(taste, "_ol_state", {"fails": 0, "down_until": 0.0})
    import tempfile, os
    state = os.path.join(tempfile.mkdtemp(), "s.db")
    wanted = [(f"book:{i}", "book", f"Book {i}", ["A Writer"], None) for i in range(50)]
    assert taste.fill(None, state, wanted, budget=50) == 0
    assert len(calls) == 3                                               # three strikes, then it stops for a while


def test_open_library_lookups_pick_the_right_book(tmp_path, monkeypatch):
    from app import suggest, taste
    monkeypatch.setattr(taste, "PAUSE", 0)
    asked = []
    def ol(path, params, timeout=15):
        asked.append(params)
        if params.get("subject"):
            return {"docs": []}
        return {"docs": [{"title": "The Lost Colony", "author_name": ["Eoin Colfer"], "subject": ["Fairies", "Leprechauns"]},
                         {"title": "The Lost Colony: The Long Winter Trilogy", "author_name": ["A. G. Riddle"], "subject": ["Climate change"]}]}
    monkeypatch.setattr(taste, "_ol", ol)
    assert taste.fetch_book("The Lost Colony", ["A.G. Riddle"]) == ["climate change"]     # never another author's same-titled book
    assert taste.short_title("Designated Targets: A Novel of the Axis of Time") == "Designated Targets"
    assert taste.short_title("Lightning Thief, The") == "The Lightning Thief"
    taste.subject_books(str(tmp_path / "s.db"), "first contact")
    assert asked[-1]["subject"] == "human-alien encounters"                               # Open Library's own wording
    works = {"x": {"id": "x", "kind": "book", "title": "Matter", "authors": json.dumps(["Iain M. Banks"])}}
    books = [{"title": "Excession", "authors": ["Iain M. Banks"], "year": 1996, "editions": 40},
             {"title": "Best of 1996", "authors": ["Gardner Dozois", "Iain M. Banks"], "year": 1996, "editions": 9},
             {"title": "Mи", "authors": ["Iain M. Banks"], "year": 1999, "editions": 9}]
    out = suggest.more_by_authors(works, {"x": 2.0}, lambda a: books, lambda it: None)
    assert [o["external"]["title"] for o in out] == ["Excession"]                          # not someone else's anthology, English only


def test_guests_get_their_own_suggestions(env):
    main, admin, guest = env
    s = _sections(guest, main, 2)
    assert "screen" not in s and "up_next" not in s                     # no history, and the owner's screens aren't theirs


def test_variety_released_and_language_rules():
    from app import suggest
    works = {"got": {"id": "got", "kind": "book", "title": "A Game of Thrones", "series_key": "", "authors": "[]"}}
    seeds = {"got": 1.0}
    related = [{"kind": "book", "label": f"Tale {i}", "year": 2000 + i, "wikidata": f"Q{i}"} for i in range(6)]
    related += [{"kind": "book", "label": "The Winds of Winter", "year": None, "wikidata": "Qw"},
                {"kind": "book", "label": "Future Book", "year": suggest.THIS_YEAR + 2, "wikidata": "Qf"},
                {"kind": "tv", "label": "Kampen om jerntronen", "year": 2012, "wikidata": "Qn"}]   # no TMDB id
    out = suggest.worlds(works, seeds, lambda w: related, lambda it: None)
    titles = [o["external"]["title"] for o in out]
    assert titles == ["Tale 0", "Tale 1", "Tale 2"]                     # three per title, released only
    recs = lambda kind, tmdb: [{"kind": "movie", "tmdb": n, "title": f"M{n}", "year": 2010} for n in range(6)]
    picks = suggest.screen_picks({}, {}, recs, lambda it: None, [("a", "movie", "1", 1.0, "Revenge of the Sith")])
    assert len(picks) == 3                                              # not a whole row from one film
    assert suggest.FOREIGN.search("Onyx Storm - Version française: The Empyrean Tome 3")
    assert not suggest.FOREIGN.search("Onyx Storm")
