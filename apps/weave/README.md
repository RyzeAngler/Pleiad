# Pleiad Weave

Default workspace MCP presets and reviewed Skills are described in the
[capability setup guide](../../docs/weave-capabilities.md). Remote connections
use the public Pleiad MCP API and remain disabled until configured. Authorize
OAuth connections with `pleiad mcp-login <server-code>` in a terminal.

## Web workspace

The same `pleiad serve` process now serves the Web UI at `http://127.0.0.1:8000/`
and its API. From the repository root, build the frontend once before starting
from a source checkout:

```bash
npm --prefix apps/weave/web ci
npm --prefix apps/weave/web run build
uv run --project apps/weave --frozen pleiad serve
```

The UI uses real spaces, conversation history, graph definitions, node attempts,
artifacts, SSE progress, and persisted approval requests. Space rules and
resource preferences are saved on the server and snapshotted per Execution.
Preferences are guidance, not an authorization boundary. Models and capability
installation continue to use the existing host/workspace configuration.

Frontend sources live in `web/`; built files are packaged under `src/web/assets/`.
Node.js 22.12+ is required for source builds, but not for installed distributions.
See the [startup and operation guide](../../docs/weave-web.md) for configuration,
first-use steps, development, and troubleshooting.

Weave is the product application built on the current public Pleiad SDK. Its flat `src/` directory installs as `pleiad_weave`; `packages/pleiad/src/` installs independently as `pleiad`.

`ExecutionService` is the shared CLI, HTTP, and TUI boundary. `WeaveAgent` declares an SDK Agent and consumes Runner streams. Graph proposals bind to SDK Graph/Node objects; product wrappers only persist revision, attempt, and artifact records. Model roles bind directly to OpenAIModelClient or AnthropicModelClient. Default file/command Tools, ShortMemory, and OCI execution come from the SDK.

## Configuration

Install the provider extra used by the deployment, for example:

```bash
uv sync --project apps/weave --extra deepseek
```

Set model credentials and `PLEIAD_SANDBOX_IMAGE` in the host `.env`. The image must use a real `name@sha256:<64 hex digits>` digest already present in Docker or Podman. SDK policy rejects mutable tags. There is no host process fallback or automatic pull.

The bundled image definition and Textual styles ship as package resources; the configured workspace, capability files, user data, and credentials do not. The [Weave architecture](../../docs/weave-architecture.md) explains the core ideas, runtime boundaries, and component relationships through C4 views.

`create_application(settings, observability=...)` accepts a host-owned SDK Observability object. Exporter creation and shutdown belong to that caller. Standard CLI startup leaves telemetry disabled. The TUI Trace tab shows event order and correlation IDs; it does not read exporter spans or a local TraceStore.

## Product lifecycle

Each workspace has one execution owner, enforced by an advisory process lock.
A second service cannot perform recovery or execute work while that owner is
active. Executions are durable and independently identified. A cancelled waiter
does not cancel the owned SDK task; explicit cancellation waits for SDK cleanup
and the terminal journal write. Requests in one conversation serialize through
SDK ShortMemory. Restart recovery explicitly fails orphaned RUNNING jobs before
rescheduling unclaimed queued jobs, preserving completed Turns and avoiding
side-effect replay.

### Explicit Tool approval

Default Tool behavior is unchanged. Hosts opt specific Tool codes into approval
with `Settings(approval_tool_codes=frozenset({"write_file", "command"}))` or
`PLEIAD_APPROVAL_TOOL_CODES='["write_file","command"]'`. A selected invocation
first returns a real SDK ToolInterruption without doing its side effect.
Weave commits its RunState, settled Graph child budget, approval policy,
WAITING_APPROVAL state, and journal event in one transaction. Its active Turn is retained. Approval or rejection
continues the same Execution, Run, and Session through Runner.resume_streamed;
clients cannot supply a replacement ToolResult or checkpoint.

WAITING_APPROVAL survives restart. A committed RESUME_QUEUED decision may be
rescheduled until execution is claimed as RUNNING. A restart after that claim
fails the job instead of replaying a potentially completed external action.
The original absolute Run deadline includes approval waiting time. Decisions
are idempotent; a contradictory or obsolete decision reports a conflict.
Later work in the same conversation remains queued until the paused owner
finishes or is cancelled. Changes to the saved approval policy fail explicitly.
The serialized resume state requires `graph_budget` (nullable before any Graph
delegation); older checkpoints without it fail explicitly instead of refilling
child allowances.

When `weave.execute_graph` requires approval, the pending action includes the
resolved graph revision and definition digest. Approval executes that revision
even if a newer one is published while waiting. A missing or changed approved
revision fails before any Node starts. `execute_graph` also accepts an explicit
`graph_revision`; otherwise it uses the Execution's selected revision or the
current stored revision.

```bash
pleiad approval list --workspace /absolute/workspace
pleiad approval approve EXECUTION_ID INTERRUPTION_ID
pleiad approval reject EXECUTION_ID INTERRUPTION_ID
```

