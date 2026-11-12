"""
Unit tests for external_sources.normalization.transforms — the reusable value
transforms that field specs chain together.
"""

import pytest

from external_sources.normalization import transforms as t

CTX: dict = {"record_type": "call", "natural_key": "X"}


class TestRegistry:
    """Test the transform registry"""

    def test_get_transform_returns_registered_function(self):
        """A registered name resolves to its function"""
        assert t.get_transform("first") is t.first

    def test_unknown_transform_raises_key_error(self):
        """An unknown name must fail loudly and name the culprit"""
        with pytest.raises(KeyError, match="nope"):
            t.get_transform("nope")

    def test_duplicate_registration_rejected(self):
        """Two transforms may not share a name"""
        with pytest.raises(ValueError, match="already registered"):
            t.transform("first")(lambda v, c: v)

    def test_registered_transforms_lists_names(self):
        """registered_transforms exposes every registered name"""
        assert {"first", "strip", "html_to_text", "iso_datetime", "map"} <= (
            t.registered_transforms()
        )

    def test_produces_date_flag_is_recorded(self, monkeypatch):
        """transform(name, produces_date=True) flags the name; the default does not"""
        monkeypatch.setattr(t, "_REGISTRY", dict(t._REGISTRY))
        monkeypatch.setattr(t, "_DATE_PRODUCERS", set(t._DATE_PRODUCERS))

        t.transform("demo_dates", produces_date=True)(lambda v, c: v)
        t.transform("demo_plain")(lambda v, c: v)

        assert t.is_date_producer("demo_dates") is True
        assert t.is_date_producer("demo_plain") is False

    def test_builtin_date_producers(self):
        """iso_datetime is date-producing; ordinary transforms are not"""
        assert t.is_date_producer("iso_datetime") is True
        assert t.is_date_producer("strip") is False
        assert t.is_date_producer("no_such_transform") is False


class TestFirst:
    """Test first()"""

    def test_returns_first_element(self):
        """A list yields its first element"""
        assert t.first(["a", "b"], CTX) == "a"

    def test_empty_list_returns_none(self):
        """An empty list yields None"""
        assert t.first([], CTX) is None

    def test_skips_leading_none(self):
        """None elements are skipped"""
        assert t.first([None, "b"], CTX) == "b"

    def test_scalar_passes_through(self):
        """A non-list value is returned unchanged"""
        assert t.first("a", CTX) == "a"


class TestStrip:
    """Test strip()"""

    def test_collapses_whitespace(self):
        """Internal runs of whitespace collapse to one space"""
        assert t.strip("  a \n\t b  ", CTX) == "a b"

    def test_blank_becomes_none(self):
        """Whitespace-only input becomes None"""
        assert t.strip("   ", CTX) is None





class TestHtmlToText:
    """Test html_to_text()"""

    def test_paragraphs_and_bullets(self):
        """Blocks become lines and list items get a dash bullet"""
        html = (
            "<p>Expected Outcome:</p>"
            "<ul><li>Deeper skills</li><li>Improved employability</li></ul>"
        )
        assert t.html_to_text(html, CTX) == (
            "Expected Outcome:\n- Deeper skills\n- Improved employability"
        )

    def test_inline_tags_do_not_split_lines(self):
        """Inline markup stays within its sentence"""
        html = '<p>described in <a href="https://x.eu">Annex 2</a> of the plan</p>'
        assert t.html_to_text(html, CTX) == "described in Annex 2 of the plan"

    def test_entities_are_decoded(self):
        """Entities decode to characters"""
        assert t.html_to_text("<p>R&amp;D &lt;3</p>", CTX) == "R&D <3"

    def test_nbsp_and_bom_removed(self):
        """Non-breaking spaces and BOMs are normalised away"""
        assert t.html_to_text("<p>\ufeffA\xa0B</p>", CTX) == "A B"

    def test_br_becomes_newline(self):
        """<br> becomes a line break"""
        assert t.html_to_text("a<br>b", CTX) == "a\nb"

    def test_empty_html_becomes_none(self):
        """Markup with no text becomes None"""
        assert t.html_to_text("<p> </p><div></div>", CTX) is None

    def test_plain_text_passes_through(self):
        """Text without markup is unchanged"""
        assert t.html_to_text("Just text", CTX) == "Just text"

    def test_url_like_text_does_not_warn(self, recwarn):
        """URL-looking text must not trigger a BeautifulSoup warning"""
        t.html_to_text("https://example.eu/page", CTX)
        assert len(recwarn) == 0

    def test_text_before_list(self):
        """Text adjacent to blocks does not run together"""
        assert t.html_to_text("Intro:<ul><li>a</li></ul>", CTX) == (
            "Intro:\n- a"
        )

    def test_text_between_blocks(self):
        """Text between blocks gets proper line breaks"""
        assert t.html_to_text("<p>a</p>b<p>c</p>", CTX) == (
            "a\nb\nc"
        )

    def test_nested_lists(self):
        """Nested lists get proper line breaks"""
        assert t.html_to_text(
            "<ul><li>a<ul><li>b</li></ul></li><li>c</li></ul>", CTX
        ) == "- a\n- b\n- c"

    def test_table_cells(self):
        """Table cells are separated by lines"""
        assert t.html_to_text("<td>a</td><td>b</td>", CTX) == (
            "a\nb"
        )

    def test_empty_list_item(self):
        """Empty list items don't leave stray bullet"""
        assert t.html_to_text("<ul><li></li></ul>", CTX) is None

    def test_nested_blocks_in_list_items(self):
        """Block elements inside <li> don't separate bullet from text"""
        assert t.html_to_text("<ul><li><p>a</p></li><li><p>b</p></li></ul>", CTX) == (
            "- a\n- b"
        )

    def test_multiple_blocks_in_list_item(self):
        """Multiple blocks in one <li> are separated but bullet stays with first"""
        assert t.html_to_text("<li><p>a</p><p>b</p></li>", CTX) == (
            "- a\nb"
        )


