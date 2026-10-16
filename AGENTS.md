# Repository Instructions

## Scope

These instructions apply to the entire repository.

Before making changes, read the applicable `AGENTS.md`, inspect the current implementation, relevant callers, tests, package metadata, and import rules, and check `git status` and existing diffs. Trace real callers, imports, and execution paths instead of relying only on docs or assumptions. Preserve unrelated worktree changes, and never use destructive Git commands to simplify the worktree.

By default, preserve existing public behavior. This project does not require API backward compatibility for an explicitly requested new contract or redesign; in that case, remove superseded fields and paths instead of retaining aliases, forwarding modules, compatibility adapters, parallel implementations, or data migrations unless explicitly requested.

## Repository Boundaries

Preserve the two distributions and the flat source layout:

- `packages/pleiad/src/` is installed as `pleiad`.
- `apps/weave/src/` is installed as `pleiad_weave`.

Pleiad owns reusable Agent Runtime mechanisms. Weave owns product composition, policy, persistence, interfaces, and deployment configuration. Weave must use the SDK through Pleiad's public API and must not duplicate or bypass its runtime mechanisms.

When ownership is unclear, use this rule:

- Reusable contract, algorithm, lifecycle, or default backend: Pleiad.
- Dobby-specific choice, authorization, product record, interface, UI, or deployment setting: Weave.

Preserve these identity rules:

- Weave `Execution` is the only durable product job and is identified by `execution_id`.
- A Pleiad Run is an SDK invocation, not another durable job.
- A Handoff remains within the same Run, Execution, and Session.
- A Worker with an independent lifecycle creates a child Execution with `parent_execution_id`.
- Message order, Turn order, tool-call/result pairing, Execution state, Graph state, and Trace state are separate facts and must not be collapsed.

See `docs/pleiad-architecture.md` for the reusable runtime design and `docs/weave-architecture.md` for product composition, persistence, and interfaces.

## Implementation Rules

- Make the smallest coherent change needed to establish the final boundary, and delete superseded paths.
- Confirm data structures, module boundaries, and interface contracts before implementing the execution flow. For defects, find the root cause and shared caller path instead of patching only the reported failure point.
- Reuse existing code, the standard library, and installed dependencies before writing replacements or adding dependencies.
- Prefer clear, direct code with limited duplication and nesting. Apply SOLID to clarify responsibilities and dependencies, but do not add interfaces or abstractions without a real use.
- Fix a contract at its owner, and migrate all affected callers and tests in the same change.
- Do not add an interface for a single implementation. Do not add factories, registries, service layers, or configuration switches for hypothetical needs.
- Keep Pleiad's public API small and provider-neutral. Do not expose Weave DTOs, persistence records, FastAPI or Textual types, or product configuration.
- Add dependencies only for current requirements, add them to the owning distribution, and update the corresponding `uv.lock`.
- Update the relevant architecture or developer documentation when public behavior changes.

## Python Rules

