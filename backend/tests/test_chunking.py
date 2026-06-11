from app.services.chunking import recursive_split


def test_empty_text_returns_nothing():
    assert recursive_split("", max_chars=1000, min_chars=100) == []


def test_short_text_is_single_chunk():
    text = "A short paragraph about statistics."
    assert recursive_split(text, max_chars=1000, min_chars=100) == [text]


def test_text_below_min_chars_is_not_dropped():
    text = "Tiny but real content."
    chunks = recursive_split(text, max_chars=1000, min_chars=300)
    assert chunks == [text]


def test_long_text_respects_bounds():
    sentence = "The sample mean estimates the population mean. "
    text = sentence * 200  # ~9400 chars
    chunks = recursive_split(text, max_chars=1000, min_chars=100, overlap=50)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 1000
        assert len(chunk) >= 100  # no tiny fragments — tails get merged
