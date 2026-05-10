# Monday Brief — `issue-triage`

A command-line tool that gives an open-source maintainer a clearer, faster, more useful picture of their repo's issues than 30 seconds of scrolling GitHub. Run it on Monday morning, get back a categorised, prioritised, summarised brief in four formats: Markdown, JSON, HTML, and a `run.json` reproducibility snapshot.

---

## What it produces

For each run, the tool writes four files to `reports/<YYYY-MM-DD>/<owner>__<repo>/`:

| File | What it is |
|---|---|
| `brief_DD-MM-YY.html` | Single-file styled report. Inline CSS, no JavaScript, opens offline in any browser. Sections: *About this brief* (collapsible explanation), *At a glance* (priority-grouped issue numbers as anchor links), *Themes this week* (cross-issue clusters), *New issues this week*, *Ongoing activity*, *Run details*. Sorted by priority within each section. |
| `brief_DD-MM-YY.md` | Markdown with a priority-grouped TOC at the top. Same structure as the HTML; renders cleanly on GitHub, in VS Code's preview, or through pandoc. |
| `brief_DD-MM-YY.json` | The canonical Pydantic dump. Designed for downstream tooling — all three brief renderers consume the same `Brief` object, so the formats can't drift apart. |
| `run_DD-MM-YY.json` | Reproducibility snapshot: provider, model, prompt versions, token counts, duration, exit status, any injection warnings, parse failures, and ongoing-activity candidates that were skipped. |

The dated filenames mean a file survives being moved out of the date folder. At the start of each run, any non-today date folder under `reports/` is moved to `reports/archive/<date>/` so the top level always shows just today's runs plus an archive subfolder.

## Two sections, one pipeline

- **New issues this week** — open issues opened in the past 7 days (configurable). Each one is categorised (`bug` / `feature` / `question` / `docs` / `other` by default, or whatever you configure), prioritised (`high` / `medium` / `low` with a one-sentence rationale), and given a 1–2 sentence neutral summary.
- **Ongoing activity** — open issues older than 7 days that gained new comments in the past 7 days. The same three things, plus a *"why this is moving now"* line that interprets the new comments.

When there are enough new issues to cluster meaningfully (3+), a separate themes pass groups them into named clusters with linked issue numbers, rendered at the top of the brief as an executive summary.

## Quick start

**Prerequisites:** Python 3.10+. An LLM tool, eg:

