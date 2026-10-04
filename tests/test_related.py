"""Related-works lookup (Wikidata) — parsed from canned SPARQL JSON; CI never calls Wikidata."""
import sqlite3

from app import requests_


def row(item, label, **kw):
    b = {"item": {"value": f"http://www.wikidata.org/entity/{item}"}, "itemLabel": {"value": label}}
    for k, v in kw.items():
        b[k] = {"value": v}
    return b


SEED = "http://www.wikidata.org/entity/Q11572"
AVATAR = {"results": {"bindings": [
    {**row("Q11572", "Avatar: The Last Airbender", typeLabel="animated television series", tmdbt="246"), "seed": {"value": SEED}},
    {**row("Q1", "The Legend of Korra", typeLabel="animated television series", tmdbt="33880", d="2012-04-14T00:00:00Z"), "seed": {"value": SEED}},
    {**row("Q2", "Avatar: The Last Airbender", typeLabel="television series", tmdbt="82452", d="2024-02-22T00:00:00Z"), "seed": {"value": SEED}},
    {**row("Q3", "The Last Airbender", typeLabel="film", tmdbm="10196", d="2010-07-01T00:00:00Z"), "seed": {"value": SEED}},
    {**row("Q4", "Avatarian", typeLabel="fictional writing system"), "seed": {"value": SEED}},
    {**row("Q5", "Chronicles of the Avatar", typeLabel="novel series"), "seed": {"value": SEED}},
    {**row("Q6", "Avatar: The Last Airbender – The Promise", typeLabel="literary work", formLabel="graphic novel", d="2012-01-01T00:00:00Z"), "seed": {"value": SEED}},
    {**row("Q7", "The Rise of Kyoshi", typeLabel="literary work", d="2019-07-16T00:00:00Z", authorLabel="F. C. Yee"), "seed": {"value": SEED}},
    {**row("Q7", "The Rise of Kyoshi", typeLabel="novel", d="2019-07-16T00:00:00Z", authorLabel="F. C. Yee"), "seed": {"value": SEED}},
    {**row("Q8", "Zuko", typeLabel="animated character"), "seed": {"value": SEED}},
    {**row("Q9", "Avatarr Parody", typeLabel="novel", genreLabel="parody"), "seed": {"value": SEED}},
    {**row("Q10", "Q10", typeLabel="film"), "seed": {"value": SEED}},
    {**row("Q11", "Avatar films", typeLabel="film series"), "seed": {"value": SEED}},
    {**row("Q12", "Avatar: The Last Airbender", typeLabel="video game", igdb="avatar-the-last-airbender", d="2006-10-10T00:00:00Z"), "seed": {"value": SEED}},
]}}


def test_parse_related_avatar():
    items = requests_.parse_related(AVATAR)
    got = {(i["kind"], i["label"], i["year"]) for i in items}
    assert ("tv", "The Legend of Korra", 2012) in got
    assert ("tv", "Avatar: The Last Airbender", 2024) in got           # live-action, via "based on"
    assert ("movie", "The Last Airbender", 2010) in got
    assert ("comic", "Avatar: The Last Airbender – The Promise", 2012) in got
    assert ("book", "The Rise of Kyoshi", 2019) in got
    assert ("game", "Avatar: The Last Airbender", 2006) in got
    labels = {i["label"] for i in items}
    for noise in ("Avatarian", "Chronicles of the Avatar", "Zuko", "Avatarr Parody", "Q10", "Avatar films"):
        assert noise not in labels
    assert len([i for i in items if i["label"] == "The Rise of Kyoshi"]) == 1  # duplicate rows merged
    assert not any(i["tmdb"] == "246" for i in items)                       # the seed itself is left out
    kyoshi = next(i for i in items if i["label"] == "The Rise of Kyoshi")
    assert kyoshi["authors"] == ["F. C. Yee"] and kyoshi["url"].endswith("/Q7")


def test_seed_clauses_are_safe():
    assert requests_.seed_from_ids("show", {"tmdb": "246", "imdb": "tt0417299", "tvdb": "74852"}).count("UNION") == 2
    assert requests_.seed_from_ids("show", {"tmdb": '1" } ?x ?y ?z . {'}) is None   # injection attempt dropped
    assert requests_.seed_from_ids("game", {"igdb": "1"}) is None
    clause = requests_.seed_from_book("Harry Potter and the Philosopher's Stone", "rowling")
    assert "Philosopher\\u2019s" in clause and '"mul"' in clause      # JSON-escaped; valid SPARQL


def test_related_caches_and_remembers_failures(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE settings (k TEXT PRIMARY KEY, v TEXT)")
    calls = []

    class R:
        def raise_for_status(self): pass
        def json(self): return AVATAR

    monkeypatch.setattr(requests_.httpx, "get", lambda *a, **k: calls.append(1) or R())
    first = requests_.related(con, "?seed wdt:P4983 \"246\" .", "show:tmdb=246")
    again = requests_.related(con, "?seed wdt:P4983 \"246\" .", "show:tmdb=246")
    assert first == again and len(calls) == 1

    def boom(*a, **k):
        calls.append(1)
        raise TimeoutError("slow")
    monkeypatch.setattr(requests_.httpx, "get", boom)
    assert requests_.related(con, "x", "huge") == [] and requests_.related(con, "x", "huge") == []
    assert len(calls) == 2                                                # failure remembered, not retried


def test_match_library():
    from app import main
    by_tmdb = {("tv", "33880"): "korra"}
    by_title = {("book", "riseofkyoshi"): [("kyoshi", {"yee"})], ("book", "dune"): [("dune", {"herbert"})]}
    assert main._match_library({"kind": "tv", "tmdb": "33880", "label": "x"}, by_tmdb, by_title) == "korra"
    assert main._match_library({"kind": "book", "label": "The Rise of Kyoshi", "authors": ["F. C. Yee"]}, by_tmdb, by_title) == "kyoshi"
    assert main._match_library({"kind": "book", "label": "Dune", "authors": ["Someone Else"]}, by_tmdb, by_title) is None
    # a screen with a TMDB id never falls back to a look-alike title
    by_title[("movie", "thelastairbender")] = [("animated-show", set())]
    assert main._match_library({"kind": "movie", "tmdb": "10196", "label": "The Last Airbender"}, by_tmdb, by_title) is None


def test_lookup_titles_include_subtitle():
    from app import normalize
    assert "The Reckoning of Roku" in normalize.lookup_titles("Avatar, the Last Airbender: The Reckoning of Roku")
