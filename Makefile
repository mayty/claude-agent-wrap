# This file has been edited with the assistance of an AI tool.
# agent-wrap QA targets.

.PHONY: install test test-live lint lintcheck format format-check typecheck markdown-check arch-check cli-check check-executables python-check carveout-check uv-check constraints-check dump-prod-constraints available-upgrades upgrade-deps check projects-db

# Every target runs on the venv bin/agent-bootstrap provisioned, never on the host's
# python3; a `python3` fallback here would quietly undo owning the interpreter.
PY_VENV := $(shell [ -f .python/current-venv ] && cat .python/current-venv)
# `:=`, not `?=`: an exported PYTHON in the developer's environment (node-gyp and friends
# claim that name) must not silently redirect the QA targets off the pinned interpreter.
# `make PYTHON=... test` still overrides.
PYTHON := .python/$(PY_VENV)/bin/python3

# uv cannot be told an interpreter the way $(PYTHON) can: it resolves `requires-python`
# from its own managed installs and PATH, neither of which is where the bootstrap unpacks
# this checkout's interpreter. Left to itself it downloads a *second* copy of the pinned
# version, or fails with "No interpreter found for Python 3.14.7". Handed an environment
# that already exists it adopts that and asks nothing. Exported rather than spelled
# `--python` per call, so a uv invocation added later cannot forget it.
export UV_PROJECT_ENVIRONMENT := .python/$(PY_VENV)

