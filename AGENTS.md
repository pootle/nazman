# AGENTS.md

Guidance for AI agents (e.g. opencode) working on the NAZMan codebase.

## Project

NAZMan is a web-based management system for ZFS NAS administration on Ubuntu
Server / Raspberry Pi OS. Backend is FastAPI (Python), frontend is vanilla
HTML/CSS/JS served via Jinja2, storage is SQLite (WAL mode).

## Branches

- `dev`: the only branch code is written on. Every change — including install
  and build tooling — lands here first and is tested here.
- `main`: a release label. It is never edited; its tree always equals `dev`'s.
  Pushing to `main` directly is not part of the normal flow (a CI drift check
  fails any `main` push whose tree differs from `dev`).

Work on `dev`; releases are a forward merge of `dev` into `main`.

### Releasing to main

`main`'s tree must always equal `dev`'s tree. To release:

1. Verify `dev`: `./venv/bin/python -m pytest tests/ -q` (CI checks this too).
2. `git checkout main && git merge --no-ff dev && git push origin main`.
3. Return to `dev`.

The deployed server stays lean without branch surgery: `build.sh`/`deploy.sh`
copy only the files listed in `deploy.txt`, so tests and dev tooling that live
in the repo are never shipped to `/opt/nazman`.

## Commands

```bash
# Set up the dev venv (creates ./venv)
./dev-env.sh

# Run the test suite
./venv/bin/python -m pytest tests/ -q

# Live-test the production setup on this machine (sudo; installs ZFS on setup)
sudo ./dev-live.sh setup     # full provision: prepare.sh + build.sh -> /opt/nazman + systemd
sudo ./dev-live.sh update    # deploy.sh: copy changed files to /opt/nazman + restart
./dev-live.sh status         # service status + HTTP check
./dev-live.sh logs -f        # journalctl -u nazman
```

## Structure

- `nazman/` — Python application package
  - `api/` — FastAPI route handlers under `/api/*`
  - `managers/` — business logic (ZFS, disk, NFS, SMB, backup, scheduler, metrics)
  - `services/` — cross-domain composition (destruction, disk view) that may touch several managers
  - `models/` — SQLAlchemy ORM models
  - `utils/` — subprocess wrappers, validation, exceptions, command log, guide renderer,
    stable device identity (`devices.py`), shared ZFS queries (`zfs_query.py`),
    shared anon-user provisioning (`provisioning.py`)
  - `wiring.py` — builds the object graph (managers/services with collaborators injected)
    and exposes FastAPI `Depends` providers; database DDL migrations live in `migrations.py`
- `static/` — CSS/JS frontend
- `templates/` — Jinja2 HTML templates
- `docs/` — user guide Markdown sources served at `/guide` (`nazman/utils/guide.py`)
- `tests/` — pytest suite

## Conventions

- The package/import root is `nazman`. Never revert to the legacy `nasman` name.
- Input validation lives in `nazman/utils/validation.py`; reuse validators rather
  than duplicating checks in route handlers.
- Subprocess calls go through `nazman/utils/commands.py` wrappers
  (`run_command`, `run_zpool`, `run_zfs`, `run_pipeline`) so they are audited and
  testable. Do not call external CLI tools directly.
- Managers are plain classes with **no module-level singletons**; collaborators
  are injected in `wiring.build_container()` and reached from routes via the
  `Depends` providers in `nazman/wiring.py`. Manager modules must not import one
  another or the API layer — put shared logic in `utils/` (e.g. `devices.py`,
  `zfs_query.py`, `provisioning.py`) and cross-domain orchestration in
  `services/` (e.g. `destruction.py`, `disk_view.py`).
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
