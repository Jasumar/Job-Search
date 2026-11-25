from __future__ import annotations

from jobsearch_core.normalize import dedup_key, normalize


def test_normalize_trims_and_lowercases():
    title, company, location, country = normalize(
        "  Junior  Python Dev  ", "  Example  Corp.  ", "  Helsinki, Finland  "
    )
    assert title == "junior python dev"
    assert company == "example corp"
    assert location == "helsinki, finland"


def test_guess_country_from_city():
    _, _, _, country = normalize("Dev", "Corp", "Helsinki")
    assert country == "Finland"

    _, _, _, country = normalize("Dev", "Corp", "Berlin, Germany")
    assert country == "Germany"

    _, _, _, country = normalize("Dev", "Corp", "Remote")
    assert country == "Remote"


def test_dedup_key_is_stable():
    a = dedup_key("python developer", "example corp", "helsinki, finland")
    b = dedup_key("python developer", "example corp", "helsinki, finland")
    assert a == b


def test_dedup_key_detects_difference():
    a = dedup_key("python developer", "example corp", "helsinki")
    b = dedup_key("python developer", "example corp", "stockholm")
    assert a != b
