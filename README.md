# Monday Issues — `issue-triage`

A command-line tool that gives an open-source maintainer a clearer, faster, more useful picture of their repo's issues than 30 seconds of scrolling GitHub. Run it on Monday morning, get back a brief.

> Built as a Senior AI/ML Engineer take-home assessment. See [docs/plan.md](docs/plan.md) for the original plan and [docs/planning/2026-S01/bl-10-05--10-05-2026.md](docs/planning/2026-S01/bl-10-05--10-05-2026.md) for the live build backlog.

---

## What it does

Given a public GitHub repo URL, the tool fetches issues from the past 7 days and produces a Monday brief covering two sections:

1. **§1 — This week's new issues** (opened in the past 7 days, still open). Each issue is categorised, prioritised, and summarised by an LLM.
2. **§2 — FYI: ongoing activity** (open issues older than 7 days that gained new comments or reactions in the past 7 days). Same pipeline plus an extra step that reads the new activity and explains why the issue is gaining attention now.

Output: three artefacts in `reports/<date>/<owner>__<repo>/`:

- `brief.md` — Markdown for reading
- `brief.json` — canonical structured data, intended for downstream tooling
- `brief.html` — single-file styled report you can open in any browser
- `run.json` — reproducibility snapshot (provider, model, prompt versions, tokens, duration, exit status)

## Status

Pre-build. The architecture is locked, the backlog is written, and code lands per the [Sprint 1 backlog](docs/planning/2026-S01/bl-10-05--10-05-2026.md).

- [x] Brief read and interpretation chosen ("past 7 days" + two-section structure)
- [x] Plan written and reviewed
- [x] Architecture locked, backlog drafted with criteria + tests
- [ ] Implementation (S1-T1.1 → S1-T1.9)
- [ ] Writeup + AI collaboration log
- [ ] Pre-submission checklist

## Stack

- **Python 3.10+**, CLI via `argparse`, data validation via Pydantic, HTTP via `httpx`, tests via `pytest`.
- **LLM providers**: `GeminiProvider` (paid, used for development) and `OllamaProvider` (free + local *or* Ollama Cloud / authenticated self-hosted). Swappable via config — no source edits.
- **No frontend.** HTML output is a single self-contained file (inline CSS, native `<details>`, no server).
- **Style**: [Google Python Style Guide](docs/style-guide.md).

## Planned layout

```
issue-triage/
├── README.md
├── CLAUDE.md                   # project conventions for Claude Code
├── pyproject.toml
├── config.example.yaml
├── maintainer_context.md       # editable; injected into the system prompt
├── .env.example
├── docs/
│   ├── plan.md                 # original pre-build plan
│   ├── style-guide.md          # link to Google Python Style Guide + house additions
│   ├── writeup.md              # half-page reflection (graded)
│   ├── ai-collaboration.md     # how the AI assistant was used (graded)
│   └── planning/2026-S01/bl-…md
├── src/issue_triage/
│   ├── __main__.py             # CLI + logging + orchestration
│   ├── config.py               # config + prompt loader
│   ├── models.py               # Pydantic data classes
│   ├── github.py               # GitHub API client
│   ├── pipeline.py             # fetch → categorise → prioritise → render
│   ├── render.py               # markdown / json / html / run.json
│   ├── providers/
│   │   ├── __init__.py         # ModelProvider ABC + factory
│   │   ├── gemini.py
│   │   └── ollama.py
│   └── prompts/
│       ├── categorise.md
│       ├── prioritise.md
│       ├── summarise.md
│       └── new_activity.md     # §2-only — explains why the issue is moving now
└── tests/
    ├── conftest.py             # FakeProvider + shared fixtures
    ├── fixtures/sample_issues.json
    └── test_{cli,github,pipeline,providers,render,config}.py
```

## Key design choices (defended in the writeup)

- **Workflow over agent loop.** The pipeline is a fixed sequence with structured outputs at every stage — auditable end-to-end.
- **Provider abstraction with an ABC.** Adding a third provider is one new file + one factory branch.
- **Prompts as markdown files** with YAML frontmatter (name, description, version, model preferences). A non-engineer can iterate on LLM behaviour without touching Python.
- **No hardcoded model, no hardcoded taxonomy.** Both the model name and the categorisation set live in config — the maintainer owns what counts as relevant for their repo.
- **Three outputs from one canonical data structure.** A `Brief` Pydantic model is the source of truth; `render.py` has three small functions that consume it.
- **Bounded by transparency, not truncation.** The tool processes every issue meeting the time-window filter — it does **not** cap inputs to save cost. Instead it prints a pre-flight estimate of LLM calls / tokens / cost / duration and waits for user confirmation. `--yes` skips the prompt; `--max-cost <$>` adds a user-set hard ceiling. A pathological safety net (`safety_max_issues`, default 1000) halts before any LLM call if the input is wildly larger than expected.
- **External content treated as untrusted.** Issue title / body / comment text fetched from GitHub is wrapped in `<issue id="…">…</issue>` delimiters before going into prompts; suspicious patterns are logged and recorded in `run.json`.

## Quick start (will be filled in once the build lands)

```bash
# Prerequisites: Python 3.10+, optionally GITHUB_TOKEN (for higher rate limit),
# either GEMINI_API_KEY (for Gemini) or a running Ollama instance / Ollama Cloud key.

git clone <repo-url>
cd issue-triage
pip install -e .

# 1. Pick a provider in config.yaml — copy the example and edit:
cp config.example.yaml config.yaml
#    Open config.yaml, set:  provider: gemini  (or: ollama)  and  model: <name>

# 2. Set credentials in .env:
cp .env.example .env
#    Fill in GEMINI_API_KEY (for Gemini) or OLLAMA_API_KEY (for Ollama Cloud).

# 3. Run:
python -m issue_triage https://github.com/<owner>/<repo>
```

The command takes one argument — the repo URL. The provider, model, lookback window and other knobs all come from `config.yaml`. CLI flags `--provider`, `--model`, `--lookback-days`, `--max-cost`, `--yes`, `--output-dir` exist as overrides for testing or scripting; you don't need them for normal runs.

Examples and full flag reference will be added in [S1-T1.9](docs/planning/2026-S01/bl-10-05--10-05-2026.md#s1-t19--readme--writeup--ai-collaboration-log-docs).
