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

Every Agent receives the default Tool references `read_file`, `write_file`, and
`command`. Explicit Tool references are appended after those defaults and duplicate
codes are rejected. The host must bind every declared Tool before a Run starts.

Selected Skills contribute catalog metadata to the initial prompt. Their instruction
files are not loaded into that prompt; the host must bind each selected Skill to the
SDK `read_file` Tool so the model can request its `SKILL.md` when needed.

## Context and Runtime

`ContextBuilder` selects request-local `ContextBlock` references and
`ContextRenderer` renders them beside the current User Message. The original
message and dynamic references remain separate from `ShortMemory`; context
budgets and compaction are evaluated for each model request at complete Turn
boundaries.

Dynamic references are rendered for every request, including Tool continuations
and checkpoint resumes. The request view preserves message order while the
original User messages remain unchanged in ShortMemory.

Runtime also binds `read_tool_result` to the invocation's ShortMemory. It is a
reserved SDK Tool; hosts do not register it or add a capability grant for it.
Omit `message_id` to list bounded, newest-first result references, using
`after_message_id` to continue the list. Supply `message_id`, `start_char`, and
`max_chars` to read a character page (4,000 by default, at most 8,000). References
include the original UTF-8 SHA-256 digest and character count. They cannot select
another memory ID or a filesystem path.

Tool observations retain their complete original content in ShortMemory. The
model request replaces observations above 64,000 characters with executable
read references, including the most recent result. History compaction can also
replace older retained observations. Even after an entire Turn leaves the
request view, its results remain discoverable through `read_tool_result` while
the host retains that memory. Weave's existing SQLite memory binding supplies
durability; no separate result database or model-facing host path is required.

`Runner` owns the ShortMemory-backed Model → Tool → Model loop. Bind concrete
model clients on the `Agent`, then supply a `ShortMemory`, `RunConfig`, and the
host's `ToolRegistry`, `SkillLoader`, and `ToolRuntime`:

```python
from pleiad.agent.agent import Agent
from pleiad.memory.short_memory import ShortMemory
from pleiad.runtime.config import RunConfig
from pleiad.runtime.execution.runner import Runner

result = await runner.run(
    Agent(name="assistant", model=client, prompt="Answer the user."),
    "hello",
    memory=ShortMemory.in_memory("conversation-1"),
    config=RunConfig(),
)
```

An interrupted run returns a `RunState` checkpoint in `RunResult`; resume it
with `Runner.resume` and explicit interruption resolutions. `RunStream` uses
the same execution path for ordered lifecycle events and early-close
cancellation. SDK notifications inherit the public `Event` envelope from
`pleiad.events.contracts`; product applications can define typed Event
subclasses without registering them with Pleiad. Stream buffering is bounded
and applies backpressure, while terminal events remain associated with their
`run_id`.

Messages are concrete role-specific contracts imported from
`pleiad.messages.message`: `SystemMessage`, `UserMessage`, `AssistantMessage`,
and `ToolMessage`. An Assistant owns its model-declared `tool_calls`; the
corresponding execution output is a later Tool message. Completed message
snapshots and live text or Tool-argument deltas are published through the
typed events in `pleiad.events.message`.

Use the stream as an async context manager when the consumer may stop before
the Run completes. Leaving the scope calls `aclose()`, cancels the owned Run,
and waits for its cleanup. A bare `break` from `events()` is not the explicit
resource boundary:

```python
from pleiad.events.message import MessageTextDeltaEvent

async with runner.run_streamed(
    agent,
    "hello",
    memory=memory,
    config=config,
) as stream:
    async for event in stream.events():
        if isinstance(event, MessageTextDeltaEvent):
            render_delta(event.message_id, event.delta)
    result = await stream.result()
```

An optional host `event_sink` is awaited and isolated as best-effort
notification. The RunStream delivery path remains reliable and propagates
execution failures through `result()`; neither path is a persistence or
exactly-once transaction interface.

## Observability

Pleiad emits provider-neutral OpenTelemetry spans and metrics from the actual
Runtime, Context, Memory, Model, Tool, MCP, Sandbox, and Graph boundaries. The
base package uses standard no-op providers unless the host explicitly supplies
providers; a process-global OpenTelemetry provider does not implicitly enable
Pleiad telemetry. Importing Pleiad does not read credentials, start exporters,
or make network connections. Content attributes such as prompts, completions, Tool
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
    result = await runner.run(...)
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

