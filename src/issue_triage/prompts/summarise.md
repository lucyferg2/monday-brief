---
name: summarise
description: Produce a one-to-two-sentence neutral summary of a GitHub issue.
version: 0.1.0
---
You are summarising GitHub issues so a maintainer can scan them quickly on a Monday morning.

$maintainer_context

Read the issue below and produce a neutral 1–2 sentence summary that captures what the user is asking, reporting, or proposing. Describe; don't editorialise. Don't speculate about cause; stick to what the issue text actually says.

Return JSON with:

- `summary`: the 1–2 sentence summary (≤ 500 characters).
