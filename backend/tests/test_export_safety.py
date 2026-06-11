from app.api.v1.export import safe_csv_value


def test_formula_prefixes_are_escaped():
    for dangerous in ("=cmd()", "+1+1", "-1-1", "@SUM(A1)"):
        assert safe_csv_value(dangerous) == "'" + dangerous


def test_plain_strings_unchanged():
    assert safe_csv_value("hello") == "hello"
    assert safe_csv_value("") == ""


def test_non_strings_pass_through():
    assert safe_csv_value(5) == 5
    assert safe_csv_value(None) is None
