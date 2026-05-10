# Style guide

This project follows the **[Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)** as its house standard.

The full guide is maintained on the web at the link above; it isn't reproduced here to avoid licensing duplication and keep this repo focused on its own code.

## House conventions on top of Google style

These are the project-specific points that aren't part of the published guide:

- **Type hints on every public function.** No `Any` in production code.
- **Docstrings on every module, class, and public function**, in Google format (`Args:` / `Returns:` / `Raises:` sections).
- **Inline comments only where the *why* is non-obvious.** Don't narrate the *what*.
- **No `utils.py` / `helpers.py` dumping grounds** — name files for what they actually do.

See [CLAUDE.md](../CLAUDE.md) for full project conventions.
