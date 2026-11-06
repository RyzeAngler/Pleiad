import { describe, expect, it } from "vitest";
import {
  actionableExecution,
  graphPositions,
  latestAttempts,
  mergeEvents,
  streamDelta,
  updateLiveMessages,
} from "./projection";
import type {
  Execution,
  ExecutionEvent,
  Graph,
  GraphRun,
  NodeRun,
} from "./types";

const graph: Graph = {
  graph_id: "draft",
  revision: 2,
  objective: "",
  nodes: ["search", "angle", "write"].map((node_id) => ({
    node_id,
    objective: node_id,
    prompt: "",
    model_role: "text",
    tool_codes: [],
    skill_codes: [],
    input_schema: {},
    output_schema: {},
  })),
  edges: [
    { source_node_id: "search", target_node_id: "write" },
    { source_node_id: "angle", target_node_id: "write" },
  ],
};
const run = (
  revision: number,
  status: string,
  started_at: string,
): NodeRun => ({
  node_run_id: `${revision}-${started_at}`,
  graph_run_id: `graph-${revision}`,
  execution_id: "execution",
  graph_id: "draft",
  graph_revision: revision,
  node_id: "write",
  status,
  input: null,
  model_name: null,
  artifact_id: null,
  error: null,
  sdk_run_id: null,
  sdk_run_status: null,
  started_at,
  finished_at: null,
});
const graphRun: GraphRun = {
  graph_run_id: "graph-2",
  graph_id: "draft",
  graph_revision: 2,
  status: "running",
  started_at: "2026-11-03T10:00Z",
  state: null,
};
const event = (id: string, sequence: number, data = {}): ExecutionEvent => ({
  event_id: id,
  sequence,
  execution_id: "execution",
  event_type: "message.text.delta",
  timestamp: "2026-11-03T10:00:00Z",
  data,
  trace_id: null,
  span_id: null,
});

describe("Graph presentation", () => {
  it("places parallel prerequisites to the left of their dependent node", () => {
    const positions = graphPositions(graph);
    expect(positions.get("search")?.x).toBe(positions.get("angle")?.x);
    expect(positions.get("write")!.x).toBeGreaterThan(
      positions.get("search")!.x,
    );
    expect(positions.get("search")?.y).not.toBe(positions.get("angle")?.y);
  });
  it("keeps large resource cards apart and centers their dependent step", () => {
    const sizes = new Map([
      ["search", { width: 400, height: 480 }],
      ["angle", { width: 320, height: 200 }],
      ["write", { width: 320, height: 300 }],
    ]);
    const positions = graphPositions(graph, sizes);
    expect(positions.get("angle")!.y).toBeGreaterThan(
      positions.get("search")!.y + 480,
    );
    expect(positions.get("write")!.x).toBeGreaterThan(
      positions.get("search")!.x + 400,
    );
    const branchMidpoint =
      (positions.get("search")!.y + positions.get("angle")!.y + 200) / 2;
    expect(positions.get("write")!.y + 150).toBe(branchMidpoint);
  });
  it.each([
    {
      edges: [{ source_node_id: "missing", target_node_id: "write" }],
      message: "编排依赖引用了不存在的节点。",
    },
    {
      edges: [
        ...graph.edges,
        { source_node_id: "write", target_node_id: "search" },
      ],
      message: "编排依赖包含循环。",
    },
  ])(
    "reports invalid dependency graphs instead of overlapping their nodes: $message",
    ({ edges, message }) => {
      expect(() => graphPositions({ ...graph, edges })).toThrow(message);
    },
  );
  it("does not apply another revision’s success to the selected graph", () => {
    expect(
      latestAttempts(graphRun, [run(1, "succeeded", "2026-11-03T10:00Z")]).size,
    ).toBe(0);
    expect(
      latestAttempts(graphRun, [
        run(2, "failed", "2026-11-03T10:00Z"),
        run(2, "running", "2026-11-03T11:00Z"),
      ]).get("write")?.status,
    ).toBe("running");
  });
  it("excludes earlier executions and unassociated historical attempts of the same revision", () => {
    const earlier = {
      ...run(2, "succeeded", "2026-11-03T10:00Z"),
      graph_run_id: "earlier",
    };
    const historical = {
      ...run(2, "succeeded", "2026-11-03T10:01Z"),
      graph_run_id: null,
    };
    expect(latestAttempts(graphRun, [earlier, historical]).size).toBe(0);
    expect(latestAttempts(undefined, [earlier, historical]).size).toBe(0);
  });
});
describe("Execution stream", () => {
  it("stops showing an aborted message as streaming and clears live text at terminal state", () => {
    const aborted = {
      ...event("abort", 3, { message_id: "m" }),
      event_type: "message.aborted",
    };
    expect(
      updateLiveMessages({ m: "partial", other: "hello" }, aborted),
    ).toEqual({ other: "hello" });
    expect(
      updateLiveMessages(
        { m: "partial" },
        { ...event("cancel", 4), event_type: "execution.cancelled" },
      ),
    ).toEqual({});
  });
  it("keeps a pending approval actionable when a later message is queued", () => {
    const record = (
      status: Execution["status"],
      execution_id: string,
    ): Execution => ({
      execution_id,
      conversation_id: "chat",
      objective: "goal",
      status,
      graph_id: null,
      graph_revision: null,
      output: null,
      error: null,
      created_at: "",
      updated_at: "",
    });
    const waiting = record("waiting_approval", "needs-decision");
    expect(
      actionableExecution([waiting, record("queued", "later-message")]),
    ).toBe(waiting);
    const running = record("running", "running");
    expect(
      actionableExecution([running, record("queued", "later-message")]),
    ).toBe(running);
  });
  it("deduplicates replayed durable events without dropping later events", () => {
    expect(
      mergeEvents([event("a", 1)], [event("a", 1), event("b", 2)]).map(
        (item) => item.event_id,
      ),
    ).toEqual(["a", "b"]);
  });
  it("keeps node-agent text out of the supervisor conversation", () => {
    expect(
      streamDelta(
        event("a", 0, {
          agent_name: "write-node",
          message_id: "m",
          delta: "node text",
        }),
      ),
    ).toBeNull();
    expect(
      streamDelta(
        event("b", 0, {
          agent_name: "weave-supervisor",
          message_id: "m",
          delta: "hello",
        }),
      ),
    ).toEqual({ id: "m", delta: "hello" });
  });
});
