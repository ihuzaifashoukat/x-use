"""Parsing hardening: the prose-fallback brace scan tracks in-string state
(quotes + backslash escapes) so braces inside JSON string values don't break
balancing, and only dict-shaped JSON counts as a successful extraction —
fenced arrays/scalars go down the parse-error path.
"""
from xuse.core.llm_service.parsing import extract_json_from_response_text


class TestBracesInsideStrings:
    def test_close_brace_inside_string_value_parses(self):
        """Repro: a '}' inside a string used to terminate the candidate early."""
        text = 'Here you go: {"text": "use } sparingly", "score": 1} done'
        data, err = extract_json_from_response_text(text)
        assert err is None
        assert data == {"text": "use } sparingly", "score": 1}

    def test_open_brace_inside_string_value_parses(self):
        """Repro: a '{' inside a string used to inflate depth so no candidate
        was found at all."""
        text = '{"text": "{hello", "score": 1}'
        data, err = extract_json_from_response_text(text)
        assert err is None
        assert data == {"text": "{hello", "score": 1}

    def test_escaped_quote_inside_string_does_not_end_it(self):
        text = 'Result: {"text": "she said \\"hi\\" } ok", "n": 2} bye'
        data, err = extract_json_from_response_text(text)
        assert err is None
        assert data == {"text": 'she said "hi" } ok', "n": 2}

    def test_escaped_backslash_before_quote(self):
        text = '{"path": "C:\\\\", "n": 1}'
        data, err = extract_json_from_response_text(text)
        assert err is None
        assert data == {"path": "C:\\", "n": 1}

    def test_braces_in_nested_string_values(self):
        text = 'prose {"outer": {"snippet": "def f() { return 1; }"}, "ok": true} tail'
        data, err = extract_json_from_response_text(text)
        assert err is None
        assert data == {"outer": {"snippet": "def f() { return 1; }"}, "ok": True}


class TestOnlyDictsAreSuccessful:
    def test_fenced_array_is_a_parse_error(self):
        data, err = extract_json_from_response_text('```json\n["quote_tweet", "like"]\n```')
        assert data is None
        assert err is not None
        assert "not an object" in err

    def test_fenced_string_is_a_parse_error(self):
        data, err = extract_json_from_response_text('```json\n"just a string"\n```')
        assert data is None
        assert "not an object" in err

    def test_fenced_number_is_a_parse_error(self):
        data, err = extract_json_from_response_text('```json\n42\n```')
        assert data is None
        assert "not an object" in err

    def test_fenced_bool_is_a_parse_error(self):
        data, err = extract_json_from_response_text('```json\ntrue\n```')
        assert data is None
        assert "not an object" in err

    def test_fenced_dict_still_succeeds(self):
        data, err = extract_json_from_response_text('```json\n{"a": 1}\n```')
        assert err is None
        assert data == {"a": 1}
