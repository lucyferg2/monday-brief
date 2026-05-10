# Monday Issues — Project Conventions

Context for Claude Code in this repo. Always loaded.

## What this project is

A CLI tool that helps an open-source maintainer get a clearer, faster, more useful picture of their repo's issues than scrolling GitHub for 30 seconds. The maintainer points it at a public GitHub repo URL on a Monday morning and gets back a brief — categorised, prioritised, summarised — across two sections:

1. **§1 — This week's new issues**: opened in the past 7 days, still open. Full triage (categorise → prioritise → summarise).
2. **§2 — FYI: ongoing activity**: open issues older than 7 days that gained new comments in the past 7 days. Same pipeline plus an additional "why this is moving now" reasoning step (the `new_activity` prompt) that reads the new comments. (Reactions as a §2 trigger are deferred — GitHub's API doesn't filter reactions by `updated_at`, so detecting "new reactions" requires fetching all reactions per issue. v1 uses comments as the §2 signal; reactions inform priority weight only.)

Output is three artefacts in `reports/<date>/`: a Markdown brief, a JSON payload, and a self-contained styled HTML report. A `run.json` snapshot accompanies each run with provider, model, prompt versions, tokens (in/out), duration, and exit status.

This is a take-home for a Senior AI/ML Engineer role with a hard 2-hour build budget. See [docs/brief.md](docs/brief.md) for the brief and [docs/plan.md](docs/plan.md) for the build plan that supersedes free-form scope discussions. Live status lives in `docs/planning/2026-S01/bl-*.md`.

## Stack

- **Language** — Python 3.10+.
- **CLI framework** — `argparse` (stdlib; one less thing to explain in interview).
- **Data validation** — Pydantic.
- **HTTP client** — `httpx` (sync mode; async is unnecessary at this scale).
- **LLM providers** — `GeminiProvider` (paid, default for dev) + `OllamaProvider` (free + local *or* Ollama Cloud). Both accessed through a single `ModelProvider` ABC. Adding a third provider is one new file + one factory branch. **Provider and model are declared in `config.yaml`** (`provider: gemini`, `model: gemini-2.0-flash`); CLI `--provider` / `--model` exist as overrides for testing but are not required for normal use.
- **Tests** — `pytest`.
- **Style** — **Google Python Style Guide** is the house standard ([link + house additions in docs/style-guide.md](docs/style-guide.md)). Type hints on every public function. Docstrings on every module, class, and public function (Google docstring format).
- **Frontend** — none. CLI only. HTML output is a self-contained file (inline CSS, minimal JS, no server).

## Workflow

Use the global skills under `~/.claude/skills/`:

