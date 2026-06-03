# This file has been created with the assistance of an AI tool.
# agent-wrap QA targets.
# Strict targets (for CI): lintcheck, format-check, typecheck, test, check
# Fix targets (for dev):    lint, format

.PHONY: test lint lintcheck format format-check typecheck markdown-check check

test:
	python3 -m pytest --cov=agent_wrap

lint:
	python3 -m ruff check --fix --unsafe-fixes .

lintcheck:
	python3 -m ruff check --output-format=github .

format:
	python3 -m ruff format .

format-check:
	python3 -m ruff format --check --diff .

typecheck:
	python3 -m pyrefly check .

markdown-check:
	python3 scripts/validate-markdown-links.py

check: lintcheck format-check test typecheck markdown-check
