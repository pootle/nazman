# NAZMan development Makefile
# Run `make` targets from the repository root (dev branch).

VENV   := venv
PY     := $(VENV)/bin/python

.PHONY: help setup deps test test-watch dev lint clean

help:
	@echo "NAZMan dev targets:"
	@echo "  make setup    Create ./venv and install deps + test tooling (dev-env.sh)"
	@echo "  make deps     (Re)install Python deps into ./venv"
	@echo "  make test     Run the pytest suite"
	@echo "  make dev      Run the dev server with auto-reload"
	@echo "  make lint     Run pyflakes/unused check via python -m pyflakes"
	@echo "  make clean    Remove ./venv and ./dev runtime dirs"

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

lint:
	$(VENV)/bin/python -m pyflakes nazman/ tests/ 2>/dev/null || \
	$(VENV)/bin/pip install pyflakes && $(VENV)/bin/python -m pyflakes nazman/ tests/

clean:
	rm -rf $(VENV) dev .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