- `/design` first; don't implement before the plan is approved.
- `/bl-sprint` for the build sprint (`docs/planning/2026-S01/...`).
- `/sdi-genai` for LLM-touching work; `/sdi` for the deterministic parts.
- `/test-py` for tests (mirror source layout, strict assertions).
- `/review` before handoff for non-trivial changes.
- `/commit` — Conventional Commits (`feat(scope): …`) with optional `Implements S1-T1.x-C1.x.y` trailer. See [Commits](#commits) below.
- `/standup` if the build crosses a day boundary.

## GitHub API conventions

- **Auth** — `GITHUB_TOKEN` env var, scope `public_repo` only. Tool runs without it (anonymous + lower rate limit) and surfaces a clear hint when rate-limited.
- **Rate limits** — check `X-RateLimit-Remaining`; back off cleanly. ETags for repeated calls within a run.
- **Pagination** — `Link` headers; never assume one page is enough.
- **Retries** — exponential backoff on 5xx and 429 (max 2 retries by default). Surface 403 rate-limit errors clearly.
- **Filters for §1 / §2** — see the [Two-section structure](#what-this-project-is) above. Lookback window configurable via `--lookback-days N` (default 7).

## LLM conventions

- **Keys** — Provider keys live in env vars only and are never logged.
  - `GEMINI_API_KEY` — required for `GeminiProvider`.
  - `OLLAMA_HOST` — base URL for `OllamaProvider`. Default `http://localhost:11434` (local). Set to your Ollama Cloud / self-hosted URL when using a remote instance.
  - `OLLAMA_API_KEY` — **optional**. When present, sent as `Authorization: Bearer <key>` on every Ollama call. Required for Ollama Cloud and any self-hosted instance behind auth; absent for vanilla local. Same code path either way.
- **No hardcoded model** — model name is a config / CLI flag (`--model`), not a literal in code. Reviewer with their own provider must be able to swap **without source edits**.
- **No hardcoded taxonomy** — categorisation categories are user-configurable in `config.yaml` (`categories: [{name, description}, ...]`); default ships as `[bug, feature, question, docs, other]` with one-line descriptions. The categorise prompt renders the configured list at runtime. Same principle as not capping inputs — the maintainer owns the right taxonomy for their repo.
- **Prompts as markdown files** under `src/issue_triage/prompts/` — `categorise.md`, `prioritise.md`, `summarise.md`, `new_activity.md`. Each has frontmatter (`name`, `description`, optional `model_preferences`) and a body. Loaded at runtime by a small loader. Versioned via a top-level `version:` field; current versions captured in `run.json`. (Within this codebase they're called *prompts*; in interview, that's literally what they are — instructions sent to whichever LLM the user has configured.)
- **Maintainer context config** — `config/maintainer_context.md` is editable by the user (their role, what counts as priority for *them*, recommendation style). Loaded into the system prompt. Ships with a sensible default + comments showing what to edit.
- **External content is untrusted.** All issue title / body / comment text fetched from GitHub is wrapped in `<issue id="…">…</issue>` delimiters before going into prompts; literal `</issue>` in body content is escaped (`< /issue>`) so the wrap can't be broken by attacker-controlled content. System role and user role are separated. Suspicious patterns (`<system>`, `ignore previous`, role-override attempts) are logged with a warning and noted in `run.json`. The wrap is defence in depth — the LLM has no tools / network / write access, so the realistic injection risks are output quality and LLM-echoing-into-HTML (the latter mitigated by the HTML render's escape discipline, see "Output and reporting" below).
- **Structured output where supported.** LLM calls pass `response_schema=<PydanticModel>` to the provider; Gemini and Ollama use their native structured-output modes (Gemini `response_mime_type="application/json"`, Ollama `format="json"`). Free-text JSON parsing is the fallback for providers without schema support, not the primary path.
- **No silent skips.** Issues that fail at any stage (categorise / prioritise / summarise / new_activity / parse) are kept in the brief with a `parse_failure` flag and a clear visual marker in the rendered output. The maintainer always sees that something went wrong rather than the issue silently disappearing. Counts of failures land in `run.json["parse_failures"]` and `run.json["section_2_skipped"]`.
- **Credentials never leak.** Provider classes hold credentials in private attributes with `repr=False`; `str(provider)` and `repr(provider)` are tested to never contain the value. A `logging.Filter` at startup scrubs known env-var values (`GEMINI_API_KEY`, `OLLAMA_API_KEY`, `GITHUB_TOKEN`) from any log record. Retry logs contain only URL + status code + attempt number — never body or headers.
- **Log levels matter.** INFO has IDs, counts, stage names — never issue body text. Issue text only at DEBUG (opt-in via `--verbose`).
- **Bounded by transparency, not truncation.** The pipeline processes all issues meeting the time-window filter — it does **not** cap inputs to save cost. Operational caps (`max_tokens_per_call`, `request_timeout_s`, `max_retries`) protect single-call behaviour. A pre-flight estimate prints expected calls / tokens / cost / duration and waits for user confirmation; `--yes` skips the prompt; `--max-cost <$>` adds a user-set hard ceiling. A pathological safety net at `safety_max_issues` (default 1000) halts before any LLM call if the input is wildly larger than expected, with a message naming `--lookback-days` as the fix.
- **Mocks in tests.** Unit tests use a `FakeProvider` injected at construction time. Integration test that hits a real provider lives behind a `@pytest.mark.integration` marker.

## Implementation tactics

These are design-review decisions that aren't obvious from the task headlines. Keep them in mind when implementing the relevant pieces.

- **Pipeline dispatch is split into three pure helpers** in `pipeline.py`: `render_prompt(prompt, issue, **extras)` (substitute template + wrap issue text + escape `</issue>` + injection-scan), `call_llm(provider, rendered, response_schema=None)` (the only network-touching function; accumulates `tokens_in`/`tokens_out` into `run_metadata`), and `parse_response(prompt, response)` (pass-through if schema mode returned a parsed object; otherwise validate the text). A 5-line `run_prompt()` orchestrates the three. Each helper is independently testable.
- **Priority heuristics are computed in Python, not asked of the LLM.** Reactions count, comments count, and issue age in days are computed client-side and passed to `prompts/prioritise.md` as template variables. The LLM blends them with its own judgement; the deterministic baseline keeps priority from being purely vibe-driven.
- **`new_activity` (§2-only) reads the new comments as explicit prompt input.** The §2 follow-up `/comments?since=...` call attaches a `new_comments: list[Comment]` to the `Issue` model. The prompt template renders that list inside the wrapped `<issue>` block; the LLM uses it to interpret *why* the issue is gaining attention now.
- **Themes aggregation is skipped silently when §1 has fewer than 3 items.** Not enough material to cluster meaningfully — logged INFO, no theme call made. Avoids producing thin / made-up theme lists from a 1–2 issue input.
- **Last-entry fallback for unknown LLM-emitted categories.** Categories are validated against the configured set; an LLM-emitted value outside the set falls back to the *last* configured entry (typically `other`) with a logged WARN. The issue stays in the brief with an "uncertain category" marker — never silently dropped.

## Output and reporting

- Each run writes to `reports/YYYY-MM-DD/<owner>__<repo>/` with: `brief.md`, `brief.json`, `brief.html`, `run.json`. The `<owner>__<repo>` path component is sanitised against `^[A-Za-z0-9][A-Za-z0-9._-]*$` before being used in the filesystem path.
- `run.json` captures: timestamp, repo URL, provider name, model, prompt version map, lookback days, issue counts (§1 / §2), LLM call count, tokens in / out, duration, exit status, injection-pattern warnings, parse failures, §2 skipped (with reasons).
- All three renderers (`render_markdown`, `render_json`, `render_html`) consume the same canonical `Brief` Pydantic object — single source of truth, no drift.
- `brief.html` is single-file: inline CSS, minimal vanilla JS for collapsible `<details>` sections, no external assets.
- **HTML escaping is the primary XSS defence.** A `safe()` helper at the top of `render.py` wraps `html.escape(s, quote=True)`; every dynamic value (issue title, body, LLM-generated category / summary / rationale / new_activity, theme names, URL strings, maintainer_context, user-configurable category names) flows through it before HTML insertion. No raw f-string interpolation of dynamic content is permitted in the HTML render. URL `href`s are scheme-allowlisted (http/https only) and `urllib.parse.quote`d. A CSP `<meta>` tag (`default-src 'none'; style-src 'unsafe-inline'; img-src data:`) provides defence in depth.

## Code style

- **Google Python Style Guide.** Snake_case for functions / variables, PascalCase for classes, UPPER_SNAKE for module constants. Two-line gap between top-level defs.
- **Type hints on every public function.** No `Any` in production code.
- **Docstrings on every module, class, and public function** in Google format (Args / Returns / Raises sections). Inline comments only where the *why* is non-obvious.
- **Naming** — descriptive. No `utils.py` / `helpers.py` dumping grounds.
- **Linters** — Black + Ruff. Add config to `pyproject.toml`. CI not required for the take-home (mention as future work in writeup).

## Commits

This project uses **Conventional Commits** for the *subject line*, with the optional structured ID from the backlog tucked into the commit body. This is an explicit override of the global `commit` skill's default `[S<n>-T<n>]` prefix style — Conventional Commits are universally recognised and defensible in code review without explanation.

**Subject line:** `<type>(<scope>): <description>` — e.g. `feat(pipeline): make categories user-configurable`.

**Types:** `feat`, `fix`, `test`, `docs`, `chore`, `refactor`.

**Scope** (optional but recommended): the area of the codebase touched — `cli`, `github`, `providers`, `pipeline`, `render`, `config`.

**Body** (optional): wrap at ~72 chars, explain the *why* if non-obvious, then a single trailing line with the criterion ID(s) for traceability:

```
feat(pipeline): make categories user-configurable

Categories now load from config.categories as a list of {name, description}.
Default ships as [bug, feature, question, docs, other]. LLM output is
validated against the configured set; unknown values fall back to the last
entry with a logged warning.

Implements S1-T1.4-C1.4.2, C1.4.3.
```

**Rules:**
- One logical change per commit (the brief grades atomic history).
- Don't commit when tests fail.
- Never `--no-verify`.
- The criterion-ID trailer is **optional**, not enforced — use it when you want the traceability, skip it for chore/test/docs commits where it adds noise.

## Backlog

`docs/planning/2026-S01/bl-*.md` is the live status. PLAN.md (in `docs/plan.md`) is the planning record from before the build started; update it only if scope shifts materially.

## Tests

`pytest`, tests under `tests/` mirroring `src/issue_triage/`. Mock GitHub API and LLM calls in unit tests. Run before every commit. Strict assertions (no relaxed checks).

## Things Claude should not do here

- **Don't add features the brief warns against.** Brief: *"If you finish with time to spare, resist the urge to add features. Use the time to write your notes more clearly."* No GitHub Action, no multi-repo support, no historical trend tracking, no auto-posting back to GitHub.
- **Don't cap issue throughput** for cost reasons — bound the cost surface operationally and surface the spend to the user. (See LLM conventions above.)
- **Don't skip the LLM** in any output section. Every meaningful section earns its LLM call. The role being assessed is AI engineering.
- **Don't introduce a framework or major library** without checking here first. The stack is locked: argparse, Pydantic, httpx, pytest, Gemini SDK, Ollama HTTP API.
- **Don't write clever Python.** No walrus, no metaclasses, no nested comprehensions, no decorator magic. Lucy will walk through every line in the interview and needs to defend each one.
- **Don't write narrating comments** ("# loop over issues"). Comments only when the *why* is non-obvious.
- **Don't commit anything that smells like a credential.** `.env`, `*.key`, `*.pem`, `.credentials*` are gitignored; keep it that way. Run `git log -p | Select-String -Pattern "key|token|secret"` before submission.
- **Don't refactor unrelated code** in the same task — atomic commits matter for the interview grade.