- A Gemini API key from [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey) (free tier covers small runs).
- An Ollama Cloud API key from [https://ollama.com/settings/keys](https://ollama.com/settings/keys), *or* a local [Ollama](https://ollama.com) install.

A `GITHUB_TOKEN` is optional but strongly recommended — without it, GitHub's anonymous rate limit of 60 requests/hour can be exhausted by a single run against a busy repo.

### Install

```bash
git clone https://github.com/lucyferg2/monday-brief
cd monday-brief
pip install -e .
```

### Pick a provider in `config.yaml`

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml`. The only fields you need to change to get started:

```yaml
provider: gemini           # or: ollama
model: gemini-2.5-flash    # or any model your provider has access to
```

### Set credentials in your shell

The tool reads credentials from environment variables — no `.env` file is loaded automatically.

**PowerShell (Windows):**
```powershell
$env:GITHUB_TOKEN = "ghp_..."
$env:GEMINI_API_KEY = "..."          # only if provider: gemini
$env:OLLAMA_API_KEY = "..."          # only if Ollama Cloud / authed self-hosted
```

**bash / zsh (macOS, Linux):**
```bash
export GITHUB_TOKEN="ghp_..."
export GEMINI_API_KEY="..."          # only if provider: gemini
export OLLAMA_API_KEY="..."          # only if Ollama Cloud / authed self-hosted
```

### Run

```bash
python -m issue_triage https://github.com/<owner>/<repo>
```

You'll see a fetch summary, a **pre-flight estimate** of expected LLM calls / tokens / cost / duration, and a `Proceed? [y/N]` prompt. Type `y` to continue or anything else to abort cleanly (no tokens spent).

When the run completes:

```
Brief written to reports/2026-05-10/owner__repo/
  brief_10-05-26.md      — Markdown for reading
  brief_10-05-26.json    — canonical structured payload
  brief_10-05-26.html    — single-file styled report
  run_10-05-26.json      — reproducibility snapshot
```

Open the `.html` in any browser, or read the `.md` in your terminal.

### Useful flags

| Flag | What it does |
|---|---|
| `--lookback-days N` | Override the default 7-day window for both sections. |
| `--max-cost <$>` | Abort before any LLM call if the pre-flight estimate exceeds this ceiling. |
| `--yes` | Skip the pre-flight `[y/N]` prompt (for CI / scripted use). |
| `--provider gemini\|ollama` | Override the provider declared in `config.yaml`. |
| `--model <name>` | Override the model. |
| `--output-dir <path>` | Override `reports/`. |
| `-v / --verbose` | Lift the root logger to DEBUG to see HTTP requests and SDK traces. |

Examples:

```bash
# Default Gemini run with a 14-day window
python -m issue_triage https://github.com/owner/repo --lookback-days 14

# Cost-capped run — aborts if the estimate exceeds 25 cents
python -m issue_triage https://github.com/owner/repo --max-cost 0.25

# Non-interactive Ollama run (e.g. from cron)
python -m issue_triage https://github.com/owner/repo --provider ollama --yes
```

## Personalising the brief for your repo

The tool ships with a generic [`maintainer_context.md`](maintainer_context.md) at the repo root. Its contents are injected into the LLM's system prompt on every call, so the brief reflects *your* maintainer perspective rather than the model's defaults.

The default works for any repo. You'll get a more useful brief if you replace it with one or two lines about your project — for example:

- *"This is a Python data-pipeline library used in production. Performance regressions in numeric code are the highest priority; feature requests can usually wait."*
- *"Issues touching `auth/` or `crypto/` are security-sensitive — always flag them high regardless of reaction counts."*

Edit `maintainer_context.md` directly; plain text only, no code changes needed.

## Customising the categorisation taxonomy

The default categories — `bug`, `feature`, `question`, `docs`, `other` — work for most projects. Override them in `config.yaml` to match your repo's labels:

```yaml
categories:
  - { name: regression, description: "A previously-working feature now broken." }
  - { name: bug, description: "Unexpected behaviour, never worked." }
  - { name: enhancement, description: "Improvement to existing functionality." }
  - { name: question, description: "Usage question." }
  - { name: other, description: "Anything that doesn't fit above." }   # keep last — fallback
```

The LLM is constrained to choose exactly one of the configured names. If it ever emits something outside the set, the last entry (typically `other`) is used as a fallback and the issue is flagged in `run.json`.

## Swapping providers

The provider is config-driven. To use a different LLM with the same code:

1. **Existing provider, different model**: change `model:` in `config.yaml`.
2. **Different provider entirely**: drop a new file in `src/issue_triage/providers/` that subclasses `ModelProvider` and calls `register("<name>", <YourClass>)`. Then set `provider: <name>` in `config.yaml`. No edits to the CLI, the pipeline, or any other consumer.

See the existing `gemini.py` and `ollama.py` for the contract — one method (`complete`) and credential handling that doesn't leak through `repr()`.

## Verifying the GitHub fetch

GitHub's web UI doesn't directly distinguish "opened in the past N days" from "older issue with new comments in the past N days", which makes the section split hard to eyeball. `scripts/verify_fetch.py` runs the same fetch the tool uses and prints each issue with timestamps and a clickable URL, so the split can be checked manually:

```bash
python scripts/verify_fetch.py https://github.com/<owner>/<repo>          # default 7-day lookback
python scripts/verify_fetch.py https://github.com/<owner>/<repo> 14       # custom window
```

Every entry in *New issues this week* should have an `opened` timestamp **after** the printed cutoff; every entry in *Ongoing activity* should be opened **before** the cutoff with at least one new comment after it.

`scripts/verify_provider.py` runs a one-shot smoke against the configured provider to confirm an API key or Ollama instance is reachable before kicking off a full pipeline.

## Project layout

```
issue-triage/
├── README.md, CLAUDE.md, pyproject.toml, .gitignore
├── config.example.yaml, maintainer_context.md
├── docs/
│   ├── plan.md             # original pre-build plan
│   ├── style-guide.md      # Google Python Style Guide pointer
│   └── planning/2026-S01/  # sprint backlog
├── src/issue_triage/
│   ├── __main__.py         # CLI entry, logging setup, orchestration
│   ├── config.py           # Config schema + prompt loader + URL parser
│   ├── models.py           # Pydantic models (Issue, Brief, RunMetadata, …)
│   ├── github.py           # GitHub client (paginated, rate-limit-aware)
│   ├── pipeline.py         # render_prompt + call_llm + parse_response + orchestrator
│   ├── render.py           # Markdown / JSON / HTML / run.json renderers
│   ├── providers/
│   │   ├── __init__.py     # ModelProvider ABC + factory + registry
│   │   ├── gemini.py
│   │   └── ollama.py
│   └── prompts/            # five markdown prompts with YAML frontmatter
└── scripts/                # verify_fetch.py, verify_provider.py
```

## Design principles (each defended in the writeup)

- **Workflow over agent.** Fixed sequence with structured output at each stage. Auditable end-to-end.
- **Bounded by transparency, not truncation.** The tool processes every issue meeting the lookback filter; cost is bounded by a pre-flight estimate and (optionally) `--max-cost`, not by capping inputs. The maintainer decides what spends.
- **No silent skips.** Parse failures, unknown LLM-emitted categories, ongoing-activity follow-up failures — all surface in the brief with a flag, not by disappearing. Counts land in `run.json`.
- **Single canonical data structure.** All three brief renderers consume the same `Brief` Pydantic object. Adding a fourth output format is one new function, not a new pipeline.
- **Three-helper dispatch.** `render_prompt` (pure, template + injection scan) → `call_llm` (the only network-touching helper) → `parse_response` (validate against schema). Each independently testable.
- **External content is untrusted.** Issue text wrapped in `<issue>…</issue>` delimiters; `</issue>` in the body is escaped before wrapping; suspicious patterns logged. HTML escape on every dynamic value; URL `href`s scheme-allowlisted; CSP meta tag for defence in depth.
- **Credentials never leak.** API keys held inside SDK clients, not as provider attributes; a `logging.Filter` at startup scrubs known env-var values from any log record.

## Stack

Python 3.10+, `argparse`, Pydantic, `httpx`, `pyyaml`, `python-frontmatter`, `google-genai` (Gemini SDK). Tests via `pytest`. Style: [Google Python Style Guide](docs/style-guide.md).
