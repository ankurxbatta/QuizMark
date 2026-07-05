"""Chapter-counter latching: excerpts starting mid-book must be accepted."""
from app.services.pdf_extractor import ChunkAccumulator


def _acc(**state) -> ChunkAccumulator:
    acc = ChunkAccumulator()
    for k, v in state.items():
        setattr(acc, k, v)
    return acc


def test_full_book_sequence_still_works():
    acc = _acc()
    assert acc._accept_chapter(1)          # 0 -> 1
    acc.current_chapter_num, acc.chapters_accepted = 1, 1
    assert acc._accept_chapter(1)          # part-divider re-match
    assert acc._accept_chapter(2)          # advance
    assert not acc._accept_chapter(12)     # TOC-ish jump rejected


def test_excerpt_first_latch_any_number():
    acc = _acc()
    assert acc._accept_chapter(4)          # chapter 4–5 excerpt pack
    acc.current_chapter_num, acc.chapters_accepted = 4, 1
    assert acc._accept_chapter(5)          # sequential advance
    acc.current_chapter_num, acc.chapters_accepted = 5, 2
    assert not acc._accept_chapter(9)      # later jump still rejected


def test_false_front_matter_latch_recovers_to_chapter_one():
    acc = _acc()
    assert acc._accept_chapter(12)         # preface line false positive
    acc.current_chapter_num, acc.chapters_accepted = 12, 1
    assert acc._accept_chapter(1)          # real Chapter 1 re-latches
    acc.current_chapter_num, acc.chapters_accepted = 1, 2
    assert acc._accept_chapter(2)
    acc.current_chapter_num, acc.chapters_accepted = 2, 3
    assert not acc._accept_chapter(1)      # no more downward re-latching once advanced


def test_chapters_accepted_survives_checkpoint_roundtrip():
    acc = _acc()
    acc.current_chapter_num, acc.chapters_accepted = 4, 1
    restored = ChunkAccumulator(state=acc.serialize())
    assert restored.chapters_accepted == 1
    assert restored.current_chapter_num == 4
    assert restored._accept_chapter(5)
    assert not restored._accept_chapter(9)


def test_old_checkpoint_without_field_defaults_to_zero():
    state = ChunkAccumulator().serialize()
    del state["chapters_accepted"]
    restored = ChunkAccumulator(state=state)
    assert restored.chapters_accepted == 0
