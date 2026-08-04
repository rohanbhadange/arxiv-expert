# Contributing

Thanks for helping improve arXiv Expert.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Run the local quality gates before opening a pull request:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/coverage run -m pytest
.venv/bin/coverage report
.venv/bin/python -m build
```

Tests must not contact arXiv or OpenAI. Use the existing fakes for normal test
coverage. `scripts/live_smoke.py` is an explicit, opt-in integration check that
uses the configured OpenAI account and cleans up its temporary remote resources.

## Pull requests

- Keep changes focused and explain the user-visible behavior.
- Add tests for new behavior and regressions.
- Preserve the official-category-only and forward-only ingestion policies.
- Never commit API keys, local databases, logs, PDFs, or chat transcripts.
- Treat changes to networking, credential storage, deletion, and schedulers as
  security-sensitive and document their validation.
