"""B2 scorer (mention page -> names): row shape, recall counting and the zero-yield reason."""

from __future__ import annotations

import sys
from pathlib import Path

SCORER = Path(__file__).resolve().parents[1] / "benchmarks" / "funnel" / "company_discovery"
sys.path.insert(0, str(SCORER))

from score_b2 import _alias_found, full_labels, score_page  # noqa: E402

ALIASES = {"nvidia": ["nvidia"], "apple": ["apple"], "ghost": ["ghostco"]}


def _directory(names: list[str]) -> str:
    items = "".join(f'<li><a href="https://x.example/c/{i}">{n}</a></li>' for i, n in enumerate(names))
    return f"<html><body><ul>{items}</ul></body></html>"


def test_alias_is_a_whole_word_match() -> None:
    assert _alias_found("apple", ["Apple Inc."])
    assert not _alias_found("apple", ["Pineapple Oy"])


def test_row_counts_labelled_names_found_and_missed() -> None:
    page = {"url": "https://x.example/list", "family": 1, "expected": ["nvidia", "apple", "ghost"]}
    names = ["NVIDIA", "Apple Inc.", "Acme", "Beta", "Gamma", "Delta", "Eps", "Zeta", "Eta"]
    row = score_page(page, _directory(names), ALIASES, None)
    assert row["extracted"] == 9
    assert row["labelled_found"] == 2
    assert row["labelled_missed"] == ["ghost"]
    assert row["reason"] is None
    assert row["full_expected"] is None


def test_page_without_a_cluster_names_its_reason() -> None:
    page = {"url": "https://x.example/p", "family": 3, "expected": ["nvidia"]}
    row = score_page(page, "<html><body><p>Nvidia is big.</p></body></html>", ALIASES, None)
    assert row["extracted"] == 0
    assert row["labelled_found"] == 0
    assert "page has no" in row["reason"] or "needs" in row["reason"]


def test_full_labels_score_against_the_complete_list() -> None:
    page = {"url": "https://x.example/list", "family": 4, "expected": ["nvidia"]}
    names = ["NVIDIA", "Acme", "Beta", "Gamma", "Delta", "Eps", "Zeta", "Eta"]
    row = score_page(page, _directory(names), ALIASES, ["NVIDIA", "Acme", "Missing Co"])
    assert (row["full_expected"], row["full_found"]) == (3, 2)


def test_folded_listicle_labels_are_keyed_by_url() -> None:
    labels = full_labels()
    assert "https://seedtable.com/best-startups-in-finland" in labels