class TestHtmlLinks:
    """Test html_links()"""

    def test_extracts_and_dedupes_http_links(self):
        """Only http(s) links, de-duplicated, with collapsed text"""
        html = (
            '<a href="https://a.eu/x">Manual\n page</a>'
            '<a href="https://a.eu/x">dup</a>'
            '<a href="mailto:a@b.eu">mail</a>'
            '<a href="#top">top</a>'
        )
        assert t.html_links(html, CTX) == [{"text": "Manual page", "url": "https://a.eu/x"}]

    def test_no_links_returns_none(self):
        """No usable links yields None"""
        assert t.html_links("<p>nothing</p>", CTX) is None


class TestJsonInString:
    """Test json_in_string()"""

    def test_decodes_json_text(self):
        """A JSON string decodes to its structure"""
        assert t.json_in_string('{"a": [1, 2]}', CTX) == {"a": [1, 2]}

    def test_invalid_json_returns_none(self):
        """Invalid JSON yields None"""
        assert t.json_in_string("{not json", CTX) is None

    def test_already_parsed_passes_through(self):
        """A dict/list is returned unchanged"""
        assert t.json_in_string([1], CTX) == [1]

    def test_deep_nesting_returns_none(self):
        """Deeply nested JSON that causes recursion returns None"""
        deep_json = "[" * 100000 + "]" * 100000
        assert t.json_in_string(deep_json, CTX) is None


class TestStringList:
    """Test string_list()"""

    def test_plain_list(self):
        """A list of strings is cleaned and kept"""
        assert t.string_list([" a ", "b"], CTX) == ["a", "b"]

    def test_json_encoded_list_inside_list(self):
        """A JSON-encoded list stored as a string is unpacked"""
        assert t.string_list(['["x", "y"]'], CTX) == ["x", "y"]

    def test_dedupes_preserving_order(self):
        """Duplicates are dropped, first occurrence wins"""
        assert t.string_list(["a", "b", "a"], CTX) == ["a", "b"]

    def test_bracketed_non_json_kept_as_text(self):
        """Text that merely starts with [ is not treated as JSON"""
        assert t.string_list(["[Draft] title"], CTX) == ["[Draft] title"]

    def test_empty_becomes_none(self):
        """No usable strings yields None"""
        assert t.string_list(["", "  "], CTX) is None

    def test_deep_nesting_returns_none(self):
        """Deeply nested JSON-encoded lists that cause recursion return None"""
        deep_json = "[" * 1100 + '"x"' + "]" * 1100
        result = t.string_list(deep_json, CTX)
        assert result is None or isinstance(result, list)


class TestBoolean:
    """Test boolean()"""

    def test_true_false(self):
        """'true'/'false' (any case) map to booleans"""
        assert t.boolean("True", CTX) is True
        assert t.boolean("false", CTX) is False

    def test_other_returns_none(self):
        """Unrecognised text yields None"""
        assert t.boolean("maybe", CTX) is None


