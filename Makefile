# This file has been created with the assistance of an AI tool.
# agent-wrap QA targets.
# Strict targets (for CI): lintcheck format-check test typecheck markdown-check arch-check check-executables
# Fix targets (for dev):    lint, format

.PHONY: test lint lintcheck format format-check typecheck markdown-check arch-check check-executables check

# Files that MUST be executable. Hardcoded on purpose: deriving the list from
# git's recorded modes would be circular — a dropped bit flips git to 100644
# too, so the comparison would always pass and catch nothing.
EXECUTABLES := bin/agent ops/statusline.py ops/telegram-notify.sh ops/validate-dockerfile-agent ops/wl-paste-shim

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

arch-check:
	python3 scripts/validate-architecture.py

# Fail if any required executable lost its executable bit. Checks BOTH the
# working-tree filesystem bit (so PATH invocation works locally) AND git's
# recorded index mode (100755 — so fresh clones get an executable file). A
# file can have one without the other (e.g. `git update-index --chmod`, or a
# checkout on a filesystem that drops modes), so both must be asserted.
check-executables:
	@fail=""; \
	for f in $(EXECUTABLES); do \
		if [ ! -x "$$f" ]; then \
			fail="$$fail\n  $$f: filesystem bit missing -> chmod +x $$f"; \
		fi; \
		mode=$$(git ls-files -s -- "$$f" | awk '{print $$1}'); \
		if [ -z "$$mode" ]; then \
			fail="$$fail\n  $$f: not tracked by git"; \
		elif [ "$$mode" != "100755" ]; then \
			fail="$$fail\n  $$f: git mode $$mode, expected 100755 -> git update-index --chmod=+x $$f"; \
		fi; \
	done; \
	if [ -n "$$fail" ]; then \
		printf 'Executable check failed:%b\n' "$$fail"; \
		exit 1; \
	fi

check: lintcheck format-check test typecheck markdown-check arch-check check-executables