Listing approvals reads a repository snapshot without starting models, Sandbox,
or recovery. CLI decisions are offline owner operations; while a service owns
the workspace, use that service's HTTP API or TUI. The TUI opens pending
approvals on restart and exposes `/approvals`, with explicit approve/reject
buttons showing the saved Tool, arguments, resolved action, reason, and deadline.
Open approval screens refresh on Execution events; a cancelled, expired, or
externally decided action is removed and an empty screen closes.

HTTP endpoints are `GET /v1/executions/approvals`,
`GET /v1/executions/{execution_id}/approvals`, and
`POST /v1/executions/{execution_id}/approvals/{interruption_id}` with
`{"decision":"approve"}` or `{"decision":"reject"}`. This remains a local,
single-owner deployment; these endpoints do not introduce multi-user identity
or authentication. Graph Agent nodes needing approval are rejected before
execution until their independent checkpoint recovery is implemented.

### Local fact memory

Memory facts retain conversation/user scope, revision, active state, and
original provenance. Old records with unknown sources keep null source/time.
Local management revisions are stamped `source=local_user` and UTC time by the
host. JSONL revision writes use a shared process lock and expected-revision
checks; stale edits cannot overwrite another edit. No history is deleted.

```bash
pleiad memory --workspace /absolute/workspace --conversation-id CHAT list
pleiad memory --workspace /absolute/workspace --conversation-id CHAT show FACT_ID --history
pleiad memory --workspace /absolute/workspace --conversation-id CHAT update FACT_ID --content TEXT --expected-revision 1
pleiad memory --workspace /absolute/workspace --conversation-id CHAT deactivate FACT_ID --expected-revision 2
```

These commands do not initialize a model or Sandbox. `list --all` includes
deactivated facts. User scope uses `--scope user` without a conversation ID and
requires `PLEIAD_MEMORY_CROSS_CONVERSATION=true`; identity is always the
deployment's `PLEIAD_MEMORY_USER_ID`, never a supplied arbitrary user ID.
Workspaces isolate their own `runtime/memory` directories. This is not a global
cross-project user store. No HTTP or model-authored memory mutation is exposed.

### Task evaluation

The repository's [evaluation suite](../../evals/README.md) provides maintained
read-only SDK task fixtures, exact JSON scoring, result reports, baseline
comparison, and failed-case reruns. Validation is offline; real model calls
require `--real-provider`. Harness tests are labeled separately and cannot
establish a quality baseline. Actual provider, OCI, and full product acceptance
remain distinct checks.

Legacy JSONL conversation journals remain untouched; they are not read or migrated into the SDK ShortMemory format.

Each Execution, capability search, graph submission, and graph execution validates the current workspace catalog. New capabilities require no process restart. Existing Runs retain their selected refs. Invalid catalogs fail instead of using an older version.

## Verification

From the repository root:

```bash
uv lock --project apps/weave --check
uv run --directory apps/weave --frozen ruff format --check .
uv run --directory apps/weave --frozen ruff check .
uv run --directory apps/weave --frozen mypy
uv run --directory apps/weave --frozen pytest -q -m 'not packaging and not oci and not provider_e2e'
uv run --project apps/weave --frozen lint-imports --config .importlinter
uv run --directory apps/weave --frozen pytest -q -m packaging
```

Deterministic tests fake only model transports and OCI processes. HTTP tests use ASGITransport; TUI tests include Textual's headless runner. These checks do not establish real-provider, OCI-daemon, remote exporter, or terminal visual acceptance.

## Agent composition

`agent/graph.py` builds SDK Graph objects from `GraphSpec` and `NodeSpec`.
The same module owns `GraphRunner`; `agent/nodes.py` owns `NodeExecutor`.
These wrappers persist product records around SDK execution.
`capabilities/tools.py` installs workspace `ScriptTool` bindings.

Graph Agent Nodes inherit the invoking Run's grants, remaining child allowance,
concurrency, and absolute deadline through the SDK. Repeated Graph invocations
and approval restarts retain the same child budget pool. Child usage is reported
separately from the parent Run's own usage; this is not an aggregate Execution
spending limit. Node limits and timeouts persist as confirmed failed attempts
with their SDK Run identity and usage.

After an owner is lost, startup also settles active Graph records as failed and
unfinished Node attempts as unknown, retaining confirmed outputs, artifacts,
and Run identities. It does not replay their side effects. Normal CLI execution
validates business JSON before constructing a provider and closes its service
even when workspace ownership prevents startup.

Prompts describe the task in plain language. Tool descriptions explain graph
parameters and mapping rules; the SDK supplies structured output constraints.
Node prompts include the objective and any additional instructions without
copying the output schema into the prose.

Full answers are stored in Execution output and delivered live. The journal records `answer.completed` with `output_location=execution`, keeping lifecycle events below the 16 KiB record limit. Replay clients obtain the answer through the Execution summary. Skill roots must remain inside `skills/` and cannot be symbolic links. Workspace JSON schemas must use local references.

SDK model completion events expose missing token counts as null. A direct final
answer can succeed without provider usage. If missing output usage consumes
the conservative Run allowance and another model call is needed, Weave records
the Execution as failed with `weave.run_limit_reached`; it does not make a
replacement call or report a fabricated answer.