Orchestration is an SDK-only layer built on the Runtime. Import concrete
objects from `pleiad.orchestration.supervisor`, `pleiad.orchestration.handoff`,
and `pleiad.orchestration.graph`; it does not create a product task or
execution record. `as_tool` is a module function for synchronous Worker
delegation, `submit_worker` and `wait_workers` are explicit asynchronous
controls inside an opened `Supervisor` scope. Concurrent Workers reserve from
one parent budget ledger and return confirmed unused budget at completion.
Worker grants are the intersection of parent, Worker, host, and Agent limits.
Handoff switches the active Agent inside the same Run and
ShortMemory. Graph uses `Graph`, `Node`, and `Edge`; Agent nodes call the
existing Runner while normal async functions run directly.

Pass the invoking `ToolContext` to `GraphExecutor.execute` for nested Agent
Nodes. The executor intersects grants, deadlines, concurrency, and remaining
allowances with the parent; sibling Nodes reserve disjoint child allowances
and wait for confirmed refunds. Repeated Graph calls reuse the parent Run's
pool. GraphExecutor and Supervisor can share an explicit `BudgetLedger` when
the host combines both forms of delegation.

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

An external NodeExecutor may return `PAUSED`. When its downstream Nodes cannot
advance and no tasks remain active, GraphExecutor returns `PAUSED` with those
Nodes still pending. Restoring that state retains completed sibling outputs.
Agent Node interruption checkpoints require host orchestration and are not
implicitly converted into a recoverable Agent Node result.

Agent Node `LIMIT_REACHED` and `TIMED_OUT` outcomes are confirmed failed Nodes
with their actual SDK Run identity, status, and usage. `GraphResult.child_usage`
and `GraphState.child_usage` report child measurements separately from the
parent's own usage. Unknown measurements remain null. GraphState requires this
field; older serialized states without it are rejected.

A recovery fingerprint includes graph input/output schemas, concurrency and
timeout settings, and node side-effect declarations. Change a graph definition
by creating a new revision instead of restoring state against a changed contract.
Executable handler code and external model behavior are host bindings; the
host must keep them stable when restoring the same graph revision.

## MCP host authorization

`McpServer` accepts `tool_permissions`, a host-owned mapping from remote Tool
name to required capability grants. Discovery freezes that mapping and rejects
requirements naming absent Tools. McpTool exposes the requirements to the same
ToolRuntime execution/resume checks used by local Tools. Unspecified Tools keep
their existing binding behavior; remote annotations never grant permissions.
Normal request cancellation propagates through FastMCP and closes the owned
connection. Remote background Task lifecycle and durable input/elicitation
bridges remain outside this adapter; task-only Tools are explicitly rejected.
Weave does not yet configure MCP servers through its workspace catalog.

## Reported usage and output budget

`ModelUsage` counts are optional: `None` means the provider did not report a
count, and `0` is a real reported zero. OpenAI streams request
`stream_options={"include_usage": true}`. Partial cumulative usage updates
preserve known fields; totals are derived only when both components are known.
Anthropic message-start output usage does not establish the final output count.
Missing counts are omitted from token metrics and span attributes;
`model.completed` events serialize them as null.

`RunUsage` propagates unknown measured totals instead of replacing them with
zero. `budget_output_tokens` is a separate cumulative budget charge, not a
measurement. A call with reported output usage charges that count; if output
usage is absent it charges the requested output limit conservatively. Each
subsequent request uses only the remaining `RunConfig.max_output_tokens`
allowance. Exhausted allowance prevents a further model call and returns
`LIMIT_REACHED`; a final answer already received can still succeed.

Worker and Graph budget settlement refunds only confirmed unused allowance.
Unknown child outcomes keep their full reservations charged. Tool completion
and checkpoint resume preserve those charges. The parent limits supplied to
orchestration measure the parent's own remaining budget. The shared ledger
accounts for child reservations and charges separately;
later decreases in the parent's limits debit that shared pool exactly once.
In-flight reservations remain held even when newly observed parent consumption
exhausts admission capacity. A Handoff also reschedules the active timeout when
the target Agent imposes an earlier deadline.

These limits apply to SDK invocations and child admission; they do not define
an aggregate spending cap for a durable product Execution. A host restoring a
parent after child delegation must persist `GraphExecutor.budget_state` along
with its Run checkpoint and call `restore_budget` before continuing. BudgetState
is settled allowance, not measured usage; active reservations cannot be saved
as settled state, and restoring a different live pool is rejected.
Run checkpoints now use version 2; version 1 checkpoints are rejected because
they do not establish this accounting contract. No old-state migration or
compatibility alias is supplied.
