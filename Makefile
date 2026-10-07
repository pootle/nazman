# NAZMan development Makefile
# Run `make` targets from the repository root (dev branch).

VENV   := venv
PY     := $(VENV)/bin/python

.PHONY: help setup deps test test-watch dev live-setup live-update live-status live-logs lint clean

help:
	@echo "NAZMan dev targets:"
	@echo "  make setup       Create ./venv and install deps + test tooling (dev-env.sh)"
	@echo "  make deps        (Re)install Python deps into ./venv"
	@echo "  make test        Run the pytest suite"
	@echo "  make dev         Run the dev server with auto-reload (lightweight; port 8080)"
	@echo "  make live-setup  Provision the production setup on this machine (sudo; installs ZFS)"
	@echo "  make live-update Copy changed files to /opt/nazman + restart the service (sudo)"
	@echo "  make live-status Service status + HTTP check"
	@echo "  make live-logs   Follow the nazman service logs"
	@echo "  make lint        Run pyflakes/unused check via python -m pyflakes"
	@echo "  make clean       Remove ./venv and ./dev runtime dirs"

setup:
	./dev-env.sh

deps:
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt
	$(VENV)/bin/pip install pytest pytest-asyncio

test:
	$(PY) -m pytest tests/ -q

dev:
	$(VENV)/bin/uvicorn nazman.main:app --reload --host 0.0.0.0 --port 8080

live-setup:
	./dev-live.sh setup

live-update:
	./dev-live.sh update

live-status:
	./dev-live.sh status

live-logs:
	./dev-live.sh logs -f

lint:
	$(VENV)/bin/python -m pyflakes nazman/ tests/ 2>/dev/null || \
	$(VENV)/bin/pip install pyflakes && $(VENV)/bin/python -m pyflakes nazman/ tests/

clean:
	rm -rf $(VENV) dev .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
