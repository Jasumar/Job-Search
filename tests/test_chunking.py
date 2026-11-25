"""Offline tests for Jev-gated chunk selection and the Decisions caller."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from jobsearch_core.llm.decisions import DecisionsClient
from jobsearch_core.search.chunking import (
    CHUNK_SIZE,
    chunk_text,
    html_to_text,
    names_signal,
    select_chunks,
    strip_gated_content,
)


class TestHtmlToText:
    def test_strips_tags_and_collapses_whitespace(self):
        raw = "<html><body>\n  <h1>Top 7 Companies</h1>\n  <p>Alpha   Oy</p>\n</body></html>"
        assert html_to_text(raw) == "Top 7 Companies Alpha Oy"

    def test_keeps_list_content_intact(self):
        raw = "<ul><li>Estateguru</li><li>Tuum</li></ul>"
        assert "Estateguru Tuum" in html_to_text(raw)

    def test_drops_script_and_style_bodies(self):
        raw = (
            "<html><head><style>.row{color:red}</style>"
            '<script>var cfg = {"company_name": "Injected Co"}; while(1){}</script>'
            "</head><body><table><tr><td>Volkswagen</td><td>Volvo</td></tr></table></body></html>"
        )
        text = html_to_text(raw)

        assert "Volkswagen Volvo" in text
        assert "Injected Co" not in text
        assert "color:red" not in text

    def test_drops_unterminated_script_body(self):
        raw = "<div>ACME</div><script>var a = 1; truncated js with no closing tag"
        assert html_to_text(raw) == "ACME"

    def test_keeps_noscript_content(self):
        # <noscript> can hold the real list on lazily-rendered pages, so it is
        # deliberately not treated as code like <script>/<style>.
        raw = "<noscript><ul><li>Alpha Oy</li></noscript>"
        assert "Alpha Oy" in html_to_text(raw)


class TestNamesSignal:
    """The class-3 trigger signal: names absent from the served HTML.

    ``False`` is the only verdict that may cost a browser render, so its
    meaning must be exact — the gate answered, and no chunk carried names.
    """

    def test_absent_when_no_chunk_clears_threshold(self):
        assert names_signal([0.05, 0.1, 0.19]) is False

    def test_present_when_any_chunk_clears_threshold(self):
        assert names_signal([0.05, 0.9]) is True

    def test_unknown_when_the_gate_never_answered(self):
        # All-failed is NOT evidence of a missing list: a broken gate must
        # never be read as "this page is client-rendered".
        assert names_signal([None, None]) is None
        assert names_signal([]) is None

    def test_partial_failure_is_not_a_missing_list(self):
        assert names_signal([0.05, None]) is False


class TestStripGatedContent:
    """Signal-based gated-content guard.

    Must remove content the page labels as restricted, and must NOT remove a
    page merely because it links to a login page — mining a nav link is not
    premium harvesting, and a blanket rule would gut legitimate sources.
    """

    def test_removes_labelled_paywall_container(self):
        raw = (
            "<html><body><div class='public'>Alpha Oy Beta Oy</div>"
            "<div class='paywall-row'>Gamma Oy Delta Oy</div></body></html>"
        )
        text = strip_gated_content(raw)
        assert "Alpha Oy Beta Oy" in text
        assert "Gamma Oy" not in text
        assert "Delta Oy" not in text

    def test_removes_members_only_by_id_or_data_attribute(self):
        for attr in ("id", "data-content-tier"):
            raw = f"<div {attr}='members-only'><p>Secret Oy</p></div><p>Public Oy</p>"
            text = strip_gated_content(raw)
            assert "Secret Oy" not in text, attr
            assert "Public Oy" in text, attr

    def test_hyphenated_class_word_is_not_split(self):
        # `class="members-only"` must match: naive per-word splitting would
        # read "members" and "only" and miss the signal.
        raw = "<div class='members-only'>Secret Oy</div><p>Public Oy</p>"
        assert "Secret Oy" not in strip_gated_content(raw)

    def test_login_link_alone_is_not_enough(self):
        raw = (
            "<nav><a href='/login'>Log in</a></nav>"
            "<table><tr><td>Alpha Oy</td><td>Beta Oy</td></tr></table>"
        )
        text = strip_gated_content(raw)
        assert "Alpha Oy" in text
        assert "Beta Oy" in text

    def test_removes_gated_call_to_action_but_keeps_public_prose(self):
        raw = (
            "<p>Alpha Oy ranks third.</p>"
            "<p>Log in to access all 50 companies in this list.</p>"
            "<p>Beta Oy ranks first.</p>"
        )
        text = strip_gated_content(raw)
        assert "Alpha Oy ranks third." in text
        assert "Beta Oy ranks first." in text
        assert "access all 50" not in text

    def test_still_strips_code_bodies(self):
        raw = (
            "<style>.r{color:red}</style>"
            "<script>var cfg={'company_name':'Injected Co'}</script>"
            "<div class='paywall'>Hidden Oy</div><p>Volkswagen</p>"
        )
        text = strip_gated_content(raw)
        assert "Injected Co" not in text
        assert "color:red" not in text
        assert "Hidden Oy" not in text
        assert "Volkswagen" in text

    def test_empty_input(self):
        assert strip_gated_content("") == ""

    def test_unguarded_page_is_unchanged_in_content(self):
        raw = "<div class='row'><p>Alpha Oy</p></div>"
        assert "Alpha Oy" in strip_gated_content(raw)


class TestChunkText:
    def test_single_short_chunk(self):
        assert chunk_text("hello") == ["hello"]

    def test_exact_split(self):
        text = "a" * (CHUNK_SIZE * 2 + 5)
        chunks = chunk_text(text)
        assert len(chunks) == 3
        assert all(len(c) <= CHUNK_SIZE for c in chunks)


def _gate(noul_values: list[float], fail_on: set[int] | None = None):
    """A DecisionsClient mock returning canned noul answers per call."""
    gate = MagicMock(spec=DecisionsClient)
    calls = {"n": 0}
    fail_on = fail_on or set()

    def ask(state, questions):
        i = calls["n"]
        calls["n"] += 1
        if i in fail_on:
            raise RuntimeError("simulated gate failure")
        return {"has_company_names": {"type": "noul", "noul": noul_values[i]}}

    gate.ask.side_effect = ask
    return gate


class TestSelectChunks:
    def test_single_chunk_bypasses_gate(self):
        gate = MagicMock(spec=DecisionsClient)
        kept, scores = select_chunks("short page", gate)
        assert kept == ["short page"]
        assert scores == [1.0]
        gate.ask.assert_not_called()

    def test_high_noul_kept_low_dropped(self):
        text = ("a" * CHUNK_SIZE) + ("b" * CHUNK_SIZE)
        gate = _gate([0.9, 0.05])
        kept, scores = select_chunks(text, gate)
        assert len(kept) == 1
        assert kept[0].startswith("a")
        assert scores == [0.9, 0.05]

    def test_top_k_always_keeps_best_even_below_threshold(self):
        text = ("a" * CHUNK_SIZE) + ("b" * CHUNK_SIZE)
        gate = _gate([0.1, 0.15])
        kept, _ = select_chunks(text, gate)
        # both below 0.2 but top_k=1 keeps the best one only
        assert len(kept) == 1
        assert kept[0].startswith("b")

    def test_partial_gate_failure_falls_back_to_top_k_for_failed_chunk(self):
        text = ("a" * CHUNK_SIZE) + ("b" * CHUNK_SIZE)
        # chunk 1 (b) fails; chunk 0 scores low; failed chunk must still be
        # reachable via top_k when it is the only ranked candidate
        gate = _gate([0.05], fail_on={1})
        kept, scores = select_chunks(text, gate)
        assert scores[1] is None
        assert len(kept) >= 1

    def test_all_gate_failures_raise(self):
        text = ("a" * CHUNK_SIZE) + ("b" * CHUNK_SIZE)
        gate = _gate([], fail_on={0, 1})
        with pytest.raises(RuntimeError, match="all Noul gate calls failed"):
            select_chunks(text, gate)


class TestDecisionsClient:
    def test_ask_builds_correct_request_and_returns_answers(self):
        client = DecisionsClient(api_key="test-key", model="typesafe/jev-1.13")
        response = {
            "answers": {"has_company_names": {"type": "noul", "noul": 0.97}}
        }
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.__enter__.return_value.read.return_value = json.dumps(
                response
            ).encode()
            mock_resp.__enter__.return_value.status = 200
            mock_resp.read.return_value = json.dumps(response).encode()
            mock_resp.__enter__.return_value = mock_resp
            # simpler: json.load on the context manager object
            mock_resp.__enter__.return_value = MagicMock()
            ctx = mock_resp.__enter__.return_value
            ctx.read.return_value = json.dumps(response).encode()
            mock_open.return_value = mock_resp
            answers = client.ask({"page_text": "x"}, {"q": {"type": "noul"}})
        assert answers["has_company_names"]["noul"] == 0.97

    def test_empty_api_key_rejected(self):
        with pytest.raises(ValueError, match="requires an OpenRouter API key"):
            DecisionsClient(api_key="")

    def test_missing_answers_key_raises(self):
        client = DecisionsClient(api_key="test-key")
        with patch("urllib.request.urlopen") as mock_open:
            ctx = MagicMock()
            ctx.read.return_value = json.dumps({"unexpected": 1}).encode()
            ctx.__enter__.return_value = ctx
            mock_open.return_value = ctx
            with pytest.raises(ValueError, match="missing answers"):
                client.ask({}, {})
