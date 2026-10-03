from app import normalize
from app.connectors.base import Unit
from app.indexer import build_work, group_books


def U(source, sid, title, author="Hugh Howey", **kw):
    fmt = {"calibre": "ebook", "abs": "audiobook", "storyteller": "readalong"}[source]
    return Unit(source=source, source_id=sid, kind="book", format=fmt, title=title, authors=[author], **kw)


def groups_of(g):
    return sorted(sorted(x) for x in g.groups())


def test_normalise_series_suffix():
    assert normalize.same_book("Wool (Silo)", "Howey, Hugh", "Wool: The Silo Saga, Book 1", "Hugh Howey") >= 0.9


def test_different_books_same_series_stay_apart():
    assert normalize.same_book("Foundation", "Isaac Asimov", "Second Foundation", "Isaac Asimov") < 0.9


def test_storyteller_exact_keys_join_all_three():
    units = [
        U("calibre", "1", "Wool (Silo)", match={"epub_names": ["Wool (Silo) - Hugh Howey.epub"]}),
        U("abs", "a1", "Wool", match={"inodes": ["953"]}),
        U("storyteller", "s1", "Wool", match={"inodes": ["953"], "epub_names": ["Wool (Silo) - Hugh Howey.epub"]}),
    ]
    g, _ = group_books(units, [], [], [])
    assert groups_of(g) == [["abs:a1", "calibre:1", "storyteller:s1"]]


def test_never_two_units_from_one_app():
    units = [U("calibre", "1", "Wool"), U("calibre", "2", "Wool"), U("abs", "a1", "Wool")]
    g, _ = group_books(units, [], [], [])
    sizes = sorted(len(x) for x in g.groups())
    assert sizes == [1, 2]                      # one ebook pairs with the audiobook, the other stays alone


def test_surname_must_agree():
    units = [U("calibre", "1", "Dune", "Frank Herbert"), U("abs", "a1", "Dune", "Brian Herbert-Smith")]
    g, _ = group_books(units, [], [], [])
    assert len(g.groups()) == 2


def test_split_override_blocks_auto_merge():
    units = [U("calibre", "1", "Wool"), U("abs", "a1", "Wool")]
    g, _ = group_books(units, [], [], [("abs:a1", "", "split")])
    assert len(g.groups()) == 2


def test_bookbridge_link_beats_fuzzy():
    units = [U("calibre", "399", "Wicked: The Inspiration for the Smash Broadway Musical", "Gregory Maguire"),
             U("abs", "w1", "Wicked", "Gregory Maguire")]
    g, _ = group_books(units, [("abs:w1", "calibre:399")], [], [])
    assert len(g.groups()) == 1


def test_display_titles():
    d = normalize.display_title
    assert d("Expanse 03 - Abaddon’s Gate", "The Expanse") == "Abaddon’s Gate"
    assert d("Forward the Foundation (The Foundation Series: Prequels, Book 2)", "Foundation") == "Forward the Foundation"
    assert d("Babylon's Ashes: The Expanse, Book 6 (Unabridged)", "The Expanse") == "Babylon's Ashes"
    assert d("Wool (Silo)", "Silo") == "Wool"
    assert d("The Fires of Heaven: Book Five of The Wheel of Time", "The Wheel of Time") == "The Fires of Heaven"
    assert d("Star Wars: Heir to the Empire") == "Star Wars: Heir to the Empire"
    assert d("Catch-22 (50th Anniversary Edition)") == "Catch-22 (50th Anniversary Edition)"
    assert d("Fahrenheit 451") == "Fahrenheit 451"
    assert d("Wool (Silo)", "Silo Saga") == "Wool"
    assert d("The Hobbit (Middle-Earth Universe)", "") == "The Hobbit"
    assert normalize.lookup_titles("The Handmaid's Tale: Special Edition")[-1] == "The Handmaid's Tale"


def test_universe_order_and_series_fallback():
    from app.config import Config
    from app.indexer import _assign_universes
    cfg = Config({"universes": [{"name": "Foundation Universe", "author": "Isaac Asimov",
                                 "order": ["The End of Eternity", "I, Robot", "Foundation"],
                                 "series": ["Robot", "Foundation"]}]})
    def w(title, series="", idx=None, author="Isaac Asimov"):
        return {"kind": "book", "title": title, "series": series, "series_index": idx, "authors": [author]}
    works = {"a": w("Foundation", "Foundation", 3), "b": w("The End of Eternity"),
             "c": w("Second Foundation", "Foundation", 5), "d": w("Foundation", author="Someone Else"),
             "e": w("Robots and Empire", "Robot", 4)}
    _assign_universes(cfg, works)
    assert works["b"]["universe_index"] < works["a"]["universe_index"] < works["e"]["universe_index"]
    assert works["c"]["universe"] == "Foundation Universe" and works["d"]["universe"] == ""


def test_screens_merge_on_ids_and_availability():
    from app.indexer import _screen_info, group_screens
    jf = Unit(source="jellyfin", source_id="j1", kind="show", format="series", title="Korra", ids={"tvdb": "251085"})
    so = Unit(source="sonarr", source_id="4", kind="show", format="tracked", title="The Legend of Korra",
              ids={"tvdb": "251085"}, extra={"arr": {"have": 52, "aired": 52}})
    coming = Unit(source="sonarr", source_id="9", kind="show", format="tracked", title="Silo",
                  ids={"tvdb": "403245"}, extra={"arr": {"have": 0, "aired": 20}})
    groups = sorted(group_screens([jf, so, coming]), key=len)
    assert [len(g) for g in groups] == [1, 2]
    assert _screen_info(groups[1])["availability"] == "in_library"
    assert _screen_info(groups[0])["availability"] == "coming"


def test_series_key_unifies_spellings():
    k = normalize.series_key
    assert k("The Silo Saga") == k("Silo") == "silo"
    assert k("The Expanse") == k("Expanse Series") == "expanse"
    assert k("Foundation") != k("Robot")


def test_work_merges_fields_and_status():
    w = build_work([
        U("calibre", "1", "Wool (Silo)", description="short"),
        U("abs", "a1", "Wool", narrators=["Minnie Goode"], duration=36000, progress=0.4, genres=["Science Fiction"]),
    ])
    assert w["title"] == "Wool (Silo)" and w["narrators"] == ["Minnie Goode"]
    assert w["status"] == "in_progress" and w["formats"] == ["audiobook", "ebook"]
    assert w["genres"] == ["Science Fiction"]
