# This file has been edited with the assistance of an AI tool.
# agent-wrap QA targets.
# Strict targets (for CI): lintcheck format-check test typecheck markdown-check arch-check check-executables
# Fix targets (for dev):    lint, format

.PHONY: test lint lintcheck format format-check typecheck markdown-check arch-check check-executables python-check carveout-check check

# Every target runs on the interpreter bin/agent-bootstrap provisioned, never on
# the host's python3 -- that is the whole point of owning the interpreter, and a
# `python3` fallback here would quietly undo it. One relative path is correct in
# all three contexts: on the host, in CI, and in the dev container, whose own
# .python/ tree shadows the host's at the same path (see .claude-agent-wrap/Dockerfile).
# Unprovisioned, PY_SLUG is empty and python-check says so in one line.
PY_SLUG := $(shell [ -f .python/current ] && cat .python/current)
# `:=`, not `?=`: an exported PYTHON in the developer's environment (node-gyp and
# friends claim that name) must not silently redirect the QA targets off the pinned
# interpreter. A deliberate one-off is unaffected -- `make PYTHON=... test` overrides
# this regardless of the operator.
PYTHON := .python/venv-$(PY_SLUG)/bin/python3

# The two regions that do NOT run on the pinned interpreter, and the floor each
# must stay inside. ops/statusline.py runs on the agent container's python3;
# litellm_runtime/ runs inside the pinned LiteLLM image. Both numbers are the
# versions actually running, not a conservative guess -- re-read them whenever
# either image is bumped. Read the RUNNING process, not `python3 -V`: PATH can
# resolve to a different interpreter than the one hosting the code (the LiteLLM
# image fronts /usr/bin/python3.13 with a venv shim, and only /proc/1/exe says so).
#   docker run --rm claude-agent python3 -V                     -> 3.12.3
#   docker exec agent-wrap-litellm-<provider> \
#     sh -c 'readlink -f /proc/1/exe; /proc/1/exe -V'            -> 3.13.15
# 3.12 stays the statusline's floor even though THIS project's own dev container
# fronts that python3 with the pinned interpreter on PATH (see
# .claude-agent-wrap/Dockerfile): every other project's container has only the
# base image's apt python3, which is where the statusline has to keep running.
CARVEOUT_STATUSLINE_PATHS   := ops/statusline.py
CARVEOUT_STATUSLINE_VERSION := 3.12
CARVEOUT_RUNTIME_PATHS      := agent_wrap/domain/providers/litellm_runtime/*.py
CARVEOUT_RUNTIME_VERSION    := 3.13

# Files that MUST be executable. Hardcoded on purpose: deriving the list from
# git's recorded modes would be circular — a dropped bit flips git to 100644
# too, so the comparison would always pass and catch nothing.
EXECUTABLES := bin/agent bin/agent-bootstrap ops/statusline.py ops/telegram-notify.sh ops/validate-dockerfile-agent ops/wl-paste-shim

test:
	$(PYTHON) -m pytest --cov=agent_wrap

lint:
	$(PYTHON) -m ruff check --fix --unsafe-fixes .

lintcheck:
	$(PYTHON) -m ruff check --output-format=github .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff format --target-version py$(subst .,,$(CARVEOUT_STATUSLINE_VERSION)) $(CARVEOUT_STATUSLINE_PATHS)
	$(PYTHON) -m ruff format --target-version py$(subst .,,$(CARVEOUT_RUNTIME_VERSION)) $(CARVEOUT_RUNTIME_PATHS)

format-check:
	$(PYTHON) -m ruff format --check --diff .

typecheck:
	$(PYTHON) -m pyrefly check --python-interpreter-path $(PYTHON) .

markdown-check:
	$(PYTHON) scripts/validate-markdown-links.py

arch-check:
	$(PYTHON) scripts/validate-architecture.py

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

# Fail fast and legibly when the interpreter is missing or is not the pinned one.
# Without this, a wrong interpreter surfaces as a wall of unrelated test failures.
# Also asserts the pin is stated consistently: python-pin.env drives the bootstrap,
# requires-python documents it for anything reading the metadata, and a bump that
# touches only one of them would leave the two describing different interpreters.
python-check:
	@if [ ! -x "$(PYTHON)" ]; then \
		if [ -z "$(PY_SLUG)" ]; then \
			printf 'No provisioned interpreter. Run: %s/bin/agent-bootstrap --dev\n' "$$PWD" >&2; \
		else \
			printf '%s is missing. Run: %s/bin/agent-bootstrap --dev\n' "$(PYTHON)" "$$PWD" >&2; \
		fi; \
		exit 1; \
	fi
	@. ./python-pin.env; \
	running=$$($(PYTHON) -c 'import sys; print(sys.version.split()[0])'); \
	if [ "$$running" != "$$AGENT_PY_VERSION" ]; then \
		printf '%s is Python %s, but python-pin.env pins %s -> bin/agent-bootstrap\n' \
			"$(PYTHON)" "$$running" "$$AGENT_PY_VERSION" >&2; \
		exit 1; \
	fi; \
	declared=$$(sed -n 's/^requires-python = "==\(.*\)"$$/\1/p' pyproject.toml); \
	if [ "$$declared" != "$$AGENT_PY_VERSION" ]; then \
		printf 'pyproject.toml requires-python is "==%s" but python-pin.env pins %s\n' \
			"$$declared" "$$AGENT_PY_VERSION" >&2; \
		exit 1; \
	fi

# Hold each carve-out region to its own floor. Three legs, all needed:
# ruff check catches version-gated syntax, pyrefly catches stdlib APIs that do not
# exist yet on that floor (datetime.UTC being the one this repo would otherwise
# have got wrong), and ruff format catches the formatter itself -- at py314 it
# strips the parentheses from `except (A, B):`, which is a SyntaxError on the
# interpreters these files actually run on.
# $(1) = paths, $(2) = floor (e.g. 3.12)
define carveout_legs
	$(PYTHON) -m ruff check --target-version py$(subst .,,$(2)) $(1)
	$(PYTHON) -m ruff format --check --diff --target-version py$(subst .,,$(2)) $(1)
	$(PYTHON) -m pyrefly check --python-interpreter-path $(PYTHON) \
		--python-version $(2) $(1)
endef

carveout-check:
	$(call carveout_legs,$(CARVEOUT_STATUSLINE_PATHS),$(CARVEOUT_STATUSLINE_VERSION))
	$(call carveout_legs,$(CARVEOUT_RUNTIME_PATHS),$(CARVEOUT_RUNTIME_VERSION))

check: python-check lintcheck format-check test typecheck markdown-check arch-check carveout-check check-executables
