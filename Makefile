# This file has been edited with the assistance of an AI tool.
# agent-wrap QA targets.
# Strict targets (for CI): lintcheck format-check test typecheck markdown-check arch-check check-executables
# Fix targets (for dev):    lint, format
# Dependency targets:       available-upgrades, upgrade-deps, dump-prod-constraints

.PHONY: install test lint lintcheck format format-check typecheck markdown-check arch-check check-executables python-check carveout-check uv-check constraints-check dump-prod-constraints available-upgrades upgrade-deps check

# Every target runs on the venv bin/agent-bootstrap provisioned, never on the host's
# python3 -- that is the whole point of owning the interpreter, and a `python3`
# fallback here would quietly undo it. One relative path is correct in all three
# contexts: on the host, in CI, and in the dev container, whose own .python/ tree
# shadows the host's at the same path (see .claude-agent-wrap/Dockerfile).
# Unprovisioned, PY_VENV is empty and python-check says so in one line.
PY_VENV := $(shell [ -f .python/current-venv ] && cat .python/current-venv)
# `:=`, not `?=`: an exported PYTHON in the developer's environment (node-gyp and
# friends claim that name) must not silently redirect the QA targets off the pinned
# interpreter. A deliberate one-off is unaffected -- `make PYTHON=... test` overrides
# this regardless of the operator.
PYTHON := .python/$(PY_VENV)/bin/python3

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

# The contributor entry point, and the only spelling of it: docs, CI, the dev container
# and python-check's own failure messages all say `make install`. Deliberately thin --
# bin/agent-bootstrap --dev owns the work, because the provisioner the shipped CLI depends
# on has to stay runnable on a host with neither make nor a checkout of this file. Takes
# no prefix argument: every caller provisions into the checkout's own .python/.
#
# The one target that must not name $(PYTHON) or $(PY_VENV). Both are `:=` and resolve at
# parse time, so on the unprovisioned checkout this target exists to fix, they are empty.
install:
	bin/agent-bootstrap --dev

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
		if [ -z "$(PY_VENV)" ]; then \
			printf 'No provisioned interpreter. Run: make install\n' >&2; \
		else \
			printf '%s is missing. Run: make install\n' "$(PYTHON)" >&2; \
		fi; \
		exit 1; \
	fi
	@if ! $(PYTHON) -m pytest --version >/dev/null 2>&1; then \
		printf 'The venv has no dev tooling. Run: make install\n' >&2; \
		exit 1; \
	fi
	@. ./python-pin.env; \
	running=$$($(PYTHON) -c 'import sys; print(sys.version.split()[0])'); \
	if [ "$$running" != "$$AGENT_PY_VERSION" ]; then \
		printf '%s is Python %s, but python-pin.env pins %s -> make install\n' \
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

# uv is a developer tool only -- nothing on the end-user path needs it -- so the targets
# that do need it declare that as a prerequisite instead of failing halfway through a
# recipe, or worse, inside a pipeline where the exit code is the tail command's. Phony,
# so make runs it at most once per invocation however many of them are in the goal list.
uv-check:
	@command -v uv >/dev/null 2>&1 || { printf 'uv not found. See docs/getting-started.md\n' >&2; exit 1; }

# The exported constraint set, to stdout. `--no-header` is what makes the output a
# pure function of uv.lock: uv's own header stamps the invoking command line into the
# file, including the -o path, so a check that exported anywhere else would see a
# phantom diff forever. The provenance line is written by dump-prod-constraints below
# instead, where it is a fixed string both targets can reproduce.
CONSTRAINTS_HEADER := \# Generated from uv.lock by `make dump-prod-constraints` -- do not edit.
CONSTRAINTS_EXPORT := uv export --locked --quiet --no-dev --no-emit-project --no-header --format requirements.txt

# Regenerate the constraints the bootstrap installs. Read-only over uv.lock by
# design: --locked asserts the lock is already current and refuses to re-resolve, so
# changing a dependency is always two deliberate steps -- `uv add` / `uv lock` first,
# then this. A lock that drifted from pyproject.toml fails here loudly instead of
# being silently repaired into whatever today's index happens to offer.
# uv is a developer tool only; nothing on the end-user path needs it.
# Written via a temp file and renamed, not straight to the destination: `>` truncates
# before uv runs, so a refusal (a stale lock, no network) would otherwise leave behind
# a header and nothing else -- a constraints file that installs no dependencies at all.
dump-prod-constraints: uv-check
	@{ printf '%s\n' '$(CONSTRAINTS_HEADER)'; $(CONSTRAINTS_EXPORT); } > bin/requirements.txt.tmp \
		|| { rm -f bin/requirements.txt.tmp; exit 1; }
	@mv bin/requirements.txt.tmp bin/requirements.txt
	@printf 'wrote bin/requirements.txt\n'

# Two legs, because there are two ways to forget. `uv lock --check` catches a
# pyproject.toml edit that was never locked; the diff catches a lock that was never
# dumped. Neither re-resolves, so the rolling exclude-newer window cannot make this
# fail spuriously -- uv records the span (P7D), not a resolved instant.
#
# Compares content, not `git status`: the answer must be the same whether the
# regenerated file has been committed yet or not, so that the natural order --
# uv lock, make dump-prod-constraints, make check, commit -- passes at every step.
constraints-check: uv-check
	uv lock --check
	@{ printf '%s\n' '$(CONSTRAINTS_HEADER)'; $(CONSTRAINTS_EXPORT); } \
		| diff -u bin/requirements.txt - > /dev/null || { \
		printf 'bin/requirements.txt does not match uv.lock.\n' >&2; \
		printf 'Run: make dump-prod-constraints\n' >&2; \
		exit 1; \
	}

# What would move if the lock were re-resolved today, and nothing else: --dry-run means
# uv reports the upgrade and writes no file. The answer is shaped by `exclude-newer` in
# pyproject.toml, so a release published this week is deliberately not offered yet.
available-upgrades: uv-check
	uv lock --upgrade --dry-run

# Move every dependency to the newest release the declared floors and the exclude-newer
# cooldown allow, and leave all three dependency artifacts agreeing again.
#
# Both guards are load-bearing: uv-check for the resolver, python-check for the
# interpreter sync-dependencies.py runs on -- the pinned one, never a host python3.
# --frozen on each `uv tree`: the prod pipe edits pyproject.toml, which makes the lock
# stale for the dev pipe, and without it uv would quietly re-resolve mid-recipe.
# The second `uv lock` is not cosmetic: uv.lock records the declared requirements, so the
# rewritten floors leave `uv lock --check` -- and therefore `make check` -- failing until
# it runs. It cannot move a resolved version, since every new floor is the version already
# locked. Stops at the files; `make install` applies them to the venv when you want it.
upgrade-deps: uv-check python-check
	uv lock --upgrade
	uv tree --frozen --no-dev --depth 1 | $(PYTHON) scripts/sync-dependencies.py prod
	uv tree --frozen --only-dev --depth 1 | $(PYTHON) scripts/sync-dependencies.py dev
	uv lock
	$(MAKE) dump-prod-constraints

check: python-check constraints-check lintcheck format-check test typecheck markdown-check arch-check carveout-check check-executables
