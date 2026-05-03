# Contributing

Use small, reviewable changes and keep provider-specific behavior covered by
offline fixtures.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Checks

Run before pushing:

```bash
pre-commit run --all-files
pytest -q
```

## Provider Fixtures

When a provider changes its session file format, add a minimized fixture to the
tests instead of committing real session data. Fixtures must use synthetic
prompts, synthetic paths, and fake identifiers.
