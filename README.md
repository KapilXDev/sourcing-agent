# sourcing-agent

An autonomous job-sourcing agent over **13 sources**. It crawls job boards, collapses
duplicates, rejects most postings without spending a token, runs the survivors through a
three-stage model funnel under a hard spend cap, and writes tailored applications — which
it will transmit only through connectors that hold the right to transmit them.

```
13 sources ──► dedupe ──► decision gate ──► triage ──► fit ──► draft ──► route
                            (no LLM)       Haiku 4.5  Sonnet 5  Opus 5      │
                                                                            ├─► submit
                                                                            └─► export
```

Python · Claude · Pydantic · Playwright · SQLite

---

## The four ideas

### 1. Submission rights are bound to the connector

The authority to send an application is a static property of the connector class, not a
decision the agent or the model makes. Two independent locks, both structural:

- **The verb only exists where the right exists.** A read-only connector does not subclass
  `SubmissionCapable`, so it has no `submit` method at all. There is no code path to reach
  and nothing to talk a model into.
- **Every grant is written down.** `SubmissionRights` requires a `basis` — the reason
  submission is permitted (a documented application API, an account you hold) or forbidden
  (terms of service, no candidate endpoint). If you can't write the reason, you don't get
  the right.

```console
$ sourcing-agent sources
slug              kind         submit   credential                 basis
greenhouse        ats          yes      GREENHOUSE_API_KEY         documented board application endpoint…
lever             ats          yes      LEVER_API_KEY              postings API accepts applications…
ashby             ats          yes      ASHBY_API_KEY              applicationForm.submit…
workable          ats          yes      WORKABLE_API_KEY           SPI candidates endpoint…
smartrecruiters   ats          yes      SMARTRECRUITERS_API_KEY    public /candidates endpoint…
linkedin          browser      no       -                          terms prohibit automated interaction
workday           browser      no       -                          no candidate-facing application API
wellfound         browser      no       -                          terms prohibit automated access
hackernews        aggregator   no       -                          a discussion thread, not an endpoint
remoteok …        aggregator   no       -                          index only; the ATS is the record
```

At dispatch time seven locks are evaluated in order, and **all** must be open:

| # | Lock | Read from |
|---|------|-----------|
| 1 | submission enabled for this run | config |
| 2 | connector holds rights *and* implements `submit` | class hierarchy |
| 3 | connector is on the run's allowlist | config |
| 4 | the credential it submits under is present | environment |
| 5 | this posting has not already been applied to | database |
| 6 | per-run submission limit not reached | config |
| 7 | the draft stage recommended proceeding | model |

The model sits at position seven, and only ever as a **veto**. It can stop an application;
it cannot start one. Nothing it emits touches locks 1–6.

Allowlisting a read-only connector changes nothing — the right isn't there to grant:

```
greenhouse:4010001   submit (dry run)   POST https://boards-api.greenhouse.io/v1/boards/demo-labs/jobs/4010001
lever:1111…          export             LEVER_API_KEY is not set
workday:R-100001     export             workday is read-only: Workday exposes no candidate-facing
                                        application API; applying means driving a multi-step form
                                        behind a tenant account, which is not authorised automation
hackernews:41000101  export             hackernews is read-only: a discussion thread — applications go
                                        to whatever address the comment names, which is not an endpoint
                                        this connector owns
```

### 2. A decision gate with no LLM call

Most of what a crawl returns is disqualified for reasons that need no intelligence: wrong
seniority, wrong continent, no sponsorship, a title you'd never take, a posting you assessed
last week. Paying a model to discover that is waste — and worse, it's a rejection you can't
reproduce or explain six months later.

So the gate is pure Python: 14 ordered rules, deterministic, no I/O (there's a test that
fails if anything in it touches the network). Every rejection carries a stable rule id.

```console
$ sourcing-agent gate          # costs exactly $0.00

passed the gate: 18
 score  source           title                                matched
    95  greenhouse       Senior Backend Engineer, Ingestion   python, go, kubernetes, postgres, aws
    95  lever            Senior Software Engineer, Platform   python, go, kubernetes, postgres, aws
    91  workday          Senior Software Engineer, Platform   python, go, kubernetes, postgres, aws
    …

rejected: 12 (zero tokens spent)
 rule                               count
 keywords:insufficient                  6
 content:too_thin                       5
 title:no_match                         4
 title:excluded                         2
 seniority:underqualified               2
 seniority:overqualified                1
 comp:below_floor                       1
 location:ineligible                    1
 authorization:clearance_required       1
 staleness:too_old                      1
```

