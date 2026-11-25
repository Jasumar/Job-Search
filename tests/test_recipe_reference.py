"""The benchmark reference must come from the capture, not from the scraper."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "recipe_author"))

from reference import build_reference  # noqa: E402


@dataclass
class Artifact:
    name: str
    fetch_mode: str
    url: str
    source: str


PAGE = "https://www.example.com/en/careers"


def page(html: str, name: str = "rendered_html", mode: str = "rendered") -> Artifact:
    return Artifact(name=name, fetch_mode=mode, url=PAGE, source=html)


def payload(body: dict | str, name: str = "json_1") -> Artifact:
    text = body if isinstance(body, str) else json.dumps(body)
    return Artifact(
        name=name,
        fetch_mode="xhr",
        url="https://www.example.com/api/search?page=1",
        source=text,
    )


def jobs_payload(count: int = 5) -> dict:
    return {
        "jobs": [
            {
                "title": f"Engineer {i}",
                "job_path": f"/en/jobs/1000{i}/engineer-{i}",
            }
            for i in range(count)
        ]
    }


class TestJsonDetector:
    def test_reads_a_job_list_that_carries_urls(self):
        ref = build_reference([page("<html></html>"), payload(jobs_payload())])
        assert ref.method == "json"
        assert ref.confidence == "structured"
        assert ref.count == 5
        assert ref.postings[0].url == "https://www.example.com/en/jobs/10000/engineer-0"
        assert ref.carried_by == "json_1"

    def test_relative_paths_resolve_against_the_payload_url(self):
        body = {"positions": [{"name": f"Analyst {i}", "url": f"/en/positions/{i}"} for i in range(4)]}
        ref = build_reference([page("<html></html>"), payload(body)])
        assert ref.count == 4
        assert ref.postings[0].url == "https://www.example.com/en/positions/0"

    def test_footer_menu_is_not_a_posting_list(self):
        """Five consistent links to five different pages is a menu, not a job list."""
        body = {
            "header": {
                "links": [
                    {"text": "Who we are", "url": "https://example.com/about-us"},
                    {"text": "Investor Relations", "url": "https://example.com/investors"},
                    {"text": "Sustainability", "url": "https://example.com/sustainability"},
                    {"text": "News", "url": "https://example.com/news"},
                    {"text": "Contact", "url": "https://example.com/contact"},
                ]
            }
        }
        ref = build_reference([page("<html></html>"), payload(body)])
        assert ref.method == "none"
        assert ref.count == 0

    def test_ids_join_to_the_page_links(self):
        """Oracle HCM style payload: ids and titles, the URLs live in the DOM."""
        html = "".join(
            f'<a href="/hcmUI/sites/CX_1/job/{24000 + i}">{i}</a>' for i in range(6)
        )
        body = {
            "items": [
                {
                    "requisitionList": [
                        {"Id": 24000 + i, "Title": f"Specialist {i}"} for i in range(10)
                    ]
                }
            ]
        }
        ref = build_reference([page(html), payload(body)])
        assert ref.method == "json"
        assert ref.confidence == "joined"
        assert ref.count == 6
        assert ref.postings[0].title == "Specialist 0"
        assert any("4 id-only item(s)" in note for note in ref.notes)

    def test_duplicate_postings_across_artifacts_count_once(self):
        ref = build_reference(
            [
                page("<html></html>"),
                payload(jobs_payload(4), name="json_1"),
                payload(jobs_payload(4), name="json_2"),
            ]
        )
        assert ref.count == 4
        assert ref.by_artifact == {"json_1": 4, "json_2": 4}


class TestJsonLdIsNotAPayload:
    """JSON-LD is evidence for the author, never a second reference source."""

    @staticmethod
    def json_ld(*postings: dict) -> Artifact:
        return Artifact(
            name="json_ld",
            fetch_mode="json_ld",
            url=PAGE,
            source=json.dumps(list(postings)),
        )

    def test_an_organization_block_does_not_become_a_posting(self):
        html = "".join(f'<a href="/en/jobs/{7000 + i}/role-{i}">Role {i}</a>' for i in range(8))
        ref = build_reference(
            [
                page(html),
                self.json_ld({"@type": "Organization", "url": "https://www.example.com/about"}),
            ]
        )
        assert ref.count == 8
        assert ref.method == "dom"
        assert not any("Organization" in url for url in ref.urls())

    def test_postings_the_block_describes_are_not_counted_twice(self):
        html = "".join(f'<a href="/en/jobs/{7000 + i}/role-{i}">Role {i}</a>' for i in range(8))
        block = [
            {"@type": "JobPosting", "url": f"https://www.example.com/en/jobs/{7000 + i}/role-{i}"}
            for i in range(8)
        ]
        ref = build_reference([page(html), self.json_ld(*block)])
        assert ref.count == 8
        assert "json_ld" not in ref.by_artifact


class TestDomDetector:
    def test_largest_job_shaped_link_group(self):
        html = "".join(
            f'<a href="/en/jobs/{5000 + i}/role-{i}">Role {i}</a>' for i in range(9)
        )
        html += '<a href="/about-us">About</a><a href="/news">News</a>'
        ref = build_reference([page(html)])
        assert ref.method == "dom"
        assert ref.confidence == "dom-only"
        assert ref.count == 9
        assert ref.postings[0].title == "Role 0"

    def test_product_catalogue_is_not_a_reference(self):
        html = "".join(f'<a href="/macbook-{i}/">MacBook {i}</a>' for i in range(30))
        ref = build_reference([page(html)])
        assert ref.method == "none"
        assert ref.count == 0
        assert any("capture gap" in note for note in ref.notes)

    def test_empty_capture_is_a_capture_gap(self):
        ref = build_reference([page("<html><body>nothing</body></html>")])
        assert ref.method == "none"
        assert ref.confidence == "none"
        assert ref.carried_by is None


class TestStaleness:
    def test_reference_is_derived_per_capture_not_stored(self):
        """Two captures of the same site with different postings give two references."""
        first = build_reference([page("<html></html>"), payload(jobs_payload(3))])
        second_body = {
            "jobs": [
                {"title": f"New Role {i}", "job_path": f"/en/jobs/9000{i}/new-role-{i}"}
                for i in range(4)
            ]
        }
        second = build_reference([page("<html></html>"), payload(second_body)])
        assert first.count == 3
        assert second.count == 4
        assert first.urls().isdisjoint(second.urls())
