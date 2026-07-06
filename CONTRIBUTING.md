# Contributing

Thanks for contributing to Anaplan Audit History. The full developer
reference lives in [docs/developer-guide.md](docs/developer-guide.md)
(rendered copy: `docs/developer-guide.docx`) — this file is the short
version.

## Setup

```bash
git clone https://github.com/jferneau/anaplan-audit-with-history.git
cd anaplan-audit-with-history
uv sync
uv run pytest        # all tests must pass before you start
```

## Before opening a PR

- [ ] `uv run pytest` — all tests pass
- [ ] `uv run mypy src/` — clean (strict mode)
- [ ] `uv run ruff check src/ tests/` and `uv run ruff format src/ tests/` — clean
- [ ] New behavior has a test; bug fixes have a regression test
- [ ] `CHANGELOG.md` updated
- [ ] If you touched `activity_events.csv`: kept legacy codes, annotated the Notes column
- [ ] If you added an `additionalAttributes` column: updated `_KNOWN_OPTIONAL_EVENT_COLUMNS`, `audit_query.sql`, the Technical Reference, and a test
- [ ] If you edited any `docs/*.md`: regenerated the matching `.docx` (`make docs`)

## Commit style

Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).

## Releases

Maintainer-only. Bump `pyproject.toml` + `src/anaplan_audit/__init__.py`,
move CHANGELOG entries to the new version, tag `vX.Y.Z`, `uv build`, and
attach the wheel + sdist to a GitHub release. Full steps in the
developer guide.