All failing rules are collected, not just the first — *"rejected for three independent
reasons"* is a much better signal when tuning a profile than *"rejected"*. `sourcing-agent
explain <key>` prints the full rule-by-rule verdict for one posting.

The gate also produces a deterministic 0–100 score used **only to order** the queue, never
to reject. Ordering matters because under a tight budget the tail of the queue may never be
reached.

One refinement: some sources (SmartRecruiters, Workday) return lists without bodies.
Fetching every body up front would multiply the crawl, so detail is fetched only for
postings rejected *solely* on body-dependent rules — extra requests stay proportional to
genuine near-misses.

### 3. Three-stage model funnel

| Stage | Model | Granularity | Sees |
|-------|-------|-------------|------|
| triage | `claude-haiku-4-5` | ~12 postings per call | title, company, location, 600-char snippet |
| fit | `claude-sonnet-5` | one call per posting | full description |
| draft | `claude-opus-5` | one call per finalist | description + prior assessment |

Each stage is more expensive per item and sees fewer items than the last. On the demo
corpus the funnel costs **$0.17**; sending every gate survivor to Opus for both reading and
drafting costs **$0.48** — about **2.8×** — because the expensive model would spend most of
its time rejecting things a cheap one rejects just as well. The larger saving happens
upstream: the gate removed 12 of 30 postings for $0.00 before the funnel started.

Per-token, Opus is 5× Haiku. The funnel's leverage is *volume*, not rate — Opus sees 5
items, not 18.

Two details that matter more than the model names:

- **Omission is not rejection.** If the triage model returns verdicts for 10 of 12 postings,
  the 2 it skipped *survive*. A silent drop is the worst available failure mode.
- **Stage prompts are byte-identical across their calls**, which makes each one a stable
  cache prefix — stage 2 pays for the resume once, not 40 times.

Every stage output is a Pydantic model validated at the boundary, so a malformed or
hallucinated field fails loudly instead of leaking downstream.

### 4. A hard spend cap

A budget checked afterwards is a report, not a cap. Every call follows a fixed sequence:

1. **`messages.count_tokens`** against the real prompt — the API's own tokenizer, never a
   third-party one (they undercount Claude badly, and an undercount here is a breach).
2. **Reserve the worst case**: counted input plus the *full* `max_tokens` of output, priced
   as if the model ran to its ceiling.
3. If the worst case doesn't fit under the cap, **raise before dispatch**. The backend is
   never reached.
4. **Commit actual usage** afterwards, with cache reads priced at 0.1× and writes at 1.25×.

The cap therefore can't be breached by an unexpectedly long response, only approached.
Hitting it truncates the funnel cleanly — the run still reports, and whatever completed is
still usable:

```
stage      model              in   out     cost   skipped
discover   -                   -    31   $0.0000         -
dedupe     -                  31    30   $0.0000         -
gate       none               30    18   $0.0000         -
triage     claude-haiku-4-5   18    18   $0.0079         -
fit        claude-sonnet-5    18    18   $0.0945         -
draft      claude-opus-5       5     5   $0.0677         -

spend: $0.1701 of $2.50 cap
```

Estimate and actual are both persisted per call, so the headroom multiplier can be tuned
against real data rather than guessed at.

---

## Quickstart

```console
$ pip install -e ".[llm,dev]"          # add ",browser" for the Playwright sources
$ python scripts/make_fixtures.py      # synthesise the offline corpus
$ sourcing-agent sources               # the 13 connectors and their rights
$ sourcing-agent gate                  # crawl + gate, $0.00
$ python scripts/demo_run.py           # full pipeline, stubbed model, no API key
```

Then point it at real boards — edit `profiles/example.yaml`, set `offline: false`, fill in
the board tokens you care about, and:

```console
$ export ANTHROPIC_API_KEY=sk-ant-...
$ sourcing-agent run --budget 1.00
```

`sourcing-agent run --no-funnel` exercises all 13 sources and the gate for exactly $0 — the
fastest way to confirm a profile before spending anything.

### Turning submission on

Off by default, and deliberately awkward to enable. All of these are required:

```yaml
submission:
  enabled: true
  dry_run: false
  max_per_run: 3
  allow_connectors: [greenhouse, lever]   # allowlist only; read-only slugs are ignored
