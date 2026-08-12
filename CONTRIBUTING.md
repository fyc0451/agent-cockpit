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
python -m compileall -q *.py agent_cockpit agent_mail_commands scripts tests
sed -n '/<script>$/,/^<\/script>$/p' static/index.html | sed '1d;$d' | node --check -
git diff --check
```

Keep changes focused, add a regression test for bug fixes, and never commit `.env`,
tokens, passwords, SQLite databases, uploaded files, or other runtime data.

## Managed releases

All source releases must run as one complete child command under
[the managed-release procedure](docs/RELEASING.md). The lane serializes
publishers, rejects a stale `origin/main`, and writes the fixed candidate and
rollback SHA to a durable receipt. Do not push `main` or change the live service
outside that lane.

Security reports should follow [SECURITY.md](SECURITY.md), not public issues.