# The two regions that do NOT run on the pinned interpreter, and the floor each must stay
# inside: ops/statusline.py on the agent container's python3, litellm_runtime/ inside the
# pinned LiteLLM image. Both are the versions actually running -- re-read them whenever
# either image is bumped, from the RUNNING process rather than `python3 -V`, since PATH
# can resolve elsewhere (the LiteLLM image fronts python3.13 with a venv shim, and only
# /proc/1/exe says so):
#   docker run --rm claude-agent python3 -V                     -> 3.14.4
#   docker exec agent-wrap-litellm-<provider> \
#     sh -c 'readlink -f /proc/1/exe; /proc/1/exe -V'            -> 3.13.15
# The first command names the base image on purpose: the statusline's floor is the apt
# python3, which is all every other project's container has, and running it from inside
# THIS project's dev container would answer for the pinned interpreter instead. The two
# now agree on the minor and differ only in the patch, so that mistake no longer looks
# like one.
#
# These feed the pyrefly leg of carveout-check only. Ruff reads the same two floors from
# [tool.ruff.per-file-target-version] in pyproject.toml -- a bump has to move both.
CARVEOUT_STATUSLINE_PATHS   := ops/statusline.py
CARVEOUT_STATUSLINE_VERSION := 3.14
CARVEOUT_RUNTIME_PATHS      := agent_wrap/domain/providers/litellm_runtime/*.py
CARVEOUT_RUNTIME_VERSION    := 3.13

# Files that MUST be executable. Hardcoded on purpose: deriving the list from
# git's recorded modes would be circular — a dropped bit flips git to 100644
# too, so the comparison would always pass and catch nothing.
EXECUTABLES := bin/agent bin/agent-bootstrap ops/statusline.py ops/telegram-notify.sh ops/validate-dockerfile-agent ops/wl-paste-shim

# The contributor entry point. Thin on purpose: bin/agent-bootstrap --dev owns the work,
# because the provisioner the shipped CLI depends on must stay runnable on a host with
# neither make nor a checkout of this file.
#
# The one target that must not name $(PYTHON) or $(PY_VENV). Both are `:=` and resolve at
# parse time, so on the unprovisioned checkout this target exists to fix, they are empty.
install:
	bin/agent-bootstrap --dev

test:
	$(PYTHON) -m pytest --cov=agent_wrap

# The tests against real third-party pages, which `test` deselects. Not part of `check`:
# they fail offline and whenever a page changes, neither of which a local change causes.
# The nightly workflow runs them.
test-live:
	$(PYTHON) -m pytest -m live

lint:
	$(PYTHON) -m ruff check --fix --unsafe-fixes .

lintcheck:
	$(PYTHON) -m ruff check --output-format=github .

format:
	$(PYTHON) -m ruff format .

format-check:
	$(PYTHON) -m ruff format --check --diff .

typecheck:
	$(PYTHON) -m pyrefly check --python-interpreter-path $(PYTHON) .

markdown-check:
	$(PYTHON) scripts/validate-markdown-links.py

arch-check:
	$(PYTHON) scripts/validate-architecture.py

# Assert docs/shell-commands.md still covers every verb and flag in the click tree.
# Coverage only -- nothing here compares wording.
cli-check:
	$(PYTHON) scripts/validate-cli-docs.py

# Checks BOTH the working-tree filesystem bit (so PATH invocation works locally) AND git's
# recorded mode (so fresh clones get an executable file). A file can have one without the
# other, so both must be asserted.
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

# Without this, a wrong interpreter surfaces as a wall of unrelated test failures. Also
# asserts python-pin.env and requires-python still agree: a bump touching only one would
# leave the two describing different interpreters.
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

# Only the pyrefly leg lives here. Both ruff legs are carried by `pyproject.toml`'s
# [tool.ruff.per-file-target-version], which lintcheck and format-check already honour
# file by file. pyrefly has no per-file equivalent, so the floors above are still needed
# to catch stdlib APIs that do not exist yet on them (datetime.UTC).
# $(1) = paths, $(2) = floor (e.g. 3.12)
define carveout_legs
	$(PYTHON) -m pyrefly check --python-interpreter-path $(PYTHON) \
		--python-version $(2) $(1)
endef

carveout-check:
	$(call carveout_legs,$(CARVEOUT_STATUSLINE_PATHS),$(CARVEOUT_STATUSLINE_VERSION))
	$(call carveout_legs,$(CARVEOUT_RUNTIME_PATHS),$(CARVEOUT_RUNTIME_VERSION))

# uv is a developer tool only, so the targets needing it declare that as a prerequisite
# rather than failing halfway through a recipe -- or worse, inside a pipeline where the
# exit code is the tail command's. Paired with python-check everywhere, because
# UV_PROJECT_ENVIRONMENT points uv at a venv only `make install` creates.
uv-check:
	@command -v uv >/dev/null 2>&1 || { printf 'uv not found. See docs/getting-started.md\n' >&2; exit 1; }

# `--no-header` is what makes the output a pure function of uv.lock: uv's own header
# stamps the invoking command line in, including the -o path, so a check that exported
# anywhere else would see a phantom diff forever.
CONSTRAINTS_HEADER := \# Generated from uv.lock by `make dump-prod-constraints` -- do not edit.
CONSTRAINTS_EXPORT := uv export --locked --quiet --no-dev --no-emit-project --no-header --format requirements.txt

# Read-only over uv.lock by design: --locked refuses to re-resolve, so a lock that
# drifted from pyproject.toml fails loudly instead of being silently repaired into
# whatever today's index offers.
# Written via a temp file and renamed: `>` truncates before uv runs, so a refusal would
# otherwise leave a header and nothing else -- a constraints file installing nothing.
dump-prod-constraints: uv-check python-check
	@{ printf '%s\n' '$(CONSTRAINTS_HEADER)'; $(CONSTRAINTS_EXPORT); } > bin/requirements.txt.tmp \
		|| { rm -f bin/requirements.txt.tmp; exit 1; }
	@mv bin/requirements.txt.tmp bin/requirements.txt
	@printf 'wrote bin/requirements.txt\n'

# Two ways to forget: `uv lock --check` catches a pyproject.toml edit that was never
# locked, the diff catches a lock that was never dumped. Neither re-resolves, so the
# rolling exclude-newer window cannot make this fail spuriously.
#
# Compares content, not `git status`, so the answer is the same whether the regenerated
# file has been committed yet or not.
constraints-check: uv-check python-check
	uv lock --check
	@{ printf '%s\n' '$(CONSTRAINTS_HEADER)'; $(CONSTRAINTS_EXPORT); } \
		| diff -u bin/requirements.txt - > /dev/null || { \
		printf 'bin/requirements.txt does not match uv.lock.\n' >&2; \
		printf 'Run: make dump-prod-constraints\n' >&2; \
		exit 1; \
	}

# --dry-run: uv reports what would move and writes no file. Shaped by `exclude-newer`, so
# a release published this week is deliberately not offered yet.
available-upgrades: uv-check python-check
	uv lock --upgrade --dry-run

# Move every dependency to the newest release the floors and the exclude-newer cooldown
# allow, leaving all three dependency artifacts agreeing again.
#
# --frozen on each `uv tree`: the prod pipe edits pyproject.toml, which makes the lock
# stale for the dev pipe, and without it uv would quietly re-resolve mid-recipe.
# The second `uv lock` is not cosmetic: the rewritten floors leave `uv lock --check`
# failing until it runs. It cannot move a resolved version, since every new floor is the
# version already locked. Stops at the files; `make install` applies them.
upgrade-deps: uv-check python-check
	uv lock --upgrade
	uv tree --frozen --no-dev --depth 1 | $(PYTHON) scripts/sync-dependencies.py prod
	uv tree --frozen --only-dev --depth 1 | $(PYTHON) scripts/sync-dependencies.py dev
	uv lock
	$(MAKE) dump-prod-constraints

check: python-check constraints-check lintcheck format-check test typecheck markdown-check arch-check cli-check carveout-check check-executables

projects-db:
	duckdb -readonly .agent-launches/db/projects.db
