from app import normalize


def test_display_title_nested_series_parentheticals():
    s = "Tales of Dunk and Egg (A Game of Thrones)"
    assert normalize.display_title("The Hedge Knight (A Game of Thrones)", s) == "The Hedge Knight"
    assert normalize.display_title("The Sworn Sword (A Game of Thrones) (The Hedge Knight (A Game of Thrones))", s) == "The Sworn Sword"


def test_display_title_keeps_non_series_parentheticals():
    assert normalize.display_title("Leviathan Wakes (The Expanse)", "") == "Leviathan Wakes (The Expanse)"
    assert normalize.display_title("Forward the Foundation (The Foundation Series: Prequels, Book 2)") == "Forward the Foundation"
