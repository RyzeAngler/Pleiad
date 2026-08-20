# Pleiad Architecture Overview

Pleiad is a runtime for turning a user objective into a bounded, observable,
and recoverable execution graph. The public architecture is intentionally
small: it describes the stable responsibilities and boundaries required to
understand the project without exposing internal design records.

## System Architecture

```mermaid
flowchart LR
    User[User]
    HTTP[HTTP API]
    TUI[Terminal UI]
    App[Application Service]
    Runtime[Runtime]

    subgraph Planning[Planning]
        Main[Main Agent]
        Context[Context Builder]
        Memory[Main-Agent Memory]
        Zoo[Tool and Skill Zoo]
        Graph[Task Graph Store]
    end

    subgraph Execution[Execution]
        Executor[Graph Executor]
        Node[Node Agent]
        Loop[ReAct Loop]
        Tools[Tool Runtime]
        Skills[Skill Loader]
    end

    subgraph Platform[Platform Services]
        Models[Model Registry]
        Events[Runtime Events]
        Trace[Observability and Trace]
        Storage[Execution and Artifact Storage]
    end

    User --> HTTP
    User --> TUI
    HTTP --> App
    TUI --> App
    App --> Runtime

    Runtime --> Main
    Runtime --> Executor
    Runtime --> Events
    Runtime --> Trace
    Runtime --> Storage

    Main --> Context
    Main --> Memory
    Main --> Zoo
    Main --> Graph
    Main --> Models
    Graph --> Executor

    Executor --> Node
    Node --> Loop
    Loop --> Models
    Loop --> Tools
    Loop --> Skills
    Tools --> Zoo

    Events --> TUI
    Trace --> TUI
    Memory --> Storage
    Graph --> Storage
```

## Request Sequence

```mermaid
sequenceDiagram
    participant U as User
    participant E as API or TUI
    participant A as Application Service
    participant R as Runtime
    participant M as Main Agent
    participant C as Context and Memory
    participant Z as Capability Zoo
    participant G as Task Graph
    participant N as Node Agent
    participant T as Tool Runtime
    participant O as Observability

    U->>E: Submit objective
    E->>A: Create execution request
    A->>R: Start execution
    R->>O: Open execution trace
    R->>C: Restore main-agent context
    C-->>R: History and bounded context
    R->>M: Run planning turn
    M->>Z: Search capability metadata
    Z-->>M: Ranked descriptors
    M->>G: Submit validated graph
    G-->>M: Graph revision
    M->>G: Execute graph
    G->>N: Start selected node
    N->>T: Invoke selected tool when needed
    T-->>N: Tool result
    N-->>G: Structured node result
    G-->>M: Execution result
    M-->>R: Final visible response
    R->>C: Persist main-agent turn
    R->>O: Close trace
    R-->>A: Execution record
    A-->>E: Result and observable updates
    E-->>U: Final response
```

## Core Components

| Component | Responsibility | Boundary |
| --- | --- | --- |
| API / TUI | Accept objectives and present execution state | Does not implement planning or execution rules |
| Application Service | Own the application request and execution lifecycle | Coordinates external entrypoints with Runtime |
| Runtime | Drive planning, graph execution, state transitions, and recovery | Does not contain provider-specific model logic |
| Main Agent | Interpret the objective, search capabilities, build a graph, and coordinate execution | Owns planning decisions; does not directly implement external side effects |
| Context Builder | Assemble bounded request-local context | Does not persist prompt text or own memory policy |
| Main-Agent Memory | Persist and restore the main Agent's conversation and personal memory | Node Agents do not use persistent memory |
| Tool and Skill Zoo | Index capability metadata and resolve selected implementations | Exposes descriptors for discovery; loading remains lazy |
| Task Graph Store | Validate, version, and persist graph definitions | Stores graph structure, not Agent message history |
| Graph Executor | Schedule graph nodes and propagate structured results | Creates bounded Node execution scopes |
| Node Agent | Execute one selected node objective through a ReAct loop | Receives only its selected Tools and Skills |
| Tool Runtime | Validate and execute side-effecting capabilities | Centralizes execution policy, errors, and observability |
| Model Registry | Resolve configured model roles and providers | Keeps provider details outside orchestration code |
| Observability | Record traces, spans, metrics, and execution diagnostics | Trace persistence is independent from Runtime Events |
| Storage | Persist executions, graphs, artifacts, traces, and memory | Storage formats are implementation details behind ports |

## Logic Principles

1. The Main Agent plans; Node Agents execute.
2. Capability discovery searches only `name`, `description`, and `code` metadata.
3. Tool and Skill search uses an in-memory BM25 index. Selected capability codes
   remain stable throughout a Node Agent's ReAct loop.
4. Tools are the only execution capability exposed to an Agent. Skills provide
   static instructions and do not perform side effects.
5. The Main Agent owns persistent conversation and personal memory. A Node Agent
   keeps only run-local messages and does not write to persistent memory.
6. The raw user query is durable input. A request-specific composed prompt is
   temporary and is not written to memory.
7. Runtime Events support live progress delivery. Trace records provide durable
   execution observability; neither is the storage mechanism for the other.
8. Graph definitions, execution state, Agent messages, and artifacts have
   separate lifecycles and storage responsibilities.
9. Dynamic capability changes become effective at a safe request boundary. An
   invalid catalog does not replace the last valid catalog.
10. External entrypoints depend on application contracts rather than internal
    Agent, Graph, Tool, or storage implementations.

## Public Scope

This document covers stable project boundaries and user-visible execution
behavior. Detailed data models, persistence layouts, context policies, recovery
algorithms, and design history are maintained separately and are not part of
the public documentation set.

For public extension guidance, see [Custom Tool Development](custom-tools.md).
