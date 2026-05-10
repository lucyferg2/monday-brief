# Lucys github Issue Triage code plan

> **Note:** This is the original pre-build plan. The actual architecture evolved during the design phase — see [docs/planning/2026-S01/bl-10-05--10-05-2026.md](planning/2026-S01/bl-10-05--10-05-2026.md) for the live backlog. Notable changes: providers are Gemini + Ollama (not OpenAI); prompts live under `src/issue_triage/prompts/` (not `skills/`); a few helper modules were folded into their consumers (`skills_loader` → `config.py`, `factory` → `providers/__init__.py`, `logging_setup` → `__main__.py`); a §2 "ongoing activity" section was added; categorisation taxonomies are user-configurable.

Applied AI engineer take-home assessment build plan   
Deliverables: link to final repo, README, written note, and the AI collaboration log 

***Brief:*** Build a tool that helps an open-source maintainer get a clearer, faster, more useful picture of what is happening in a repo's open issues than they would by scrolling GitHub for 30s. An LLM should be doing meaningful work somewhere in the pipeline. The shape, output, and definition of "useful" are up to me to make and defend.   
Up to 2 hours build.   
Submit the repo plus a short written reflection and an AI collaboration log.

## The interpretation

This tool is for one maintainer, running it on demand against one repository at a time. The output is a Monday morning brief: categorised, prioritised, summarised. The maintainer runs it locally from their terminal, and gets back persistent files they can read, share, and archive — not ephemeral terminal output.

The tool reads issue text, not source code. It cannot tell a maintainer whether a bug is technically severe — only whether it sounds severe to a careful reader. The maintainer still triages the top of the list themselves. The tool's job is to make the top of the list short and meaningful.

This interpretation is named explicitly in the writeup. Other valid interpretations exist; this one was chosen because it is well-scoped for the time, directly addresses the brief's described user (a maintainer staring at a long list of issues), and produces an artifact that demonstrates the engineering principles the brief grades on.

## Scope

### In scope

- Single repository, on-demand invocation via CLI  
- Fetch open issues from GitHub's REST API  
- LLM-driven categorisation (bug / feature / question / docs / other)  
- Priority signal combining LLM judgement with simple heuristics (reactions, comment count, age)  
- Aggregation into themes and a small "top issues" list  
- Three output formats from a single underlying data structure: Markdown, JSON, and a self-contained styled HTML report/pywebview app UX  
- Provider abstraction with at least two implementations (one paid, one free/local)  
- Prompts as skill-style markdown files  
- Configurable bounds: max issues, max LLM calls, max tokens, request timeouts  
- Structured logging, graceful failure modes, helpful error messages  
- Tests for the parts that matter (parsing, rendering, pipeline logic with mocked provider)  
- README that lets a fresh-machine reviewer get the tool running in under five minutes  
- Maybe: Multi-repository support

### Explicitly out of scope (named in writeup)

- Reading the repository's source code or suggesting code changes  
- Scheduled automation (GitHub Actions, cron)  
- Multi-repository support  
- Historical trend analysis across runs  
- Posting back to GitHub (auto-labelling, commenting on issues)  
- Custom categorisation taxonomies (only the default set)  
- Multi-language support  
- Fine-tuning or domain adaptation

The "what I'd do with more time" section of the writeup names the next step: extending this from a CLI to a self-hosted single-user service with weekly automation via GitHub Actions, persistent report history, and lightweight trend views. The CLI's data layer is shaped to support this — the service would consume the CLI's outputs rather than re-implement the pipeline.

## Architecture

### Directory layout

issue-triage/

├── README.md

├── pyproject.toml

├── .env.example

├── .gitignore

├── config.example.yaml

├── src/

│   └── issue\_triage/

│       ├── \_\_init\_\_.py

│       ├── \_\_main\_\_.py         \# entry: CLI parsing, config load, orchestration

│       ├── github.py           \# fetch issues, handle pagination and rate limits

│       ├── pipeline.py         \# categorise, prioritise, aggregate

│       ├── render.py           \# markdown, html, json renderers

│       ├── models.py           \# Pydantic models

│       ├── providers/

│       │   ├── \_\_init\_\_.py

│       │   ├── base.py         \# ModelProvider ABC

│       │   ├── openai.py

│       │   ├── ollama.py

│       │   └── factory.py      \# build\_provider(config) \-\> ModelProvider

│       └── skills/

│           ├── categorise.md

│           ├── prioritise.md

│           └── summarise.md

└── tests/

    ├── test\_github.py

    ├── test\_pipeline.py

    ├── test\_render.py

    └── fixtures/

        └── sample\_issues.json

### Key design choices

**Provider abstraction.** A `ModelProvider` ABC defines a single method, `complete(messages, **kwargs) -> str`. Two concrete implementations ship with the tool: `OpenAIProvider` (paid, used for development) and `OllamaProvider` (free, local, used by the reviewer). A factory function builds the right one based on config. Adding a third provider is one new file and one new branch in the factory. This directly satisfies the brief's "swappable model provider" requirement.

