from app.wanted import _rank, acceptable


def R(title, fmt="m4b", size=500_000_000, lang="en", sid=None, seeders=5):
    return {"title": title, "format": fmt, "size_bytes": size, "language": lang, "source_id": sid or title, "seeders": seeders}


def ok(r, want="Dune", fmt="audiobook", tried=()):
    return acceptable(r, want, fmt, set(tried))[0]


def test_rejects_series_packs_for_a_single_book():
    assert not ok(R("The Dune Saga - All Six Books, Fully Chaptered"))
    assert not ok(R("Dune (Books 1-6)"))
    assert not ok(R("Frank Herbert Collection", fmt="mp3"))
    assert not ok(R("Dune Chronicles 1-8", size=5_390_000_000))


def test_accepts_the_single_book():
    assert ok(R("Dune - Frank Herbert (Unabridged) [m4b]"))
    assert ok(R("Frank Herbert - Dune", fmt="mp3"))


def test_unstated_format_allowed_but_ranked_after_stated():
    unknown = R("Dune by Frank Herbert", fmt=None, size=2_000_000, seeders=99)
    stated = R("Dune by Frank Herbert EPUB", fmt=None, size=2_000_000, seeders=1)
    assert ok(unknown, fmt="ebook") and ok(stated, fmt="ebook")
    assert not ok(R("Dune by Frank Herbert PDF", fmt=None, size=2_000_000), fmt="ebook")
    assert sorted([unknown, stated], key=lambda r: _rank(r, "ebook"))[0] is stated


def test_two_books_in_one_listing_match_on_first_title():
    assert ok(R("Stephen Fry - Mythos (2017) epub", fmt="epub", size=3_000_000), want="Mythos / Heroes", fmt="ebook")


def test_format_language_size_and_tried():
    assert not ok(R("Dune", fmt="pdf"), fmt="ebook")
    assert ok(R("Dune epub", fmt="epub", size=1_200_000), fmt="ebook")
    assert not ok(R("Dune", lang="de"))
    assert not ok(R("Dune", size=5_000_000))                      # 5 MB is not an audiobook
    assert not ok(R("Dune", sid="x"), tried={"x"})
    assert not ok(R("Summary & Study Guide: Dune", fmt="epub", size=900_000), fmt="ebook")


def test_wanted_series_title_may_be_a_pack():
    assert ok(R("The Expanse Series Box Set", fmt="epub", size=9_000_000), want="The Expanse Series Box Set", fmt="ebook")


def test_rank_prefers_seeders_then_format():
    a, b = R("Dune", fmt="mp3", seeders=50), R("Dune", fmt="m4b", seeders=50)
    assert sorted([a, b], key=lambda r: _rank(r, "audiobook"))[0]["format"] == "m4b"
    c = R("Dune", fmt="mp3", seeders=200)
    assert sorted([a, b, c], key=lambda r: _rank(r, "audiobook"))[0] is c
