# Maintained SDK task evaluations

These three small, maintained fixtures exercise file-backed task reasoning and
request-local business context through the public SDK Runner. They are sample
tasks, not production traces or a full Weave end-to-end benchmark. The oracle
stays outside each isolated temporary workspace. Answers are scored by exact,
canonical JSON equality; no second model judges or replaces a failed response.

Validate the suite offline:

```bash
uv run --project apps/weave --frozen python -m pleiad_weave.evaluation validate evals/suite.json
```

Run actual model requests only with the explicit flag. This uses the normal
provider settings and credentials, and requires the configured pinned Sandbox
image reference. The host grants only `file.read`; no Docker/Podman process is
started, and attempted writes or commands fail the task even if its answer is
otherwise correct.

```bash
uv run --project apps/weave --frozen python -m pleiad_weave.evaluation run evals/suite.json /tmp/pleiad-current.json --real-provider
uv run --project apps/weave --frozen python -m pleiad_weave.evaluation compare /tmp/pleiad-baseline.json /tmp/pleiad-current.json
uv run --project apps/weave --frozen python -m pleiad_weave.evaluation run evals/suite.json /tmp/pleiad-retry.json --real-provider --failures /tmp/pleiad-current.json
```

Reports preserve actual output, Run identity, elapsed time, attempted Tools,
reported usage (including unknown values), and complete task fingerprints.
Comparisons reject changed fixtures/oracles, missing cases, and harness-test
reports. A failed-case subset is not a full-suite baseline. No baseline scores
are shipped until a real run is performed and reviewed.

Programmatic hosts can pass an existing `Observability` object, including a
Langfuse binding, to `evaluate_case`; the harness never initializes an exporter
or reads telemetry credentials implicitly. Default tests validate the harness
with fake model transports and do not establish model quality scores.
