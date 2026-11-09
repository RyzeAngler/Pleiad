# Pleiad

Pleiad is the provider-neutral Agent runtime and development framework. The
official interactive application, Pleiad Weave, is maintained separately.

The source package lives directly in `src/`. Setuptools maps that directory to
the public `pleiad` import namespace; there is no nested `src/pleiad/` directory.

The package intentionally contains no TUI, HTTP application, CLI, or product
configuration. Install optional model extras only when the host application
needs the bundled provider adapters.

Source code and C4 architecture documentation are maintained in the
[project repository](https://github.com/RyzeAngler/Pleiad).

## Model clients

The public model objects select the request protocol by their class:
`OpenAIModelClient` accepts an OpenAI Chat Completions body, and
`AnthropicModelClient` accepts an Anthropic Messages body. The `provider`
argument selects the vendor SDK binding that sends the request and converts
the vendor response back to that protocol. Provider-specific request options
belong in `provider_options`; callers do not pass a separate protocol value or
a shared model client.

Install `pleiad[models]` for all bundled provider adapters. The protocol object
determines the public request and response shape; the provider module owns the
vendor SDK call and the conversion at that boundary:

| provider | `OpenAIModelClient` | `AnthropicModelClient` |
| --- | --- | --- |
| `openai` | OpenAI SDK | — |
| `anthropic` | — | Anthropic SDK |
| `deepseek` | OpenAI SDK | Anthropic SDK |
| `glm` | Zhipu `zai-sdk` | Anthropic SDK |
| `qwen` | DashScope SDK | Anthropic SDK |
| `kimi` | OpenAI SDK | Anthropic SDK |
| `minimax` | OpenAI SDK | Anthropic SDK |

For example, this remains an OpenAI request even though DeepSeek is the
provider:

```python
from pleiad.models.openai.client import OpenAIModelClient

client = OpenAIModelClient(
    provider="deepseek",
    model="deepseek-chat",
    api_key="...",
)
response = await client.complete(
    {"messages": [{"role": "user", "content": "hello"}]},
)
```

Use `AnthropicModelClient` with the same `provider` value when the caller's
body is an Anthropic Messages request. There is no public `protocol` argument
and no public common model client.

DeepSeek Chat Completions accepts JSON mode, but not OpenAI's `json_schema`
response format. Its provider binding maps a structured-output request to
`json_object` and includes the schema in system instructions before sending it.
This is the normal request mapping, not an error-triggered retry. The provider
does not enforce the schema; an Agent's final output is still validated by
Runtime, and invalid output fails the Run. Direct model-client callers must
validate the returned JSON against their schema themselves. See DeepSeek's
[Chat Completions contract](https://api-docs.deepseek.com/api/create-chat-completion/)
and [JSON Output guide](https://api-docs.deepseek.com/guides/json_mode/).

## Agent declarations

`Agent` is a frozen declaration of a model, prompt, capabilities, selected Skills,
output schema, and policy hints. Import it from `pleiad.agent.agent`.

Omitting `tools` selects the default references `read_file`, `write_file`, and
`command`. An explicit `tools` tuple is the complete selected set, including an
empty tuple for a text-only Agent. The host binds every selected Tool before a
Run starts and separately grants its permitted capabilities.

Selected Skills contribute catalog metadata to the initial prompt. Their instruction
files are not loaded into that prompt; the host must bind each selected Skill to the
SDK `read_file` Tool so the model can request its `SKILL.md` when needed.

## Sessions and Runtime

`Session` gives an interaction a stable host-selected identity. `SessionStore`
owns its ordered input inbox, messages, complete `ModelStep` records, writer
leases, Run checkpoints, receipts, tool intents, operations, and event journal.
Use `InMemorySessionStore` for deterministic in-process execution or
`SqliteSessionStore` for durable local state. Session input acceptance remains
available while a Run owns its writer.

An `InputEnvelope` has a stable `input_id`, kind, source, content, and optional
causation identity. Repeating the same accepted input returns its original
receipt. Reusing its identity with different content fails. `Runner.start`
accepts an explicit `invocation_id` and batch `input_ids`. Acknowledgement loss
can be resolved through `Session.run_for_invocation` using the same identity.
The `input_mode` selects a fixed batch or live intake at safe model boundaries.

```python
from pleiad.agent.agent import Agent
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.session import Session
from pleiad.skills.loader import SkillLoader
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime

runner = Runner(tools=ToolRegistry(), skills=SkillLoader(), tool_runtime=ToolRuntime())
session = Session("chat-1", InMemorySessionStore())
await session.enqueue(InputEnvelope(
    input_id="message-1", kind="user", source="host", content="hello",
))
handle = await runner.start(
    Agent(name="assistant", model=client, prompt="Answer the user.", tools=()),
    session=session,
    invocation_id="invocation-1",
    input_ids=("message-1",),
    input_mode="live",
    limits=RunLimits(max_model_steps=16),
)
async with handle:
    outcome = await handle.result()
```

`RunHandle` owns active execution. Its `events(after_sequence=...)` iterator
replays persisted `RunEvent` records, each containing a sequence and serialized
`event` object. Closing an event iterator detaches that observer. Closing an
active handle cancels its owned execution and awaits cleanup. Cancelling a
caller waiting on `result()` preserves that caller's cancellation and leaves
the independently owned execution available through its handle.

Terminal `RunResult.state` is `succeeded`, `failed`, or `cancelled`. Its committed
receipt includes output, usage, and a precise `reason`, such as `timeout` or
`limit_reached`. A safe pause produces `RunSuspension` with the original Run's
checkpoint and interruptions. `Runner.resume` validates the saved identity,
Agent fingerprint, authorization, and limits before claiming the paused writer.
Hosts supply explicit `ResumeDecision` objects for bound interruptions.
`Runner.cancel_paused` settles a saved suspension without executing its action.

`ModelStep`, imported from `pleiad.sessions.model_step`, represents one Assistant
response and its ordered Tool results. Multiple User inputs may precede that
response. Step identities use `step_id`, events use `step.started` and
`step.completed`, and `RunLimits.max_model_steps` counts model responses.
Messages, steps, SDK Runs, and product jobs have independent identities.
`Session.history()` returns protocol-complete steps for model requests while
`Session.read()` exposes bounded original interaction facts.

`ContextBuilder` selects request-local `ContextBlock` values and
`ContextRenderer` renders them alongside the selected inputs. Compaction changes
the request view while original messages remain in the Session. Saved resume
context retains the checkpoint's accepted reference snapshot. Runtime binds the
reserved `read_tool_result` Tool to the current Session writer. It lists bounded
result references or reads an original ToolMessage by `message_id` and character
offset. Completed background results use a separate `operation_id` selector and
`kind="operation"` reference listing. Both access paths remain within the bound
Session, including historical Runs. Oversized observations retain complete source
content in the store.

A host enables `ToolPolicy.background=True` for a Tool that implements
`supports_background` and `execute_background`. The Tool receives a
`BackgroundAdmission` carrying its stable `operation_id`. It first persists the
fully resolved request digest through `prepare(ToolSubmission)`, then
acknowledges concrete backend acceptance with a matching `ToolAcceptance`.
Runtime persists that receipt before returning one `started` ToolResult. Backend
completion is a separate idempotent `operation_result` input containing a bounded `ResultReference`
with the complete result digest and reading instructions. Its full `ToolResult`
remains in the persisted operation record. The Run's TaskGroup retains
concurrency and cleanup ownership through actual settlement.

`BaseTool.authorize` can request host authorization before external admission.
Runtime records its interruption with `phase="authorization"`. An approved
`ResumeDecision` starts the original action in the same Run and operation scope.
Tool-native interruptions use `phase="execution"` and resume through the Tool's
own implementation. Current schemas, binding identities, permissions, and
absolute deadlines are checked again on resume.

The command Tool passes its operation identity into `SandboxRequest`. OCI
acceptance records the actual created container, ownership labels, admission
token, and request digest before starting it. `OciSandbox.query(operation_id)`
recovers verified facts through another backend instance, and
`OciSandbox.stop(SandboxReceipt)` verifies the exact receipt before removal and
preserves exit facts already confirmed by its current inspection. Conflicting
creates retain the original action. Ordinary backend absence and unconfirmed
cancellation remain `unknown`. `ToolCancelledError` carries concrete
backend stop confirmation when cleanup establishes it. Recovery evidence can
include the original `ToolAcceptance` to commit a receipt recovered after an
acknowledgement-loss window. Recovered acceptance must match the original
prepared request digest.

`OperationRecord.state` describes SDK Tool settlement. Command Tool success
means it obtained actual process-exit facts. The command's `exit_code` and
`timed_out` fields describe those facts, while `SandboxObservation.state`
describes the backend process. A nonzero command exit therefore retains a
successful Tool observation, and delivery requirements evaluate the actual exit
facts against their declared expectations.

## Observability

Pleiad emits provider-neutral OpenTelemetry spans and metrics from the actual
Runtime, Context, Session, Model, Tool, MCP, Sandbox, and Graph boundaries. The
base package uses standard no-op providers unless the host explicitly supplies
providers; a process-global OpenTelemetry provider does not implicitly enable
Pleiad telemetry. Operation metrics count actual admission, active ownership,
settlement status, and lifecycle duration. Input-consumption metrics measure
accepted input to its committed safe-boundary acknowledgement, once per input.
Importing Pleiad does not read credentials, start exporters, or make network
connections. Content attributes such as prompts, completions, Tool
arguments, command text, and process output are denied by default.

Langfuse is an explicit, host-owned optional integration. Install the extra,
create it once during application startup, pass its `observability` object to
each `Runner`, and shut it down from the same owner:

```python
from pydantic import SecretStr

from pleiad.observability.config import LangfuseConfig
from pleiad.observability.integrations.langfuse import create_langfuse_observability

langfuse = create_langfuse_observability(
    LangfuseConfig(
        public_key=SecretStr("pk-lf-..."),
        secret_key=SecretStr("sk-lf-..."),
        base_url="https://cloud.langfuse.com",
        environment="production",
    )
)
runner = Runner(..., observability=langfuse.observability)
try:
    handle = await runner.start(agent, session=session, invocation_id="next-call",
                                input_ids=("next-input",), input_mode="batch", limits=RunLimits())
    async with handle:
        result = await handle.result()
finally:
    await langfuse.shutdown()
```

The integration owns its private OpenTelemetry provider and Langfuse client;
Run completion never flushes or closes shared resources. Export and shutdown
waits are bounded and their failures do not change Run results. Use an
explicit host Provider with `Observability(tracer_provider=..., meter_provider=...)`
when the host owns the exporter lifecycle. If bounded shutdown waiting expires,
the integration retains ownership of the single background shutdown operation;
a later call waits for that same operation instead of starting another one.

## Orchestration

Orchestration builds on the same Runner. Import definitions directly from
`pleiad.orchestration.handoff.handoff`, `pleiad.orchestration.subagents.subagent`,
`pleiad.orchestration.subagents.group`, and the defining Graph modules.

A Handoff changes the active Agent within the same Run and Session. A
`SubagentGroup` owns child SDK invocations scoped to one explicit parent Run.
The first invocation binds its `ToolContext.run_id` for the group's lifetime.
`as_tool`, `submit_subagent`, and `wait_subagents` provide model-facing delegation
controls. Independent product Workers are composed by the host using durable
product jobs and their own Sessions.

Graph `Node` bindings use `session_store`, `runner`, `agent`, and `limits`.
`GraphExecutor.execute` receives the invoking `ToolContext`, reserves disjoint
child allowances, and intersects parent grants, deadlines, and concurrency.
Agent interruptions return a node checkpoint. Restore `GraphState` and provide
`NodeResume` decisions to continue that same Run while retaining completed
siblings and its original reservation. The node input and graph revision must
match the saved contract. `GraphExecutor.cancel_paused` owns child cancellation
and exact budget settlement while preserving completed siblings.

An external Node outcome with unknown effects remains `NodeStatus.UNKNOWN` and
returns a failed Graph with `orchestration.graph.recovery_unknown`. Recovery
requires host reconciliation before replay. Graph state includes cumulative
child usage and `BudgetCheckpoint` reservations. `BudgetLedger` refunds only
confirmed unused allowances. Multiple orchestration components may share an
explicit ledger for the same parent.

## Data model boundaries

Pleiad uses Pydantic models for untrusted data, caller-authored configuration,
persistence records, and public serialization contracts. Configuration models
use strict validation and field constraints so one invalid input reports its
exact field without handwritten multi-field checks.

Frozen, slotted dataclasses remain the default for already-resolved internal
values, runtime work sets, live dependency bundles, and behavior-bearing values
that are not parsing or serialization boundaries. A public Python class is not
automatically a Pydantic model: the deciding factor is whether it accepts raw
data or owns a serialization contract.

## Sandbox

`Sandbox` executes one resolved request through the host-selected `OciSandbox`
backend. The policy requires a sha256 image digest, a non-root numeric UID:GID,
finite execution and cleanup budgets, resource limits, and an explicit
environment allowlist. Requests provide either `argv` or an explicitly allowed
`shell_command`; the model-visible `command` Tool does not choose images,
mounts, credentials, or OCI flags.

Before creating a container, the backend checks the selected engine with
`docker info` or `podman info` within the request's execution budget. An
unreachable engine raises `sandbox.runtime.unavailable` without entering
container cleanup. Once creation has been attempted, cleanup still resolves
the owned container even if the create command returned no ID. If execution
and cleanup both fail, `sandbox.cleanup.failed` reports both failures and
preserves the execution exception as its cause. Creation and exit-status
inspection failures include up to 2 KiB of runtime stderr for diagnosis;
request arguments, environment values, stdin, and stdout are not appended.

Each request creates one uniquely named, SDK-owned Docker or Podman container,
attaches to it, inspects its real exit status, and removes it by the confirmed
container ID. The fixed baseline is network-disabled, read-only rootfs,
capability-drop-all, no-new-privileges, non-root, no published ports, and no
host-process fallback. Output is collected per stream with a bounded retained
prefix while excess bytes are drained. Cancellation propagates after cleanup;
timeout returns bounded facts only after container removal.

`read_file` and `write_file` remain host-side scoped file Tools. `command`
mounts only the host-provided workspace, selected Skill roots, and scratch root
at their fixed virtual paths. Skill scripts use the container's own interpreter
and preinstalled dependencies; the SDK does not install packages or inherit
host credentials inside the container.

Host file Tools use directory descriptors and refuse symbolic links at every
path component on Linux and macOS. Reads reject non-regular files and retain at
most the configured byte limit plus one detection byte. Filesystem work runs
off the event loop, and cancellation waits for owned cleanup. Writes stage
content before rechecking the expected digest under a directory advisory lock;
SDK processes using that lock serialize their writes. Uncoordinated external
editors do not participate in that lock, so conditional replacement is not an
atomic compare-and-swap against arbitrary external writers.

## Graph validation and recovery

Graph and Node schemas reject external references before execution. JSON Pointer
array indices use RFC 6901 syntax: `0` or an ASCII decimal integer without a
leading zero; signed indices are rejected. Branch activation follows the
validated Node state, including output-schema failures.

A paused Agent Node retains its Run checkpoint, input, usage, and budget
reservation. Restoring that state keeps completed sibling outputs. Confirmed
cancellation has its own terminal Node status. Failed or unknown external effects
remain distinct from a safe resumable pause.

A recovery fingerprint includes graph input/output schemas, concurrency and
timeout settings, and node side-effect declarations. Change a graph definition
by creating a new revision. Executable handlers and external model behavior are
host bindings that must retain the same contract for a restored revision.

## MCP host authorization

`McpServer` accepts `tool_permissions`, a host-owned mapping from remote Tool
name to required capability grants. Discovery freezes that mapping and rejects
requirements naming absent Tools. McpTool exposes the requirements to the same
ToolRuntime execution/resume checks used by local Tools. Unspecified Tools keep
their existing binding behavior; remote annotations never grant permissions.
Normal request cancellation propagates through FastMCP and closes the owned
connection. Remote background Task lifecycle and durable input/elicitation
bridges remain outside this adapter; task-only Tools are explicitly rejected.
Hosts own MCP configuration, credentials, discovery, and connection policy.

## Reported usage and output budget

`ModelUsage` counts are optional. `None` means the provider did not report the
measurement, while `0` is a reported zero. Partial cumulative stream updates
preserve known fields. Totals are derived only when both components are known.
Missing measurements stay null in records and are omitted from token metrics.

`RunUsage.input_tokens`, `output_tokens`, `total_tokens`, and `model_calls`
measure the current invocation's own model work. `child_model_calls` and
`child_budget_output_tokens` hold conservative scoped-descendant charges.
`budget_output_tokens` includes both own and descendant charges. Unknown output
usage consumes the requested output allowance conservatively. The next model
request receives the remaining inclusive allowance, and the model-step guard
counts own calls plus charged child steps.

Before entering the provider, Runner commits a `ModelAttemptRecord` containing
the exact request digest, output allowance, deadline, and usage baseline. That
transaction counts one call and reserves its full authorized output allowance.
Cancellation, connection loss, and owner loss retain unknown measurements and
the committed charge. A complete provider result atomically records its
measurement and settles that attempt once. An expired authorization never
enters the provider or creates a charged attempt.

`BudgetLedger`, `BudgetState`, and `BudgetCheckpoint` are imported from
`pleiad.budgets`. Graph and SubagentGroup share that ledger with the parent
Runtime. Active child reservations also reduce parent model admission. A parent
can wait for an active child to settle before deciding that allowance is
exhausted. Provider-reported overage remains charged at its actual amount.

Host bindings expose the concrete `BaseTool.budget_ledger`. `FunctionTool`
accepts the same keyword when a bound function executes SDK graph or child
work. All scoped tools selected for a Run, including Handoff targets, must
share one ledger. ToolRuntime attaches typed `UsageCharge` records to Tool
results and interruptions independently of the model-visible JSON output.
Each charge retains its original reservation identity and cumulative counters.
Runtime merges that identity once, including partial resumes and repeated waits.

Subagent tools also expose their actual `ToolScope`. Runner enters this scope,
awaits or cancels accepted children, and closes it before committing the parent
receipt. Async submission remains charged when the parent finishes without a
wait call. A safe pause saves all scoped ownership snapshots, cumulative charge
identities, and the shared ledger. Restored scope bindings use the original
child checkpoints and outstanding reservations. Hosts invoking Group methods
directly own an explicit `async with group` scope.

A durable product Execution's cross-Run spending contract belongs to its host.
The host settles each committed SDK receipt idempotently and retains aggregate
product consumption across retries and resumed Runs.

Session persistence uses its current versioned schema and Run checkpoints use
version 1. Incompatible stored contracts fail validation before execution. A
host selects a fresh storage path when adopting this redesigned contract.