- Prefer modern Python 3.11+ syntax when it makes code clearer or simpler; avoid dense nested expressions, metaprogramming, and obscure tricks. This repository supports Python 3.12 only, and Ruff and strict mypy must pass.
- Every `__init__.py` must be completely empty.
- Never define or use `__all__`; import public symbols directly from their defining modules.
- Put every import at the top of the file. Do not use inline imports or wrap imports in `try`/`except`.
- Do not add functions, methods, variables, or attributes whose names start with `_`, except Python-required special methods such as `__init__`.
- Use precise type annotations. Do not use broad `Any`, unparameterized collections, blanket `# type: ignore`, or unchecked `cast` to hide design problems.
- Fully annotate public APIs. Do not use `del context` or rename an unused parameter to `_context` to silence a warning; change the interface or explain why the parameter must remain.
- Write public class, method, and function docstrings in English using Google style. Document purpose, parameters, returns, and raised exceptions as applicable; explain key constraints in complex internal logic without commenting every line.
- Do not add explanatory comments or module docstrings at file top level. Write code comments and docstrings in English.
- Use Pydantic at untrusted input, configuration, persistence, and public serialization boundaries. Prefer frozen, slotted dataclasses for validated internal values.
- Validate state transitions centrally; do not use exceptions for ordinary state transitions.
- Missing required data or contract violations must fail clearly; do not mask them with defaults such as `or ""`, `or {}`, or `or []`, old fields, or unrelated fields. Catch only specific exceptions this layer can handle and re-raise the rest. Never use empty `except` blocks, ignore errors, fabricate success, or silently call another LLM to replace an upstream failure. Fallback behavior requires an explicit business contract.
- Preserve the original exception as the cause when wrapping errors. Never swallow `asyncio.CancelledError`.
- Avoid circular dependencies and inspect the actual import chain after changes. Keep logs concise and preserve diagnostic context when logging exceptions. Logger calls must never use formatting placeholders such as `%s`, `{}`, or equivalent template markers, and must never pass interpolation arguments separately; construct the complete message before calling the logger.
- Use `asyncio.TaskGroup` for related child tasks. The component that creates a task owns its cancellation and cleanup.
- Use `sys.executable` for Python subprocesses. Do not perform synchronous network or subprocess work on the event loop.

Ruff owns formatting, import ordering, naming, docstring, and other configured style details. Do not duplicate or override Ruff configuration here.

## Testing and Verification

- When writing, reviewing, or fixing Python tests, use the local `testing-python` Skill first. If the Skill conflicts with this file or repository configuration, this file and repository configuration take precedence.
- Do not mechanically generate unit tests merely because code changed. First identify the observable behavior, public contract, or regression risk of the affected feature. If there is no behavior to verify, do not add a test.
- Each test must verify one functional behavior through a public interface or a real component boundary. The test name must state the failing product scenario clearly.
- Do not write tests that merely prove the implementation exists, such as checking that source code contains text, that a function was called, that code was written locally, or that a file created by the test exists.
- Assert file contents, paths, or persisted output only when persistence, resource generation, package contents, or the file lifecycle is itself part of the functional contract.
- Prefer extending the nearest existing test. Add a new test file only when existing tests cannot express the target behavior clearly.
- Use real project-owned implementations to verify collaboration between components. Use fakes, in-memory implementations, or mocks only at external boundaries such as model providers, networks, clocks, and processes. Do not mock the subject under test merely to restate its implementation.
- A non-trivial behavior change must leave behind the smallest behavior or contract test that would fail without the change. Do not add tests for formatting, documentation, or mechanical changes already covered completely by existing gates.
- Add behavior tests for changes to branches, parsing, or state transitions; when fixing a defect, prefer a regression test. For concurrency tests, use barriers or fault injection rather than fixed sleeps.
- Pleiad tests must not import Weave. Use deterministic in-memory dependencies and fake model transports by default.
- Default tests must not call real providers. Mark real-provider tests `provider_e2e` and OCI tests `oci`.
- Run focused tests while iterating, then run the affected project's quality gates before handoff.
- Run packaging tests when package metadata, exports, source layout, dependencies, or packaged resources change.
- Run platform-specific tests on their actual target platform.
- Report OCI, provider, packaging, transport-only, and skipped checks separately; do not describe them as full end-to-end validation.

Use the commands maintained in `README.md` and `.pre-commit-config.yaml` from the repository root. At minimum, the affected distribution must pass Ruff format, Ruff lint, strict mypy, and its deterministic pytest suite. Weave boundary changes must also pass import-linter. Inspect `git diff` before finishing.

## Delivery Rules

- Finish a coherent vertical slice; do not keep old and new APIs active together.
- Do not weaken assertions, skip tests, or hide errors to make a gate pass.
- Report the exact commands and results; identify checks that were not run or failed.
- Do not commit, push, publish, delete user data, or modify external services unless explicitly requested.
- Report changed behavior, key files, commands actually run, exact results, and remaining unverified acceptance.
