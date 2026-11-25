# Entry 3 — posting reach rate

Does a posting URL yield usable posting text? Transport only: JS-only boards,
bot walls, login walls, PDF-only postings. No LLM anywhere in the scorer.

This entry is what finally gives Known Gap 7 (Playwright live validation, stale
since 3.6b) a number instead of an assumption.

## Run it

    uv run --extra dev python benchmarks/reach/run_benchmark.py

The user runs it; the agent does not. Read the printed report, then compare
`data/last_report.json` against the previous run in git:

    git diff benchmarks/reach/data/last_report.json

## The files

- `data/cases.json` — hand labels. **Yours to edit**; the runner never writes it.
- `score_reach.py` — the scorer. Independently written, deliberately not
  importing the transport layer's own usability heuristic (see below).
- `run_benchmark.py` — the runner: GT load, live fetch, report.
- `data/last_report.json` — last run's numbers.

## The label is an obstacle, not a boolean

Binary was cheap, but once you are labelling by hand anyway, recording *why*
costs nothing and turns the report into a diagnosis. Labels: `reachable`,
`blocked`, `login_wall`, `binary_document`, `too_thin`, `transport_error`, and
`not_a_posting`.

`not_a_posting` is the important one. A careers landing page or an FAQ page
arrives with plenty of text and is **not** a transport failure — deciding
otherwise needs semantics, and semantically it belongs to discovery and to
suite entry 5. Those rows are reported and excluded from scoring rather than
being forced into the reachable/unreachable split.

## Why the scorer does not reuse the transport heuristic

The obvious implementation asks `ingest.http._is_usable_content` whether the
response was good. That is circular: it defines "reachable" as "the code under
test agrees with itself", so a regression in the markers could not show up as a
bad number — it would redefine the target instead. `score_reach.py` therefore
carries its own marker lists, and disagreement between the two is itself a
finding.

The floor is `MIN_USABLE_CHARS = 200`, the same number the transport layer
uses, so the two are comparable side by side in a report.

## Adding cases

Copy a row, change `url`, set `label`, set `last_verified` to the day you
checked. `last_verified` is when you last confirmed the label, not when you
added the row — a closed posting or a board rebuild makes an old label wrong,
and a wrong old label reads as a code regression.

Rows whose `last_verified` is missing or older than 90 days are reported as
stale. Not a gate: a stale label is a reason to re-verify by hand, not a
reason to throw a measurement away.

**The seeded cases are nearly all easy.** The DB's 699 URLs are 9 hosts, 470
of them one Nokia board. A run over easy cases only reports a high reach rate
that means very little — the hard cases are the entire reason this entry
exists, and they have to be found by hand.

## What the first live run found (2026-10-02)

The run is also a detector. Four findings, none of them speculative:

1. **`fetch_detail` cannot distinguish a failed fetch from an empty posting.**
   It catches `(RuntimeError, ValueError)` and returns `None`, so a transport
   failure and a page with no posting both arrive as `None`. The first run
   reported `builtin.com` and `ats.rippling.com` as `too_thin`, which claims we
   got a page and found it empty. `measure()` now calls `client.fetch` and
   `extract_detail_description` separately — same two production functions, same
   order, but the exception stays visible. Product behaviour is unchanged;
   `fetch_detail`'s contract is untouched.
2. **Login walls cannot be detected by marker presence alone.** The eight live
   Built In postings carry "Please log in or sign up to report this job" next to
   4–15KB of perfectly good description. A marker match called all eight login
   walls. A wall *replaces* the posting; an incidental login link sits inside
   one. `LOGIN_WALL_MAX_CHARS = 2000` separates them — calibrated on the real
   data: genuine walls are 763 chars, incidental-widget pages 4844–15152.
3. **A padded wall is a documented blind spot.** The test is length-based, so a
   login page padded to 8KB reads as reachable. Recorded in `score_reach.py`
   rather than hidden.
4. **Listing URLs are not postings.** `microsoft_careers` returned 483KB and
   `nvidia_careers` 131KB, both of which are careers-page theme JSON, not
   posting text. Both are labelled `not_a_posting` and excluded. This is also
   why the rate follows the *label*, not the prediction — see below.

### The rate follows the label, never the prediction

An earlier `_aggregate()` filtered on the predicted obstacle and reported
`reach rate 0.706 (12/17)`, counting 9 unlabelled cases and 3 listing pages as
scored. Two of those "reachable" rows were the theme-JSON blobs above. The
denominator is now taken from the hand labels: a case counts only when a label
says it is a posting URL whose reachability this entry can judge. When nothing
is scored the rate is `None`, not `0.0` — a 0/0 denominator has no honest
numeric answer and `0.0` would read as total failure.

## Known label corrections (2026-10-02)

The first live run disagreed with three seeded labels. All three were the
labels' fault, not the scorer's, which is the point of hand-labelling:

- `nokia_plain_2` was labelled `reachable`; the posting is gone and returns
  "This job is no longer available". Relabelled `too_thin`. **A dead posting URL
  is not a reach failure** — the transport worked.
- `builtin_plain_1` was labelled `reachable`; it now fails to fetch. Relabelled
  `transport_error` and left unscored — a transport failure is a property of the
  moment, not of the site. (The seeded URL also contains a real-world typo:
  `builtin.com/job/recuiter/...`.)
- `siemens_login_wall_2` turned out to be a French-locale account-verification
  notice, not a login wall. Left unlabelled for a hand decision.

## Scope discipline

A benchmark number never blocks another lane from merging. If the number is
bad, that is a finding to report, not a reason to hold a merge.
