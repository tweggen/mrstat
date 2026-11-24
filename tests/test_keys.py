from mrstat.keys import extract_keys


def test_basic_extraction_in_order():
    assert extract_keys("ABC-12: fix, see XY-3 and ABC-12") == ["ABC-12", "XY-3"]


def test_allowed_projects_filter_noise_and_case():
    text = "feature/abc-123-utf-8-handling ISO-8859"
    assert extract_keys(text, {"ABC"}) == ["ABC-123"]


def test_without_allowed_lowercase_ignored_unless_requested():
    assert extract_keys("abc-1") == []
    assert extract_keys("abc-1", ignore_case=True) == ["ABC-1"]


def test_no_match_inside_words_or_zero():
    assert extract_keys("XABC-1x ABC-0 ABC-07") == []
