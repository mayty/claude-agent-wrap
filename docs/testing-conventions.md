<!-- This file has been created with the assistance of an AI tool. -->
# Testing Conventions

How tests are organized, mocked, and written in this project. All code contributions must follow these rules.

## Test placement

- **Domain service tests** live in `agent_wrap/domain/<subpackage>/tests/` alongside their
  service. Example: tests for `LaunchService` → `agent_wrap/domain/launch/tests/test_launch.py`.
- **CLI tests** live in `agent_wrap/cli/<command>/tests/` and test ONLY argument parsing and
  the domain-layer calling protocol (which `services.xxx` method is called, with what
  arguments). Never test domain logic through the CLI.
- **Lib tests** live in `agent_wrap/lib/tests/`.

## Test structure

- **No test classes.** Tests are flat, top-level `def test_...():` functions —
  never grouped inside `class Test...:` blocks. Shared setup that would have
  lived in a class attribute or `setup_method` belongs in a module-level
  `@pytest.fixture` instead; shared state is passed explicitly as a function
  parameter (never `self.<attr>`).
- Rely on descriptive `test_<subject>_<scenario>` names for grouping, not
  comment banners — banners are an antipattern. If a file's test groups are
  genuinely distinct concerns, split them into separate files instead
  (e.g. per-command completion tests live in each command's own
  `tests/` directory, not bundled into one file).

## Test isolation

- **CLI tests**: mock `services.<xxx>_service` (the singleton on `agent_wrap.containers`)
  and verify call signatures. Never import from `agent_wrap.domain.*`.
- **Domain service tests**: instantiate the service under test directly; mock ALL
  constructor-injected neighbors with `mocker.Mock(spec=NeighborClass)` or
  `mocker.create_autospec(NeighborClass)`.
## Mocking

- Always use spec-based mocks: `mocker.Mock(spec=RealClass)`,
  `mocker.create_autospec(RealClass)`, or `mocker.patch("module.func", autospec=True)`.
- Never use bare `mocker.Mock()`, `mocker.MagicMock()`, or
  `type("MockName", (), {})()`.
- For subprocess results, use `mocker.Mock(spec=["returncode", "stdout", "stderr"])`.

## Fixtures (not helpers)

- Test setup belongs in `@pytest.fixture` or conftest fixtures.
- Never define module-level "helper construction functions" (`_make_service()`,
  `_setup_temp_paths()`) or module-level service instances (`_service = SomeService()`).
- Module-level constants for test data (strings, tuples, dicts) are fine.

## CLI-layer mocking boundary (non-negotiable)

- **CLI tests MUST mock at the `services.xxx_service` boundary, never at
  `agent_wrap.domain.*` internals.** Never use
  `mocker.patch("agent_wrap.domain.launch.launch.resolve_image")` — the
  CLI must never reach into domain internals.
- **The CLI conftest (`agent_wrap/cli/conftest.py`) MUST use an autouse
  fixture that replaces ALL `services.*_service` attributes with
  spec-mocked instances.** Every CLI test starts with a fully mocked
  service layer — no test can accidentally call real domain code.
  Individual tests that need specific behavior or return values override
  the relevant mock further (e.g., `services.launch_service.launch.return_value = 0`).
- **Negative-path tests must verify the *reason* for failure** (check
  stderr/stdout for the expected message), not just the exit code. A test
  that only asserts `rc == 1` can pass for the wrong reason.

## Parametrization

- When two or more tests exercise the same scenario and differ only in
  input/expected values, unify them with `@pytest.mark.parametrize`.
  Tests that share the same body but cover *different* scenarios should
  remain separate.
- Parametrize argument names must be consistent across the decorator and
  function signature (per Ruff PT006).

## Subtests

- Prefer `@pytest.mark.parametrize` over subtests whenever possible. Use
  subtests only when parametrization is not enough — for example, when
  inputs are generated dynamically or each iteration needs different
  assertion logic.
- When a test loops over inputs and asserts per-iteration, wrap each
  iteration with the `pytest-subtests` plugin so that one failure does
  not hide subsequent ones. Never use `unittest.TestCase` or
  `self.subTest(...)` — this is a pytest-only project.
- Add `pytest-subtests` as a dev dependency; use the `subtests`
  fixture it provides:
  ```python
  def test_something(subtests):
      for item in items:
          with subtests.test(msg=str(item)):
              assert ...
  ```
