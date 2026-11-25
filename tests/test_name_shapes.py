"""PX2: name candidates from tables, lists and headings, picked by name-likeness.

Also the text-only ``dom`` recipe (PX3): no ``link`` selector, identity is the name.
"""

from __future__ import annotations

from jobsearch_core.ingest.listing import Capture, author_input, name_reference
from jobsearch_core.ingest.name_shapes import clean_name, looks_like_name
from jobsearch_core.ingest.recipe import DomRecipe, name_identity, run_dom_recipe
from jobsearch_core.ingest.recipe_author import Artifact

PAGE = "https://example.org/list"
COMPANIES = [
    "Nokia Oyj", "Kone Oyj", "Wärtsilä Oyj", "Fortum Oyj", "Neste Oyj",
    "UPM-Kymmene Oyj", "Stora Enso Oyj", "Outokumpu Oyj", "Valmet Oyj", "Metso Oyj",
]


def _reference(html: str):
    return name_reference([Artifact("static_html", "static", PAGE, html)])


def _table(names: list[str], header: str = "Company") -> str:
    rows = "".join(f"<tr><td>{i}</td><td>{n}</td><td>{i * 10}</td></tr>" for i, n in enumerate(names, 1))
    return f"<table><tr><th>Rank</th><th>{header}</th><th>Score</th></tr>{rows}</table>"


def test_clean_name_strips_rank_and_footnote() -> None:
    assert clean_name("12. Nokia Oyj [3]") == "Nokia Oyj"
    assert clean_name("#4 Apple") == "Apple"


def test_looks_like_name() -> None:
    assert looks_like_name("Nokia Oyj")
    assert looks_like_name("iZettle")
    assert not looks_like_name("Contact us")
    assert not looks_like_name("12,345")
    assert not looks_like_name("This is a full sentence about companies.")


def test_text_only_table_column_is_extracted() -> None:
    ref = _reference(f"<html><body>{_table(COMPANIES)}</body></html>")
    assert [job.title for job in ref.names] == COMPANIES
    assert ref.shape == "table"
    assert all(job.url.startswith("name:") for job in ref.names)
    assert "name-like" in ref.note


def test_numbered_headings_are_extracted() -> None:
    body = "".join(f"<h2>{i}. {n}</h2><p>Some prose about the company.</p>" for i, n in enumerate(COMPANIES, 1))
    ref = _reference(f"<html><body><article>{body}</article></body></html>")
    assert [job.title for job in ref.names] == COMPANIES
    assert ref.shape == "headings"


def test_chrome_lists_are_ignored() -> None:
    nav = "<nav><ul>" + "".join(f"<li><a href='/{n}'>{n}</a></li>" for n in COMPANIES) + "</ul></nav>"
    ref = _reference(f"<html><body>{nav}<p>Nothing else here.</p></body></html>")
    assert ref.names == ()
    assert ref.note


def test_name_like_candidate_beats_a_larger_prose_one() -> None:
    prose = "".join(f"<li>this is the sentence number {i} about nothing in particular here.</li>" for i in range(40))
    html = f"<html><body><ul>{prose}</ul>{_table(COMPANIES)}</body></html>"
    ref = _reference(html)
    assert ref.shape == "table"
    assert "not chosen" in ref.note
    assert "name-like" in ref.note


def test_a_zero_yield_page_names_why_every_shape_lost() -> None:
    items = "".join(f"<li>click {i}</li>" for i in range(12))
    ref = _reference(f"<html><body><ul>{items}</ul></body></html>")
    assert ref.names == ()
    assert "name-like" in ref.note


def test_dom_recipe_without_link_replays_by_name() -> None:
    recipe = DomRecipe.model_validate(
        {
            "kind": "dom",
            "allowed_hosts": ["example.org"],
            "verification": {
                "expected_count": 10,
                "sample_titles": ["Nokia Oyj"],
                "url_pattern": "^name:",
            },
            "container": "table tr td:nth-child(2)",
            "title": {"selector": ""},
        }
    )
    jobs = run_dom_recipe(recipe, _table(COMPANIES), PAGE)
    assert [j.title for j in jobs] == COMPANIES
    assert jobs[0].url == name_identity("Nokia Oyj") == "name:nokia oyj"


def test_author_input_accepts_text_only_reference() -> None:
    html = _table(COMPANIES)
    ref = _reference(html)
    capture = Capture([Artifact("static_html", "static", PAGE, html)], [], {}, html, None)
    inp = author_input(PAGE, capture, ref, lambda url: "")
    assert "name:" in inp.url_pattern
    assert inp.allowed_hosts == ["example.org"]
