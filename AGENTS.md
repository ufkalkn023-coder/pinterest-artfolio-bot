# AGENTS.md — pinterest-art-bot

Durable rules for all AI coding agents working on this repository.

## Invariants

1. **Never publish copyrighted or unclear-rights artwork automatically.** Unknown rights = DENY. Default-deny is a hard gate. Do not infer reuse rights from artwork age alone.

2. **Never publish an artwork already present in authoritative publication history.** Check `StateRepository.artwork_was_published()` before any publish attempt.

3. **Never retry an ambiguous Pinterest write blindly.** If a `POST /pins` request succeeds on the server but the client doesn't receive the response, the state is `AMBIGUOUS`. It must be reconciled — never automatically retried.

4. **Never hardcode secrets.** All credentials (Pinterest tokens, museum API keys) come from environment variables or `.env`. Never commit `.env`.

5. **Never hardcode board IDs in business logic.** Board IDs come from `config/boards.yaml` or the `BoardsConfiguration` model. Python code references `board_key`, never raw Pinterest IDs.

6. **Movement classification must retain evidence.** Every `MovementClassification` must include `evidence`, `confidence`, and `reason`. A bare movement string without evidence is not acceptable.

7. **Low-confidence classification cannot auto-route.** Only `HIGH` confidence (≥0.8) auto-routes by default. `MEDIUM` requires explicit opt-in. `LOW` and `NONE` must never auto-route.

8. **Production publishing defaults OFF.** `PUBLISH_TO_PINTEREST`, `PINTEREST_AUTO_CREATE_BOARDS`, and `PINTEREST_PRODUCTION_SCHEDULE_ENABLED` all default to `false`. Enabling requires explicit configuration.

9. **Board creation defaults OFF.** `PINTEREST_AUTO_CREATE_BOARDS=false` by default. Board creation requires an explicit feature flag.

10. **Tests must pass before production-related changes.** Run `pytest`, `ruff check`, and `ruff format --check` before any merge or deployment.

## Architecture

- Museum adapters must NOT know about Pinterest.
- Pinterest code must NOT contain museum-specific parsing.
- Classification must NOT make API requests.
- Board routing must NOT scatter string comparisons — all normalization goes through `classification/aliases.py`.
- Rights evaluation is a HARD GATE — it cannot be bypassed or weakened silently.

## Code Organization

- Domain models: `src/pinterest_art_bot/domain/`
- Museum adapters: `src/pinterest_art_bot/museums/<source>/adapter.py`
- Movement aliases: `src/pinterest_art_bot/classification/aliases.py`
- Board config: `config/boards.yaml`
- Settings: `src/pinterest_art_bot/config/settings.py`

## Adding a Museum Adapter

1. Create `src/pinterest_art_bot/museums/<source>/__init__.py`
2. Create `src/pinterest_art_bot/museums/<source>/adapter.py`
3. Implement `MuseumProvider` protocol from `museums/base.py`
4. Register in `MuseumRegistry` (via `museums/factory.py`; adapters needing an API key are skipped when the key is absent)
5. Add tests

## Rights Policy

- Automatic publishing is limited to the statuses in
  `AUTO_PUBLISHABLE_STATUSES` (default: `public_domain`, `cc0`).
  `open_access`, `cc_by`, and `cc_by_sa` are permanently blocked and there is no
  override command. Do not add one without an explicit decision.
- Every Pin must disclose its licence. `PinComposer` appends the licence block
  after the templated copy so a copy change can never drop a disclosure.
- Widen the policy only through `RIGHTS_AUTO_PUBLISHABLE_STATUSES`, never by
  editing the default set in code.

## Deduplication

Four signals, checked in `PipelineRunner._check_duplicates`:
canonical ID + publication state, exact SHA-256, perceptual dHash, and
cross-source work identity. When adding a signal:
  - fingerprints must come from the ORIGINAL bytes, not a re-encoded copy;
  - matching must be conservative — a false merge hides an artwork;
  - `PipelineRunSummary` counters must stay truthful.

## Publishing Changes

- Never add a code path that publishes without consulting `Settings.can_publish`.
- `SafePinterestClient` is the only sanctioned way to reach a write; `StubPinterestClient` is for tests only.
- Any new write path must handle `PinterestAmbiguousWriteError` by parking the record in `AMBIGUOUS` — never by retrying.
- Any new deduplication signal must be checked in `PipelineRunner._check_duplicates`, and the counters on `PipelineRunSummary` must stay truthful.
- `bootstrap.py` is the only place that wires components together. Do not construct clients inside business logic.
- New Pinterest reads must go through the client so the rate limiter applies. Writes go through `PublicationService`.
- Run `pytest`, `ruff check`, `ruff format --check`, and `mypy` before opening a PR. CI enforces a coverage floor of 85%.
- If you change an adapter, check `tests/test_contracts.py`: re-record fixtures
  (`tests/fixtures/*.json`) when the upstream schema legitimately changed, and
  never weaken a contract assertion to make it pass.
- If you change copy, templates, or media handling, verify the real rendered Pin
  text and image with a live dry-run run before committing.
- A dry run must stay side-effect free: it may write dry-run plans and run
  history, never publication records, fingerprints, or work identities.
- Long-running or expensive scans (reconciliation, board listings) must take an
  explicit budget and report what they left unprocessed. Never infer absence
  from a truncated scan.
- A crashed process must never leave a record unrecoverable: in-flight records
  are swept on the next run, `PUBLISHING` to `AMBIGUOUS` and `RESERVED` to
  `FAILED`. Never retry a write that might have reached Pinterest.