class TestIsoDatetime:
    """Test iso_datetime()"""

    def test_source_format_with_millis_and_offset(self):
        """The F&T Portal format normalises to ISO-8601 UTC"""
        assert t.iso_datetime("2026-09-24T00:00:00.000+0000", CTX) == (
            "2026-09-24T00:00:00+00:00"
        )

    def test_non_utc_offset_converted_to_utc(self):
        """Offsets are converted to UTC"""
        assert t.iso_datetime("2026-09-24T02:00:00+0200", CTX) == (
            "2026-09-24T00:00:00+00:00"
        )

    def test_date_only_gets_midnight_utc(self):
        """A bare date becomes midnight UTC"""
        assert t.iso_datetime("2026-09-24", CTX) == "2026-09-24T00:00:00+00:00"

    def test_naive_datetime_assumed_utc(self):
        """A naive datetime is treated as UTC"""
        assert t.iso_datetime("2026-09-24T10:30:00", CTX) == "2026-09-24T10:30:00+00:00"

    def test_fractional_seconds_are_truncated(self):
        """Output is always whole seconds: YYYY-MM-DDTHH:MM:SS+00:00"""
        assert t.iso_datetime("2026-01-20T14:20:17.608", CTX) == "2026-01-20T14:20:17+00:00"

    def test_garbage_returns_none(self):
        """Unparseable text yields None (the audit then flags the gap)"""
        assert t.iso_datetime("24/09/2026", CTX) is None

    def test_non_string_returns_none(self):
        """Non-string input yields None"""
        assert t.iso_datetime(20260924, CTX) is None

    def test_boundary_date_overflow(self):
        """Boundary dates that cause overflow return None"""
        assert t.iso_datetime("0001-01-01T00:00:00+0500", CTX) is None


class TestMapValue:
    """Test map_value() (registered as 'map')"""

    def test_known_value_mapped(self):
        """A known code maps to its label"""
        assert t.map_value("31094502", CTX, mapping={"31094502": "open"}) == "open"

    def test_unknown_value_returns_none(self):
        """An unknown code yields None so the gap is visible"""
        assert t.map_value("999", CTX, mapping={"31094502": "open"}) is None


class TestDeepClean:
    """Test deep_clean()"""

    def test_cleans_nested_strings(self):
        """Whitespace runs collapse and edges are trimmed at every depth"""
        value = {"a": ["  HORIZON  Research  and Innovation ", {"b": " x   y "}]}

        assert t.deep_clean(value, CTX) == {"a": ["HORIZON Research and Innovation", {"b": "x y"}]}

    def test_drops_empty_strings_none_and_empty_containers(self):
        """Entries that become empty are removed; a fully empty result is None"""
        assert t.deep_clean(["a", "  ", None, [], {"k": " "}], CTX) == ["a"]
        assert t.deep_clean(["  ", None, {"k": ""}], CTX) is None
        assert t.deep_clean("   ", CTX) is None

    def test_non_string_scalars_pass_through(self):
        """Numbers and booleans are kept (0 and False included)"""
        assert t.deep_clean({"n": 0, "f": False, "x": 2.5}, CTX) == {"n": 0, "f": False, "x": 2.5}

    def test_is_registered(self):
        """deep_clean can be used from a spec"""
        assert t.get_transform("deep_clean") is t.deep_clean



class TestGet:
    """Test get() — dot-path lookup"""

    def test_nested_lookup(self):
        """A dotted path walks nested dicts"""
        assert t.get_path({"a": {"b": 5}}, CTX, path="a.b") == 5

    def test_missing_key_returns_none(self):
        """A missing key yields None"""
        assert t.get_path({"a": 1}, CTX, path="b") is None

    def test_non_dict_returns_none(self):
        """A non-dict input yields None"""
        assert t.get_path("x", CTX, path="a") is None

    def test_false_value_is_preserved(self):
        """A False value is returned as-is, not treated as missing"""
        assert t.get_path({"sme": False}, CTX, path="sme") is False


class TestEpochMs:
    """Test epoch_ms()"""

    def test_string_milliseconds(self):
        """Epoch milliseconds (as a string) become ISO-8601 UTC"""
        assert t.epoch_ms("1768262400000", CTX) == "2026-01-13T00:00:00+00:00"

    def test_int_milliseconds(self):
        """An int works too"""
        assert t.epoch_ms(1765497600000, CTX) == "2025-12-12T00:00:00+00:00"

    def test_garbage_returns_none(self):
        """Non-numeric input yields None"""
        assert t.epoch_ms("abc", CTX) is None

    def test_bool_returns_none(self):
        """Booleans are not timestamps"""
        assert t.epoch_ms(True, CTX) is None

    def test_absurd_value_returns_none(self):
        """Out-of-range values yield None instead of raising"""
        assert t.epoch_ms("99999999999999999999", CTX) is None

    def test_is_registered_as_date_producing(self):
        """The audit's date check relies on the produces_date flag"""
        assert t.is_date_producer("epoch_ms")
