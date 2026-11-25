"""Jev spike: does Noul separate list-content chunks from boilerplate?

Canned chunks (no page fetching):
  - list_chunk: the Estonia listicle prose (ground-truth names present)
  - boilerplate_chunk: site nav / legal footer style text
  - borderline_chunk: prose mentioning a couple of company names in passing
"""
from __future__ import annotations

import json
import os
import urllib.request

CHUNK_SIZE = 20000

text = open("listicle_benchmark_copied_content.txt", encoding="utf-8").read()
start = text.index("Fintech in estonia")
end = text.index("world", start) if "world" in text[start:] else len(text)
estonia = text[start:text.index("https://companiesmarketcap", start)]


def strip_html(raw: str) -> str:
    import re

    no_tags = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", no_tags).strip()


list_chunk = strip_html(estonia)[:CHUNK_SIZE]

boilerplate_chunk = (
    "Skip to main content. Home Categories About Contact Sign in Sign up. "
    "Terms of Service Privacy Policy Cookie Settings. We use cookies to "
    "personalize content and ads, to provide social media features and to "
    "analyze our traffic. We also share information about your use of our "
    "site with our social media, advertising and analytics partners. "
    "All rights reserved. Newsletter: subscribe to our weekly digest for the "
    "latest updates. Follow us on X, LinkedIn and Facebook. Careers Press "
    "Kit Advertise With Us. This website is protected by reCAPTCHA and the "
    "Google Privacy Policy and Terms of Service apply. "
) * 60  # pad past a few KB so the chunk is realistically large

borderline_chunk = (
    "In this edition of our industry newsletter we look back at the quarter "
    "in European technology. Analysts debated whether companies like Spotify "
    "and Klarna will keep their valuations after the rate cycle turned. "
    "Meanwhile regulators in Brussels published fresh guidance on platform "
    "interoperability, and several trade associations responded. The rest of "
    "this report covers macroeconomic indicators, funding statistics aggregated "
    "across the region, hiring trends, and office-space demand in the major "
    "capitals. None of the individual portfolio companies are profiled here. "
    "For the full dataset see the appendix. This section is part of a longer "
    "year-end review; subsequent sections cover logistics, energy, and public "
    "sector procurement in detail. "
) * 30

QUESTIONS = {
    "has_company_names": {
        "type": "noul",
        "instructions": (
            "Does `page_text` contain the names of companies presented as "
            "subjects of the content (e.g. profiled, ranked or listed as "
            "businesses), as opposed to only incidental mentions in generic "
            "prose or boilerplate?"
        ),
    }
}


def call_jev(chunk: str, label: str) -> None:
    body = json.dumps(
        {
            "model": "typesafe/jev-1.13",
            "state": {"page_text": chunk},
            "questions": QUESTIONS,
        }
    ).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/alpha/decisions",
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    ans = data["answers"]["has_company_names"]
    print(f"{label}: noul={ans['noul']} model={data.get('model')} cost={data.get('usage', {}).get('cost')}")


if __name__ == "__main__":
    if not os.environ.get("OPENROUTER_API_KEY"):
        # load from .env
        for line in open(".env", encoding="utf-8"):
            if line.startswith("OPENROUTER_API_KEY"):
                k, _, v = line.strip().partition("=")
                v = v.strip().strip("'\"")
                if v:
                    os.environ[k.strip()] = v
    for label, chunk in [
        ("list_chunk", list_chunk),
        ("boilerplate_chunk", boilerplate_chunk),
        ("borderline_chunk", borderline_chunk),
    ]:
        print(f"{label}: {len(chunk)} chars")
        call_jev(chunk, label)
