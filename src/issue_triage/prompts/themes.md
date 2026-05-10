---
name: themes
description: Cluster this week's new issues into named themes.
version: 0.1.0
---
You are clustering this week's new GitHub issues into themes for a maintainer's Monday-morning brief.

$maintainer_context

Below is a list of summaries from this week's new issues. Group the issues into 1–5 themes that the maintainer should be aware of. A theme is a cluster of issues that share a common cause, area of the codebase, or user-visible symptom.

Rules:

- A theme must have **at least 2** issues. If you can't form a 2+ issue cluster, return an empty list (`{"themes": []}`).
- Maximum 5 themes total.
- An issue can appear in at most one theme.
- Don't force every issue into a theme — leave singletons out.

For each theme, output:

- `name`: a short noun-phrase label (e.g. "macOS install regressions", "auth-token refresh bugs").
- `summary`: one sentence describing what binds these issues together.
- `issue_numbers`: the issue numbers (integers) belonging to this theme.

Return JSON of shape `{"themes": [{...}, ...]}`.
