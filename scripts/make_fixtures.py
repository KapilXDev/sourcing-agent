"""Generate the offline fixture corpus.

Real recorded fixtures are better, and ``SOURCING_RECORD=1`` writes them. But a
public repo cannot ship other people's job postings, so this script synthesises
a corpus in each source's *actual* response shape - Greenhouse's escaped HTML,
Lever's split description blocks, Workday's relative "Posted 5 Days Ago"
strings, Hacker News' pipe-delimited comment convention.

The corpus is deliberately mixed: roles that should pass the gate, and roles
that should be rejected by each individual rule, so ``sourcing-agent gate``
produces a meaningful rejection table out of the box.

    python scripts/make_fixtures.py
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

NOW = datetime.now(timezone.utc)


def days_ago(n: int) -> datetime:
    return NOW - timedelta(days=n)


BODY = (
    "We are looking for an engineer to own our ingestion platform end to end. "
    "You will design and operate distributed services in Python and Go, run them "
    "on Kubernetes, and keep a heavily loaded Postgres fleet healthy under a "
    "sustained write path. The team owns its services in production, including "
    "on-call, capacity planning and incident review. We care about correctness "
    "under partial failure more than raw throughput.\n\n"
    "What we look for: strong fundamentals in distributed systems, comfort with "
    "AWS primitives, and a track record of shipping services that other teams "
    "depend on. Experience with Kafka or gRPC is a plus but not required."
)

SALES_BODY = (
    "As a Solutions Architect you will partner with our enterprise sales team to "
    "run technical discovery, deliver demos and shepherd proofs of concept from "
    "first call through to signature. You will spend most of your week in front "
    "of prospects and will carry a shared quota with your account executives. "
    "Travel is roughly 40 percent. Experience selling infrastructure software to "
    "platform teams is strongly preferred, alongside enough technical depth to "
    "earn credibility with engineers."
)

JUNIOR_BODY = (
    "This is a new grad role on our platform team. You will pair with senior "
    "engineers on services written in Python, learn how we run Kubernetes in "
    "production, and take on progressively larger pieces of the Postgres-backed "
    "ingestion path. We run a structured twelve week onboarding programme with a "
    "dedicated mentor. No prior industry experience required - we hire on "
    "fundamentals and curiosity, not on years served."
)

SPONSOR_BODY = BODY + (
    "\n\nPlease note: we are unable to provide visa sponsorship for this role. "
    "Applicants must already be authorized to work in the United States."
)

CLEARANCE_BODY = BODY + (
    "\n\nThis position supports a federal customer and requires an active TS/SCI "
    "clearance with polygraph at the time of application."
)

LOWPAY_BODY = BODY + "\n\nCompensation: $95,000 - $115,000 depending on experience."

GOODPAY_BODY = BODY + "\n\nCompensation: $180,000 - $225,000 plus equity."


def write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}")


# --------------------------------------------------------------------------


def greenhouse() -> None:
    def job(job_id, title, location, content, days, metadata=None):
        return {
            "id": job_id,
            "title": title,
            "updated_at": days_ago(days).isoformat(),
            "first_published": days_ago(days).isoformat(),
            "absolute_url": f"https://boards.greenhouse.io/demo-labs/jobs/{job_id}",
            "location": {"name": location},
            "offices": [{"name": location}],
            "company_name": "Demo Labs",
            "metadata": metadata or [],
            # Greenhouse returns HTML-escaped HTML.
            "content": html.escape(f"<div><p>{content.replace(chr(10)+chr(10), '</p><p>')}</p></div>"),
        }

    write(
        FIXTURES / "greenhouse" / "demo-labs.json",
        {
            "jobs": [
                job(4010001, "Senior Backend Engineer, Ingestion", "Remote - US", GOODPAY_BODY, 2,
                    [{"name": "Salary Range", "value": "$180,000 - $225,000"}]),
                job(4010002, "Staff Platform Engineer", "Austin, TX", BODY, 5),
                job(4010003, "Backend Engineer", "Remote - US", SPONSOR_BODY, 9),
                job(4010004, "New Grad Software Engineer", "Austin, TX", JUNIOR_BODY, 3),
                job(4010005, "Solutions Architect, Enterprise", "New York, NY", SALES_BODY, 4),
                job(4010006, "Senior Backend Engineer, Billing", "Remote - US", LOWPAY_BODY, 11),
                job(4010007, "Senior Backend Engineer, Federal", "Reston, VA", CLEARANCE_BODY, 6),
                job(4010008, "Senior Backend Engineer, Archive", "Remote - US", BODY, 64),
            ]
        },
    )


def lever() -> None:
    def job(job_id, title, location, body, days, commitment="Full-time"):
        head, _, tail = body.partition("\n\n")
        return {
            "id": job_id,
            "text": title,
            "hostedUrl": f"https://jobs.lever.co/demoworks/{job_id}",
            "createdAt": int(days_ago(days).timestamp() * 1000),
            "categories": {"location": location, "team": "Engineering", "commitment": commitment},
            "descriptionPlain": head,
            "lists": [{"text": "What you will do", "content": f"<ul><li>{tail}</li></ul>"}],
            "additionalPlain": "",
        }

    write(
        FIXTURES / "lever" / "demoworks.json",
        [
            job("11110000-aaaa-4000-8000-000000000001", "Senior Software Engineer, Platform", "Remote (US)", GOODPAY_BODY, 1),
            job("11110000-aaaa-4000-8000-000000000002", "Infrastructure Engineer", "San Francisco, CA", BODY, 7),
            job("11110000-aaaa-4000-8000-000000000003", "Engineering Manager, Platform", "Remote (US)", BODY, 3),
            job("11110000-aaaa-4000-8000-000000000004", "Backend Engineer (Contract)", "Remote (US)", BODY[:120], 2),
        ],
    )


def ashby() -> None:
    def job(job_id, title, location, body, days, remote, comp=None):
        return {
            "id": job_id,
            "title": title,
            "location": location,
            "isRemote": remote,
            "employmentType": "FullTime",
            "jobUrl": f"https://jobs.ashbyhq.com/demoscale/{job_id}",
            "publishedAt": days_ago(days).isoformat(),
            "descriptionPlain": body,
            "compensation": {"compensationTierSummary": comp} if comp else {},
        }

    write(
        FIXTURES / "ashby" / "demoscale.json",
        {
            "jobs": [
                job("a1000001-0000-4000-8000-000000000001", "Senior Backend Engineer", "Remote - United States", GOODPAY_BODY, 1, True, "$175K - $215K"),
                job("a1000001-0000-4000-8000-000000000002", "Staff Infrastructure Engineer", "San Francisco, CA", BODY, 8, False, "$210K - $250K"),
                job("a1000001-0000-4000-8000-000000000003", "Support Engineer", "Remote - United States", BODY, 4, True),
            ]
        },
    )


def workable() -> None:
    def job(shortcode, title, city, body, days, telecommuting):
        head, _, tail = body.partition("\n\n")
        return {
            "id": shortcode,
            "shortcode": shortcode,
            "title": title,
            "city": city,
            "state": "TX" if city == "Austin" else "",
            "country": "United States",
            "url": f"https://apply.workable.com/demoflow/j/{shortcode}/",
            "published_on": days_ago(days).date().isoformat(),
            "telecommuting": telecommuting,
            "employment_type": "Full-time",
            "description": f"<p>{head}</p>",
            "requirements": f"<ul><li>{tail}</li></ul>",
        }

    write(
        FIXTURES / "workable" / "demoflow.json",
        {
            "name": "Demoflow",
            "jobs": [
                job("A1B2C3D4E5", "Senior Backend Engineer", "Austin", GOODPAY_BODY, 3, True),
                job("F6G7H8I9J0", "Platform Engineer", "Berlin", BODY, 6, False),
            ],
        },
    )


def smartrecruiters() -> None:
    postings = [
        ("743999900000001", "Senior Backend Engineer", "Austin", "TX", True, 2),
        ("743999900000002", "Software Engineer, Data Platform", "New York", "NY", False, 5),
    ]
    write(
        FIXTURES / "smartrecruiters" / "DemoGroup.json",
        {
            "content": [
                {
                    "id": pid,
                    "uuid": pid,
                    "name": title,
                    "company": {"name": "Demo Group"},
                    "releasedDate": days_ago(days).isoformat(),
                    "location": {"city": city, "region": region, "country": "us", "remote": remote},
                    "applyUrl": f"https://jobs.smartrecruiters.com/DemoGroup/{pid}",
                }
                for pid, title, city, region, remote, days in postings
            ]
        },
    )
    # The list endpoint carries no body - detail is fetched lazily by the gate's
    # rescue pass, which is exactly what these fixtures exercise.
    for pid, title, *_ in postings:
        write(
            FIXTURES / "smartrecruiters" / f"detail-{pid}.json",
            {
                "id": pid,
                "name": title,
                "jobAd": {
                    "sections": {
                        "companyDescription": {"text": "<p>Demo Group builds logistics software.</p>"},
                        "jobDescription": {"text": f"<p>{GOODPAY_BODY}</p>"},
                        "qualifications": {"text": "<ul><li>7+ years of backend experience</li></ul>"},
                    }
                },
            },
        )


def recruitee() -> None:
    write(
        FIXTURES / "recruitee" / "demostudio.json",
        {
            "offers": [
                {
                    "id": 900001,
                    "slug": "senior-backend-engineer",
                    "title": "Senior Backend Engineer",
                    "company_name": "Demo Studio",
                    "location": "Remote",
                    "remote": True,
                    "published_at": days_ago(4).isoformat(),
                    "careers_url": "https://demostudio.recruitee.com/o/senior-backend-engineer",
                    "description": f"<p>{BODY}</p>",
                    "requirements": "<ul><li>Python, Go, Kubernetes</li></ul>",
                }
            ]
        },
    )


def remoteok() -> None:
    write(
        FIXTURES / "remoteok" / "feed.json",
        [
            {"legal": "See https://remoteok.com/api for terms."},
            {
                "id": "770001",
                "slug": "senior-backend-engineer",
                "company": "Remote Widgets",
                "position": "Senior Backend Engineer",
                "location": "Worldwide",
                "date": days_ago(2).isoformat(),
                "url": "https://remoteok.com/remote-jobs/770001",
                "salary_min": 160000,
                "salary_max": 200000,
                "tags": ["python", "kubernetes", "postgres"],
                "description": f"<p>{BODY}</p>",
            },
            {
                "id": "770002",
                "company": "Remote Widgets",
                "position": "Growth Marketer",
                "location": "Worldwide",
                "date": days_ago(3).isoformat(),
                "url": "https://remoteok.com/remote-jobs/770002",
                "tags": ["marketing"],
                "description": "<p>Own our paid acquisition channels end to end, from creative "
                "testing through to attribution modelling. You will manage a six figure "
                "monthly budget across search and social, and report to the VP of Growth.</p>",
            },
        ],
    )


def remotive() -> None:
    write(
        FIXTURES / "remotive" / "feed.json",
        {
            "jobs": [
                {
                    "id": 880001,
                    "title": "Backend Engineer (Python)",
                    "company_name": "Remotive Demo Co",
                    "candidate_required_location": "USA Only",
                    "publication_date": days_ago(5).isoformat(),
                    "url": "https://remotive.com/remote-jobs/880001",
                    "salary": "$150,000 - $190,000",
                    "job_type": "full_time",
                    "category": "Software Development",
                    "description": f"<p>{BODY}</p>",
                },
                {
                    "id": 880002,
                    "title": "Senior Backend Engineer, Ingestion",
                    "company_name": "Demo Labs",
                    "candidate_required_location": "Remote - US",
                    "publication_date": days_ago(2).isoformat(),
                    "url": "https://remotive.com/remote-jobs/880002",
                    "job_type": "full_time",
                    # Same role as greenhouse 4010001 - should dedupe away.
                    "description": f"<p>{BODY}</p>",
                },
            ]
        },
    )


def arbeitnow() -> None:
    write(
        FIXTURES / "arbeitnow" / "feed.json",
        {
            "data": [
                {
                    "slug": "senior-backend-engineer-berlin-990001",
                    "company_name": "Arbeit Demo GmbH",
                    "title": "Senior Backend Engineer",
                    "location": "Berlin",
                    "remote": True,
                    "url": "https://www.arbeitnow.com/view/senior-backend-engineer-berlin-990001",
                    "created_at": int(days_ago(6).timestamp()),
                    "tags": ["python", "kubernetes"],
                    "job_types": ["full_time"],
                    "description": f"<p>{BODY}</p>",
                }
            ]
        },
    )


def hackernews() -> None:
    thread_id = 41000000
    write(
        FIXTURES / "hackernews" / "latest-thread.json",
        {"hits": [{"objectID": str(thread_id), "title": "Ask HN: Who is hiring? (Month Year)"}]},
    )
    write(
        FIXTURES / "hackernews" / f"thread-{thread_id}.json",
        {
            "id": thread_id,
            "children": [
                {
                    "id": 41000101,
                    "author": "demo_founder",
                    "created_at": days_ago(7).isoformat(),
                    "text": (
                        "<p>Thread Systems | Senior Backend Engineer | Remote (US) | "
                        f"$170k-$210k | REMOTE</p><p>{BODY}</p>"
                    ),
                },
                {
                    "id": 41000102,
                    "author": "someone",
                    "created_at": days_ago(7).isoformat(),
                    "text": (
                        "<p>Does anyone know if this thread is still monitored after the "
                        "first week? I posted last month and got nothing. Asking because I "
                        "want to know whether it is worth writing a careful post or not.</p>"
                    ),
                },
            ],
        },
    )


def workday() -> None:
    write(
        FIXTURES / "workday" / "demotech-DemoCareers.json",
        {
            "total": 2,
            "jobPostings": [
                {
                    "title": "Senior Software Engineer, Platform",
                    "externalPath": "/job/Austin-TX/Senior-Software-Engineer_R-100001",
                    "locationsText": "Austin, TX",
                    "postedOn": "Posted 4 Days Ago",
                    "bulletFields": ["R-100001"],
                },
                {
                    "title": "Principal Backend Engineer",
                    "externalPath": "/job/Remote/Principal-Backend-Engineer_R-100002",
                    "locationsText": "Remote, US",
                    "postedOn": "Posted 12 Days Ago",
                    "bulletFields": ["R-100002"],
                },
            ],
        },
    )
    for req, body in (("R-100001", GOODPAY_BODY), ("R-100002", BODY)):
        write(
            FIXTURES / "workday" / f"detail-demotech-{_workday_tail(req)}.json",
            {"jobPostingInfo": {"jobDescription": f"<p>{body}</p>", "startDate": days_ago(4).date().isoformat()}},
        )


def _workday_tail(req: str) -> str:
    return {
        "R-100001": "Senior-Software-Engineer_R-100001",
        "R-100002": "Principal-Backend-Engineer_R-100002",
    }[req]


def linkedin() -> None:
    def card(job_id, title, company, location, days):
        return f"""<li>
  <div class="base-card relative job-search-card">
    <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/{title.lower().replace(' ', '-')}-at-{company.lower().replace(' ', '-')}-{job_id}?trk=public_jobs">
      <span class="sr-only">{title}</span>
    </a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">{title}</h3>
      <h4 class="base-search-card__subtitle"><a class="hidden-nested-link">{company}</a></h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">{location}</span>
        <time class="job-search-card__listdate" datetime="{days_ago(days).date().isoformat()}"></time>
      </div>
    </div>
  </div>
