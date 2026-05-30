# This file has been created with the assistance of an AI tool.
# agent-wrap QA targets.
# Strict targets (for CI): lintcheck, format-check, typecheck, test, check
# Fix targets (for dev):    lint, format

.PHONY: test lint lintcheck format format-check typecheck check

test:
	python3 -m pytest

lint:
	python3 -m ruff check --fix .

lintcheck:
	python3 -m ruff check --output-format=github .

format:
	python3 -m ruff format .

format-check:
	python3 -m ruff format --check --diff .

typecheck:
	python3 -m pyrefly check .

check: lintcheck format-check test typecheck
