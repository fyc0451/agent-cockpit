# Contributing

Thanks for helping improve Agent Cockpit.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Before submitting a pull request, run the full tests, validate Python syntax, and
check the inline frontend script:

```bash
.venv/bin/pytest -q
python -m compileall -q *.py tests
sed -n '/<script>$/,/^<\/script>$/p' static/index.html | sed '1d;$d' | node --check -
git diff --check
```

Keep changes focused, add a regression test for bug fixes, and never commit `.env`,
tokens, passwords, SQLite databases, uploaded files, or other runtime data.

Security reports should follow [SECURITY.md](SECURITY.md), not public issues.
