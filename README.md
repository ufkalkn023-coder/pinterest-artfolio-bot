# pinterest-art-bot

Automated Pinterest art publishing system — museum ingestion, artwork intelligence, curation, and Pinterest publishing.

> ⚠️ **All production switches are OFF by default.**
>
> Nothing publishes until you explicitly set `PUBLISH_TO_PINTEREST=true` *and* provide a
> `PINTEREST_ACCESS_TOKEN`. Dry-run is the default mode for every command.

## Purpose

1. Query official museum APIs (Met, Art Institute of Chicago, Cleveland, Rijksmuseum)
2. Discover high-quality, public-domain artworks
3. Classify artworks by art movement with evidence-based confidence
4. Route artworks to the correct Pinterest board
5. Publish Pins through the official Pinterest API
6. Prevent duplicates and handle failures safely

## Quick start

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env

pinterest-art-bot status                    # inspect switches, museums, boards
pinterest-art-bot ingest --limit 5         # dry-run: never writes to Pinterest
pytest                                     # 345 tests
```

To go live, work through `preflight` — it reports blockers and what to do about
each, and never writes to Pinterest:

```bash
pinterest-art-bot preflight                 # resolves blockers, one at a time
export PINTEREST_ACCESS_TOKEN=...          # credentials
pinterest-art-bot boards-sync               # resolve board IDs into config/boards.yaml
pinterest-art-bot preflight                 # must report no blockers

# Canary first: one artwork, one day. Then open up.
export PUBLISH_TO_PINTEREST=true
export PINTEREST_MAX_PINS_PER_DAY=1
pinterest-art-bot ingest --publish --limit 1
pinterest-art-bot reconcile                 # confirm the outcome from Pinterest
```

`preflight` checks: production switches, credentials, the rights policy in force,
the pin budget, board configuration and ID resolution, open ambiguous/failed
records, live museum reachability, observed dry-run yield, and the Pinterest
connection. It names the actual bottleneck rather than just a yield number —
a zero yield caused by classification confidence looks completely different from
one caused by unusable images.

## Measured dry-run yield

Sample of 40 artworks per museum, dry-run, September 2026:

| Museum | Fetched | Rights approved | HIGH confidence | Ready plans |
| ------ | ------- | --------------- | --------------- | ----------- |
| `cleveland` | 40 | 40 | 13 (33%) | 13 |
| `met` | 40 | 40 | 10 (25%) | 10 |
| `artic` | 30 | 30 | 0 | 0 |

Two things this measured, both worth knowing before you enable publishing:

- **Classification confidence, not rights, is the limiting factor.** Every
  artwork passed the rights gate. Yield is capped by the rule that only HIGH
  confidence auto-routes. Widening `classification/artists.py` is the fix; the
  0.8 bar is not.
- **`artic` yields nothing from unblocked networks** because its image CDN
  returns a Cloudflare challenge, so all 30 images failed validation. That is an
  access problem, not a data problem — `preflight` reports it as such.

## Architecture

```
Museum APIs
    ↓
Ingestion adapters (per museum)          museums/<source>/adapter.py
    ↓
Normalized Artwork domain model          domain/artwork.py
    ↓
Rights eligibility gate (default: DENY)  rights/evaluator.py
    ↓
Media validation + fingerprints         media/validation.py, media/hashing.py
    ↓
Deduplication (exact + perceptual)       state/
    ↓
Movement classification (evidence-based) classification/
    ↓
Curation eligibility                     curation/eligibility.py
    ↓
Board routing (deterministic, config)    boards/router.py
    ↓
Reservation + publication pipeline        pipeline/runner.py
    ↓
Pinterest client (guarded)               pinterest/client.py
    ↓