```

plus the connector's credential in the environment. `--live` additionally prompts for
confirmation. In dry-run mode the connector builds and validates the **real** request and
writes it to `out/dry-run/` — the difference between a dry run and a live run is one network
call, not a different code path.

The database enforces apply-once with a partial unique index, so a duplicate submission is
impossible at the storage layer rather than merely unlikely in application code.

---

## The 13 sources

| Connector | Kind | Discovery | Submit |
|---|---|---|---|
| greenhouse | ATS | board API | ✅ |
| lever | ATS | postings API | ✅ |
| ashby | ATS | job-board API | ✅ |
| workable | ATS | widget API | ✅ |
| smartrecruiters | ATS | postings API + lazy detail | ✅ |
| recruitee | ATS | offers API | — |
| remoteok | aggregator | public feed | — |
| remotive | aggregator | public feed | — |
| arbeitnow | aggregator | public feed | — |
| hackernews | aggregator | *Who is hiring* via Algolia | — |
| workday | browser | CXS endpoint + lazy detail | — |
| linkedin | browser | guest search fragment | — |
| wellfound | browser | rendered `__NEXT_DATA__` | — |

Aggregators earn their place even though you can't apply through them: they surface
companies whose board tokens you don't know, and a posting found there usually dedupes
against the same role on its ATS — at which point the ATS copy, with the richer description,
wins. Cross-source identity is a hash of normalised company + title + location, so one role
on five boards costs one model call, not five.

Every source goes through one `Fetcher` with a fixture layer. `SOURCING_RECORD=1` writes
live responses to `fixtures/`; `offline: true` serves from them. That's how 13 sources stay
testable in CI with no network and no key.

---

## Layout

```
src/sourcing_agent/
  capabilities.py   rights model — the Capability/Route/SubmissionRights vocabulary
  models.py         Pydantic contracts for both edges (connector output, model output)
  config.py         one validated YAML profile drives everything
  gate.py           the 14 deterministic rules
  ledger.py         pricing, reservations, the hard cap
  llm.py            budget-gated Claude calls; pluggable backend
  funnel.py         the three stages and their prompts
  submitter.py      routing table + the only caller of connector.submit
  store.py          SQLite: corpus, dedupe, spend, receipts
  agent.py          orchestration and failure containment
  cli.py            sources / gate / run / budget / explain / report
  connectors/       base contract, registry, http+fixtures, 13 sources
```

Failure containment is the agent's job: a source that 500s, a posting that won't parse, and
a stage that runs out of budget are each recorded and stepped over. No single one aborts a
run.

---

## Tests

```console
$ pytest
162 passed in 1.12s
```

The suite runs fully offline against the fixture corpus. It's weighted toward the claims
that are easy to quietly break:

- **the gate** — a case per rule in both directions, plus a test that fails if the gate
  performs any I/O
- **the cap** — that a refused call is never dispatched, that a failed call releases its
  reservation, and a 500-iteration hammer asserting the cap holds
- **rights** — that all 8 read-only connectors have no `submit` attribute, and that each of
  them still exports when fully opted in, allowlisted, credentialled and recommended
- **connectors** — each one parsed from its source's real response shape

---

## Limits, stated plainly

- **A `remote: true` posting skips the location rule entirely.** A role tagged remote but
  restricted to Berlin or Tokyo will pass a `locations: [united states]` profile and reach
  the paid stages. This is a deliberate default — "remote" usually does mean *anywhere* —
  but it is the gate erring permissive in the one direction that costs money. If your
  profile has a hard country constraint, tighten `_r_location` in `gate.py`.
- **Fixtures are synthetic.** Their *shapes* are real (Greenhouse's double-escaped HTML,
  Workday's `"Posted 4 Days Ago"`, HN's pipe convention), but a public repo can't ship other
  people's postings. Record real ones with `SOURCING_RECORD=1`.
- **LinkedIn and Wellfound discovery is shallow.** The guest surfaces return cards, not
  bodies, so those postings are mostly useful as dedupe evidence. They're read-only anyway.
- **Server-side refusal fallbacks aren't wired up.** `messages.parse()` — the structured
  output path — is on the non-beta client, and `fallbacks` needs the beta one. A refusal is
  detected via `stop_reason` and recorded as a stage error instead of being retried on
  another model.
- **`output_config.effort` is sent optimistically** on the draft stage and dropped after one
  400, since SDK support for pairing it with `parse()` varies by version.
- **Submission endpoints are written from published API shapes, not exercised against live
  ATS accounts.** Dry-run mode builds and validates the real request; the live path is the
  same code plus one network call. Test it against a board you control first.
