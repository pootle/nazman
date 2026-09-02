# AGENTS.md

Guidance for AI agents (e.g. opencode) working on the NAZMan codebase.

## Project

NAZMan is a web-based management system for ZFS NAS administration on Ubuntu
Server / Raspberry Pi OS. Backend is FastAPI (Python), frontend is vanilla
HTML/CSS/JS served via Jinja2, storage is SQLite (WAL mode).

## Branches

- `main`: production artifact. App code + deployment scripts only, no tests.
- `dev`: primary development branch. Adds tests, dev-env.sh, Makefile, CI.

Work on `dev`; only merge production changes into `main`.

## Commands

```bash
# Set up the dev venv (creates ./venv)
./dev-env.sh

# Run the test suite
./venv/bin/python -m pytest tests/ -q

# Run the dev server with auto-reload
./venv/bin/uvicorn nazman.main:app --reload --host 0.0.0.0 --port 8080
```

## Structure

- `nazman/` — Python application package
  - `api/` — FastAPI route handlers under `/api/*`
  - `managers/` — business logic (ZFS, disk, NFS, SMB, backup, scheduler, metrics)
  - `models/` — SQLAlchemy ORM models
  - `utils/` — subprocess wrappers, validation, exceptions, command log
- `static/` — CSS/JS frontend
- `templates/` — Jinja2 HTML templates
- `tests/` — pytest suite

## Conventions

- The package/import root is `nazman`. Never revert to the legacy `nasman` name.
- Input validation lives in `nazman/utils/validation.py`; reuse validators rather
  than duplicating checks in route handlers.
- Subprocess calls go through `nazman/utils/commands.py` wrappers
  (`run_command`, `run_zpool`, `run_zfs`, `run_pipeline`) so they are audited and
  testable. Do not call external CLI tools directly.
- ZFS is the source of truth for pools/datasets/NFS; do not duplicate that state
  in the database.
- Disk identity uses stable `/dev/disk/by-id/` paths, never ephemeral kernel
  names. Partition slots are marked with a `nazman:<uuid>` GPT PARTLABEL.
- Use async/await throughout (FastAPI + async subprocess).
- No code comments unless they add real context (project style).
- Keep production dependencies in `requirements.txt`; test tooling (pytest,
  pytest-asyncio) is dev-only.

## Tests

- Run `./venv/bin/python -m pytest tests/ -q` before considering work done.
- The test suite overrides settings with temp dirs and disables auth; it never
  touches `/etc/nazman` or real ZFS state.
