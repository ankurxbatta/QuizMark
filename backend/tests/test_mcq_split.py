from app.services.question_generator import _normalise_mcq, _split_mcq_text


# The real-world failure: the model put the genuine options inline on the stem
# line, then appended a second answer-key block (with the correct option
# annotated). The old line-anchored parser grabbed the answer block and leaked
# "is the correct expression" into option B.
MEMORYLESS = (
    "What is the memoryless property of the exponential distribution? "
    "A. $P(X > r + t | X > r) = P(X > r)$ B. $P(X > r + t | X > r) = P(X > t)$ "
    "C. $P(X > r + t | X > r) = P(X < t)$ D. $P(X > r + t | X > r) = 1 - P(X < t)$\n"
    "A. P(X > r + t | X > r) = P(X > r + t)\n"
    "B. $P(X > r + t | X > r) = P(X > t)$ is the correct expression of the memoryless property.\n"
    "C. P(X > r + t | X > r) = P(X > r) + P(X > t)\n"
    "D. P(X > r + t | X > r) = P(X < r) + P(X < t)"
)


def test_split_takes_inline_options_not_answer_block():
    stem, options = _split_mcq_text(MEMORYLESS)
    assert stem == "What is the memoryless property of the exponential distribution?"
    assert options["A"] == "$P(X > r + t | X > r) = P(X > r)$"
    assert options["B"] == "$P(X > r + t | X > r) = P(X > t)$"
    assert options["C"] == "$P(X > r + t | X > r) = P(X < t)$"
    assert options["D"] == "$P(X > r + t | X > r) = 1 - P(X < t)$"
    # the leaked answer-key text must not appear in any option
    assert all("is the correct expression" not in v for v in options.values())


def test_normalise_mcq_produces_clean_block_without_leak():
    q = {
        "question_text": MEMORYLESS,
        "question_type": "mcq",
        "model_answer": "B. $P(X > r + t | X > r) = P(X > t)$ is the correct expression of the memoryless property.",
        "topic_tag": "Continuous Random Variables",
    }
    _normalise_mcq(q, MEMORYLESS)
    lines = q["question_text"].split("\n")
    assert lines[0] == "What is the memoryless property of the exponential distribution?"
    assert lines[1] == "A. $P(X > r + t | X > r) = P(X > r)$"
    assert lines[2] == "B. $P(X > r + t | X > r) = P(X > t)$"
    assert q["correct_answer"] == "B"
    assert "is the correct expression" not in q["question_text"]


def test_split_well_formed_line_break_mcq_unchanged():
    text = "Which is prime?\nA. 4\nB. 6\nC. 7\nD. 8"
    stem, options = _split_mcq_text(text)
    assert stem == "Which is prime?"
    assert options == {"A": "4", "B": "6", "C": "7", "D": "8"}


def test_split_ignores_stray_letter_in_prose():
    # "grade A." is not an options run (no ordered B, C following) → stays stem.
    text = "A student earned grade A. Which distribution is symmetric?\nA. Normal\nB. Poisson\nC. Binomial\nD. Geometric"
    stem, options = _split_mcq_text(text)
    assert "Normal" == options["A"]
    assert options["B"] == "Poisson"
    assert "grade A" in stem