State / deduplication / reconciliation   state/sqlite.py
```

Each layer is independent. Museum adapters don't know about Pinterest. Pinterest code doesn't contain museum-specific logic. `bootstrap.py` is the single place where the graph is wired.

## Safety Model

All production features are **OFF by default**:

| Switch | Default | Purpose |
|--------|---------|---------|
| `PUBLISH_TO_PINTEREST` | `false` | Master publishing switch |
| `PINTEREST_AUTO_CREATE_BOARDS` | `false` | Board auto-creation (requires the master switch) |
| `PINTEREST_PRODUCTION_SCHEDULE_ENABLED` | `false` | Scheduled runs |
| `RIGHTS_ENFORCE_STRICT` | `true` | Deny attribution-requiring work with no credit line |
| `CROSS_SOURCE_DEDUP_ENABLED` | `true` | Detect the same work held by several museums |
| `BOARD_ROUND_ROBIN` | `true` | Spread a run's Pins across boards |

### Rights policy

Only `public_domain` and `cc0` may be published automatically. `open_access`,
`cc_by`, and `cc_by_sa` are **permanently blocked** — reported as
`rights_review_required`, with no override command. Widen (never silently
narrow) with `RIGHTS_AUTO_PUBLISHABLE_STATUSES=["public_domain","cc0","cc_by"]`.

Every Pin discloses its licence (`License:` line, canonical URL for CC), and
attribution-requiring statuses additionally carry the museum's credit line.

Invalid combinations are rejected by `Settings` at construction time:
- `PUBLISH_TO_PINTEREST=true` without a token → `ValidationError`
- `RIGHTS_ENFORCE_STRICT=false` while publishing → `ValidationError`
- `PINTEREST_AUTO_CREATE_BOARDS=true` while publishing is off → `ValidationError`

Hard gates:
- **Unknown rights = DENY** — artworks without explicit open-access evidence are never published
- **Low confidence = SKIP** — only `HIGH` confidence auto-routes; `LOW`/`NONE` never do
- **Ambiguous writes = RECONCILE** — a write that may have landed is never retried; `pinterest-art-bot reconcile` proves the outcome from Pinterest itself
- **Budgets are hard caps** — `PINTEREST_MAX_PINS_PER_RUN` never exceeds `PINTEREST_MAX_PINS_PER_DAY`
- **Reservations prevent double writes** — a second run cannot publish an artwork another run holds
- **Dry-run is unpublishable** — `DryRunPlan.publishable` is always `false`, and dry-run never touches publication history

## Commands

| Command | What it does |
|---------|--------------|
| `status` | Safety switches, capabilities, museums, board sync state |
| `ingest` | Discovery + gates; `--publish` for real writes; `--json` for machine output |
| `boards-sync` | Resolve board IDs by name; `--create` (needs the board switch) |
| `reconcile` | Resolve `AMBIGUOUS` writes (`--limit` for a larger batch) |
| `retry` | Re-attempt artworks whose previous write definitively failed |
| `state counts\|list\|ambiguous\|dry-runs\|runs` | Inspect state, plans, and run history |
| `schedule` | Interval loop; requires the schedule switch and a single-instance lock |

`ingest` discovery modes: `--mode highlights` (default), `--mode search --query "..."`, `--mode object-id --object-ids 1 2`.

Logs are structured JSON on **stderr**; stdout carries command output only, so `--json` stays machine-parseable.

## Museum Adapters

| Museum | Source ID | API Key | Status |
|--------|-----------|---------|--------|
| Metropolitan Museum of Art | `met` | No | Implemented |
| Art Institute of Chicago | `artic` | No | Implemented |
| Cleveland Museum of Art | `cleveland` | No | Implemented |
| Rijksmuseum | `rijksmuseum` | Yes | Implemented (skipped when the key is absent) |

Adding a museum:
1. Create `src/pinterest_art_bot/museums/<source>/adapter.py`
2. Implement the `MuseumProvider` protocol from `museums/base.py`
3. Translate API responses to the canonical `Artwork` model
4. Register it in `museums/factory.py`
5. Add tests

## Art Movement Taxonomy

35+ canonical movements from Ancient through Contemporary, with 130+ aliases for normalization. Classification is evidence-based with three priority layers:

1. **Museum metadata** — explicit movement, style, period fields (highest weight)
2. **Artist mappings** — 200+ known artist → movement associations, tolerant of museum name formats (`"Monet, Claude (French, 1840-1926)"`, `"El Greco (Domenikos Theotokopoulos)"`)
3. **Date heuristics** — weak signal only, never definitive (capped at 0.35 confidence)

Confidence levels: `HIGH` (auto-route) → `MEDIUM` (opt-in) → `LOW` (skip) → `NONE` (unclassified)

Aliases are matched on **word boundaries**, so a museum tag such as `Popes` is never read as
`POP_ART` just because `pop` is a substring. A wrong movement is worse than no movement.

## Board Routing

Movement → board mapping is configuration, not code. See `config/boards.yaml`; IDs are filled in by `boards-sync` and never hardcoded in Python.

## Safe Incremental Work

- A **dry run writes nothing** that can change a later run: no publication
  records, no work identities, no fingerprints. Dry-run plans are stored in their
  own table for inspection.
- One `PipelineRunner` drives **one run at a time**; concurrent callers are
  serialised rather than sharing per-run budgets and counters.
- Housekeeping failures never fail a run — they are logged and skipped.
- A **crash mid-publish cannot wedge a record**: on the next run, a write left
  in flight beyond `STATE_INFLIGHT_TIMEOUT_SECONDS` is recovered —
  `PUBLISHING` becomes `AMBIGUOUS` (the write may have landed, so it is
  reconciled, never retried) and `RESERVED` becomes `FAILED` (nothing was sent,
  so it is retryable).
- `boards-sync` edits `config/boards.yaml` **in place**, so hand-written
  comments, key order, and inline notes survive. Pinterest boards with identical
  names are reported as ambiguous and never matched by guesswork.

## Discovery Progression

Each run resumes where the previous one stopped: the discovery cursor is persisted per museum,
so scheduled runs walk through the collection instead of rescanning its head. The cursor wraps
to the start when a listing is exhausted. Pass `--offset` to pin a position, or `rotate: false`
in the request to disable it.

## Deduplication

Four layers, in order:
1. **Canonical ID + state** — `PUBLISHED` is never republished, `AMBIGUOUS` is never retried, `FAILED`/`RECONCILED` are retryable (`retry`)
2. **Exact media** — SHA-256 of the original downloaded bytes
3. **Near-duplicate** — 64-bit dHash within `PERCEPTUAL_HASH_MAX_DISTANCE`; degenerate hashes (flat images) are ignored
4. **Cross-source work identity** — the same painting in two museums' catalogues, matched on normalized artist + title overlap + compatible dates. Conservative by design: a wrong merge hides an artwork, so anything short of strong agreement stays separate (`domain/work_identity.py`)

Fingerprints always come from the **original** bytes, so re-encoding for publishing never weakens deduplication.

## Pin text

Copy is configuration, not code: `config/pin_templates.yaml` holds board- and
movement-specific templates, rendered with safe placeholders (unknown ones are
dropped, never fatal). Museum annotations are stripped from public text
("Claude Lorrain (Claude Gellée)" → "Claude Lorrain"), and a single generated
hashtag line is appended — no duplicates, no noise from long department names.

## Board diversity

`MAX_PINS_PER_BOARD_PER_DAY` caps Pins per board per day, and
`BOARD_ROUND_ROBIN` processes the least-used boards first, so one movement
cannot dominate a run. Board usage is counted from authoritative state.

## Rate limiting

Pinterest's hourly quota is spent deliberately, not discovered through 429s:
`PINTEREST_CALLS_PER_HOUR` and `PINTEREST_CALLS_BURST` throttle the client
before each request. Writes are still never retried.

## Observability

`status` shows safety switches, capabilities, and 24-hour activity
(`published_24h`, `runs_24h`, `errors_24h`, `ambiguous_open`, `failed_open`) plus
warnings that need attention. `state runs` lists run history, and the runner
persists every run — including failed discoveries — to the state database.

Reconciliation is budgeted (`RECONCILE_MAX_RECORDS`,
`RECONCILE_MAX_PAGES_PER_BOARD`) and reports what it left for a later pass. A
board scan that hits its budget never concludes "the Pin does not exist" — the
record stays `AMBIGUOUS` for manual review.

## State Storage

`SqliteStateRepository` is the authoritative local store: WAL journaling,
off-loop writes serialised behind a lock, crash tolerance, and retention for
reservations, dry-run plans, run history, and media fingerprints.

Retention is applied on **every** run, dry-run included, so a dry-run-only host
still stays bounded. Media fingerprints are only purged for artworks that can
*never* publish (`rights_denied`, `rejected`) — a lost fingerprint on a
published, failed, or ambiguous artwork would allow a double publish, so those
are always kept.

Business logic depends only on the `StateRepository` protocol, so a distributed
backend can replace it.

## Development

```bash
pytest                       # all tests (live network tests deselected)
pytest -m live               # opt-in live museum API reads
ruff check .
ruff format --check .
mypy
pytest --cov=pinterest_art_bot --cov-fail-under=85   # the CI gate
```

Contract tests in `tests/test_contracts.py` assert adapter behaviour against
payloads recorded from the real museum APIs (`tests/fixtures/*.json`), so an
upstream schema change fails in CI instead of in production.

## Project Structure

```
src/pinterest_art_bot/
├── domain/           # Core models (Artwork, Rights, Movement, Publication)
├── museums/          # Museum adapters (met, artic, cleveland, rijksmuseum) + registry
├── rights/           # Rights eligibility evaluation (hard gate)
├── classification/   # Movement classification + alias normalization
├── boards/           # Board routing configuration + router
├── pinterest/        # HTTP client, board sync, reconciliation
├── media/            # Media validation, perceptual hashing, preparation
├── observability/    # Metrics derived from authoritative state
├── curation/         # Publication eligibility evaluation
├── state/            # State repository protocol, in-memory + SQLite
├── pipeline/         # Runner, publication service, pin composer, scheduler
├── config/           # Application settings
├── logging/          # Structured logging setup
├── bootstrap.py      # Application graph wiring
└── cli.py            # Command-line interface
```

## License

MIT
