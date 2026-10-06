# Development

This is a concise local-development guide for contributors. Do not commit `.env`, credentials, runtime data or generated artifacts.

## Prerequisites

- macOS for native app development.
- Python 3.13 recommended.
- Docker Desktop for the full service stack.
- Swift command-line tools for native checks.
- `uv` (0.11+) for the Python toolchain. Install surface is `uv sync` from `pyproject.toml` + `uv.lock`.

## Setup

```sh
python3 scripts/setup.py
uv sync --group dev
uv run playwright install chromium
```

Copy or edit only local configuration files as needed. Keep real provider keys in `.env` or the system keychain and never paste them into issues, pull requests or logs.

Optional page allowlist (task grant only; empty refuses every page):

```sh
# in .env
ORBIT_BROWSER_HOSTS=example.com
```

## Run Services

```sh
docker compose up -d --build
uv run python scripts/launch.py
```

For Python-only iteration with Redis in Docker:

```sh
docker compose stop task voice automation research decision room
docker compose -f compose.yaml -f compose.direct.yaml up -d redis
uv run python scripts/run.py all
```

## Checks

Run the checks relevant to your change before opening a pull request:

```sh
uv run pytest -q
uv run ruff check shared services tests scripts
bash native/check.sh
```

Broker and worker smoke (no provider inference):

```sh
uv run python scripts/smoke.py
uv run python scripts/smoke.py --workers
```

End-to-end harness with HTML report (uses local `.env` BYOK; does not print secrets):

```sh
uv run python scripts/e2e_harness.py
# Report: var/e2e-report.html
```

## Pi

Pipecat and other second voice runtimes stay out of this tree. See `ARCHITECTURE.md` refuse list. Any Pi integration note is documentation-only; do not add a second voice runtime or host shell.

## Generated Files

Keep generated files out of Git unless they are deliberately sanitized fixtures. Runtime state, builds, validation output, logs and private drafts should remain ignored.
