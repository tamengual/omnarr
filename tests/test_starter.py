from app import indexer
from app.config import Config


def _cfg(raw):
    return Config(raw, state_path=":memory:")


def test_starter_lists_load_and_merge():
    s = indexer._starter()
    assert len(s["adaptations"]) > 50 and any(u["name"] == "Middle-earth" for u in s["universes"])
    mine = {"name": "Middle-earth", "series": ["Mine"]}
    unis = indexer._list(_cfg({"universes": [mine]}), "universes")
    assert unis[0] is mine
    assert sum(1 for u in unis if u["name"] == "Middle-earth") == 1          # user replaces starter
    assert indexer._list(_cfg({"starter_lists": False, "universes": [mine]}), "universes") == [mine]


def _work(title, series="", idx=None, author="Robin Hobb"):
    return {"kind": "book", "title": title, "series": series, "series_index": idx, "authors": [author], "universe": ""}


def test_universe_orders_series_as_listed():
    works = {
        "a": _work("Fool's Assassin", "Fitz and the Fool", 1),
        "b": _work("Assassin's Apprentice", "The Farseer Trilogy", 1),
        "c": _work("Ship of Magic", "The Liveship Traders", 1),
        "d": _work("Some Other Book", "Unrelated", 1),
    }
    indexer._assign_universes(_cfg({}), works)
    assert works["d"]["universe"] == ""
    order = sorted("abc", key=lambda k: works[k]["universe_index"])
    assert [works[k]["title"] for k in order] == ["Assassin's Apprentice", "Ship of Magic", "Fool's Assassin"]
    assert {works[k]["universe"] for k in "abc"} == {"Realm of the Elderlings"}


def test_adaptation_links_deduplicated():
    works = {"b1": {"kind": "book", "title": "Wool", "series": "Silo", "year": 2011},
             "s1": {"kind": "show", "title": "Silo", "series": "", "year": 2023}}
    links = indexer._adaptation_links(_cfg({"adaptations": [{"book": "Wool", "screen": "Silo"}]}), works)
    assert links == [("b1", "s1")]
