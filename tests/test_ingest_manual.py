from __future__ import annotations

from jobsearch_core.db.models import Job
from jobsearch_core.normalize import dedup_key, normalize


def test_add_job_via_cli(session):
    norm_title, norm_company, norm_location, country = normalize(
        "Python Dev", "TestCorp", "Helsinki"
    )
    dkey = dedup_key(norm_title, norm_company, norm_location)

    job = Job(
        url="https://example.com/jobs/py",
        raw_title="Python Dev",
        raw_company="TestCorp",
        raw_location="Helsinki",
        normalized_title=norm_title,
        normalized_company=norm_company,
        normalized_location=norm_location,
        country=country,
        description="Python and SQL experience required.",
        dedup_hash=dkey,
        status="new",
    )
    session.add(job)
    session.commit()

    retrieved = session.query(Job).filter(Job.url == "https://example.com/jobs/py").first()
    assert retrieved is not None
    assert retrieved.raw_title == "Python Dev"
    assert retrieved.country == "Finland"