**Prompts as skill-style markdown files.** Each prompt lives in `src/issue_triage/skills/` as a markdown file with frontmatter (name, description, optional model preferences) and a body. A small loader reads them at runtime. This means non-engineers can iterate on LLM behaviour without touching Python — the configuration-over-code pattern that matches how the GSK skills ecosystem works. Prompts are not buried as inline strings.

**Workflow over agent.** The pipeline is a predefined sequence of steps with structured outputs at each stage: fetch → categorise → prioritise → aggregate → render. No agent loop. The tool's behaviour is auditable end-to-end. The brief invites a justified architectural choice; this is it.

**Pydantic models at every boundary.** `Issue`, `CategorisedIssue`, `PrioritisedIssue`, `Brief`. Every LLM call returns parsed structured output, not free text. Validation failures are caught and logged.

**Three render targets from one data structure.** The `Brief` Pydantic model is canonical. Three renderers (`markdown`, `html`, `json_export`) each consume it. Adding a fourth output format is one new function. The HTML render is a single self-contained file with embedded CSS and minimal JS — a maintainer can open it in a browser and get a styled report without running a server.

**Bounded by config.** Max issues fetched, max LLM calls per run, max tokens per call, request timeouts, retry policy. All configurable via YAML. All have sensible defaults. The cost ceiling is enforced — if the run would exceed the budget, it halts loudly with a clear message before making the call.

**Fail loudly and well.** Errors are caught at the pipeline level and surfaced with context: the URL, the issue number, the operation that failed. No bare stack traces. No silent skips. The tool either produces a complete report or fails with a clear explanation of what went wrong and what to try.

## Build order

Each step ends with a working tool that does more than it did before. Each step ends with a commit.

| \# | Step | Deliverable |
| :---- | :---- | :---- |
| 1 | Skeleton and config | `python -m issue_triage <url>` parses args, loads config, prints intent |
| 2 | GitHub client | Tool fetches real issues from a real repo and prints a count |
| 3 | Provider abstraction | Provider swappable via config; tested with a fake provider |
| 4 | Pipeline: categorise | Issues are returned with categories attached |
| 5 | Pipeline: prioritise \+ aggregate | Complete `Brief` data structure produced |
| 6 | Render: Markdown \+ JSON | Tool writes the full report to dated files |
| 7 | Render: HTML | Self-contained styled HTML report |
| 8 | Hardening | Cost ceiling, retry/backoff, error messages, structured logging |
| 9 | README \+ writeup \+ AI log | All written deliverables polished |

## Risks during the build

- **Time creep on HTML rendering.** The temptation to over-design is real. Set a 45-minute timer on Step 7 and stop when it expires. Whatever is shipped is enough.  
- **Prompt engineering rabbit hole.** The brief grades the engineering, not the prompt. Get prompts to "good enough" and move on. There is no reward for the perfect prompt.  
- **Untested provider swap.** Easy to write the abstraction and never actually test the swap. Verify the Ollama path works end-to-end at least once before submission.  
- **Secrets in git history.** Check `.gitignore` early. Run `git log -p | grep -iE "key|token|secret"` before submission.  
- **Forgetting the writeup.** The brief grades the written reflection heavily. If running short, cut the build, not the writeup.

## Pre-submission checklist

- [ ] No secrets anywhere in git history  
- [ ] `.env.example` present, `.env` ignored  
- [ ] README runs cleanly on a fresh clone (test by cloning into `/tmp` and following the README)  
- [ ] `issue-triage <some-public-repo-url>` produces a brief with the default config  
- [ ] Provider swap works (tested with both Gemini and Ollama)  
- [ ] All three output files generated to `reports/<date>/`  
- [ ] Tests pass: `pytest`  
- [ ] No bare `try: ... except: pass` anywhere  
- [ ] Helpful error messages on the common failures: bad URL, no API key, no Ollama running, rate-limited  
- [ ] Commit history is atomic and tells a story

## Post-build prep (separate from the build itself)

After the build is done, before the interview:

1. **Code walkthrough rehearsal.** Open every file. Explain what it does and why, out loud, as if to the interviewer. Any section that stumbles is a flag — go back and either rewrite or prepare a clearer explanation.  
2. **Modification practice.** Make ten small changes to the codebase without an AI assistant. Add a flag. Add a category. Add a log line. Add error handling somewhere. Add a field to a Pydantic model and propagate it. The interview's live coding section is most likely an extension of this codebase, so practising on it directly is the highest-leverage prep.  
3. **Anticipate likely probes.** questions: what happens if the LLM returns malformed JSON? Where would audit logging go? How would the API token be scoped if this ran in CI? How would this be hardened for production? Have answers ready.