</li>"""

    write(
        FIXTURES / "linkedin" / "backend-engineer.html",
        "<ul class='jobs-search__results-list'>"
        + card(3900001, "Senior Backend Engineer", "Demo Labs", "Austin, TX", 3)
        + card(3900002, "Backend Engineer", "Northwind Systems", "Remote", 6)
        + "</ul>",
    )


def wellfound() -> None:
    payload = {
        "props": {
            "pageProps": {
                "jobListings": [
                    {
                        "id": 660001,
                        "slug": "senior-backend-engineer",
                        "title": "Senior Backend Engineer",
                        "startup": {"name": "Seedling Inc"},
                        "locationNames": ["Remote", "San Francisco"],
                        "remote": True,
                        "liveStartAt": int(days_ago(5).timestamp()),
                        "salaryMin": 160000,
                        "salaryMax": 200000,
                        "description": f"<p>{BODY}</p>",
                        "url": "https://wellfound.com/jobs/660001",
                    }
                ]
            }
        }
    }
    write(
        FIXTURES / "wellfound" / "software-engineer.html",
        "<!doctype html><html><body><div id='__next'></div>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>",
    )


def main() -> None:
    print(f"writing fixtures to {FIXTURES}")
    for builder in (
        greenhouse,
        lever,
        ashby,
        workable,
        smartrecruiters,
        recruitee,
        remoteok,
        remotive,
        arbeitnow,
        hackernews,
        workday,
        linkedin,
        wellfound,
    ):
        builder()
    print("done")


if __name__ == "__main__":
    main()
