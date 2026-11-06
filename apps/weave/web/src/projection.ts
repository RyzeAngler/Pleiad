import type {
  Execution,
  ExecutionEvent,
  Graph,
  GraphRun,
  NodeRun,
} from "./types";

export const labels: Record<string, string> = {
  queued: "排队中",
  pending: "未开始",
  ready: "就绪",
  running: "执行中",
  waiting_approval: "待审批",
  resume_queued: "等待恢复",
  succeeded: "已完成",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  skipped: "已跳过",
  unknown: "结果未确认",
  interrupted: "等待处理",
};
export const statusLabel = (status: string) => labels[status] ?? status;
export const isActive = (execution: Execution) =>
  !["succeeded", "failed", "cancelled"].includes(execution.status);
export function actionableExecution(
  executions: Execution[],
): Execution | undefined {
  for (const status of [
    "waiting_approval",
    "running",
    "resume_queued",
    "queued",
  ]) {
    const match = executions.find((execution) => execution.status === status);
    if (match) return match;
  }
  return undefined;
}
export const graphKey = (graph: Graph) => `${graph.graph_id}:${graph.revision}`;

export function latestAttempts(
  graphRun: GraphRun | undefined,
  runs: NodeRun[],
): Map<string, NodeRun> {
  if (!graphRun) return new Map();
  const matching = runs.filter(
    (run) =>
      run.graph_run_id === graphRun.graph_run_id &&
      run.graph_id === graphRun.graph_id &&
      run.graph_revision === graphRun.graph_revision,
  );
  return new Map(
    matching
      .sort((a, b) => a.started_at.localeCompare(b.started_at))
      .map((run) => [run.node_id, run]),
  );
}

export function graphNodeStatus(
  nodeId: string,
  graphRun: GraphRun | undefined,
  attempt: NodeRun | undefined,
): string {
  const scheduled = graphRun?.state?.node_states.find(
    (node) => node.node_id === nodeId,
  );
  if (scheduled) return scheduled.status;
  if (attempt) return attempt.status;
  return !graphRun || graphRun.status === "running" ? "pending" : "unknown";
}

export function graphPositions(
  graph: Graph,
  sizes: ReadonlyMap<string, { width: number; height: number }> = new Map(),
): Map<string, { x: number; y: number }> {
  const ranks = new Map(graph.nodes.map((node) => [node.node_id, 0]));
  const incoming = new Map(graph.nodes.map((node) => [node.node_id, 0]));
  const children = new Map(
    graph.nodes.map((node) => [node.node_id, [] as string[]]),
  );
  for (const edge of graph.edges) {
    if (!ranks.has(edge.source_node_id) || !ranks.has(edge.target_node_id))
      throw new Error("编排依赖引用了不存在的节点。");
    incoming.set(edge.target_node_id, incoming.get(edge.target_node_id)! + 1);
    children.get(edge.source_node_id)!.push(edge.target_node_id);
  }
  const queue = graph.nodes
    .filter((node) => incoming.get(node.node_id) === 0)
    .map((node) => node.node_id);
  for (let index = 0; index < queue.length; index++) {
    const id = queue[index];
    for (const child of children.get(id)!) {
      ranks.set(child, Math.max(ranks.get(child)!, ranks.get(id)! + 1));
      incoming.set(child, incoming.get(child)! - 1);
      if (incoming.get(child) === 0) queue.push(child);
    }
  }
  if (queue.length !== graph.nodes.length)
    throw new Error("编排依赖包含循环。");
  const columns = new Map<number, string[]>();
  for (const node of graph.nodes) {
    const rank = ranks.get(node.node_id)!;
    columns.set(rank, [...(columns.get(rank) ?? []), node.node_id]);
  }
  const height = (id: string) => sizes.get(id)?.height ?? 280;
  const columnHeight = (ids: string[]) =>
    ids.reduce((total, id) => total + height(id), 0) + (ids.length - 1) * 64;
  const tallest = Math.max(0, ...[...columns.values()].map(columnHeight));
  const positions = new Map<string, { x: number; y: number }>();
  let x = 0;
  for (const [, ids] of [...columns].sort(([a], [b]) => a - b)) {
    let y = (tallest - columnHeight(ids)) / 2;
    for (const id of ids) {
      positions.set(id, { x, y });
      y += height(id) + 64;
    }
    x += Math.max(...ids.map((id) => sizes.get(id)?.width ?? 360)) + 90;
  }
  return positions;
}

export function mergeEvents(
  previous: ExecutionEvent[],
  incoming: ExecutionEvent[],
): ExecutionEvent[] {
  const events = new Map(previous.map((event) => [event.event_id, event]));
  incoming.forEach((event) => events.set(event.event_id, event));
  return [...events.values()]
    .sort(
      (a, b) =>
        a.timestamp.localeCompare(b.timestamp) || a.sequence - b.sequence,
    )
    .slice(-500);
}

export function streamDelta(
  event: ExecutionEvent,
): { id: string; delta: string } | null {
  if (
    event.event_type !== "message.text.delta" ||
    event.data.agent_name !== "weave-supervisor"
  )
    return null;
  const { message_id: id, delta } = event.data;
  return typeof id === "string" && typeof delta === "string"
    ? { id, delta }
    : null;
}

export function updateLiveMessages(
  previous: Record<string, string>,
  event: ExecutionEvent,
): Record<string, string> {
  if (
    ["execution.completed", "execution.failed", "execution.cancelled"].includes(
      event.event_type,
    )
  )
    return {};
  if (
    event.event_type === "message.aborted" &&
    typeof event.data.message_id === "string"
  ) {
    return Object.fromEntries(
      Object.entries(previous).filter(([id]) => id !== event.data.message_id),
    );
  }
  const fragment = streamDelta(event);
  return fragment
    ? {
        ...previous,
        [fragment.id]: (previous[fragment.id] ?? "") + fragment.delta,
      }
    : previous;
}

export const eventLabel = (event: ExecutionEvent): string => {
  const names: Record<string, string> = {
    "execution.queued": "任务已提交",
    "execution.started": "开始执行",
    "graph.revision.accepted": "编排已保存",
    "graph.started": "编排开始执行",
    "graph.completed": "编排执行结束",
    "node.completed": "节点执行结束",
    "model.started": "模型开始处理",
    "model.completed": "模型响应完成",
    "tool.started": "调用工具",
    "tool.completed": "工具调用结束",
    "tool.interrupted": "工具调用待审批",
    "execution.waiting_approval": "等待审批",
    "execution.completed": "任务完成",
    "execution.failed": "任务失败",
    "execution.cancelled": "任务已取消",
  };
  const detail = event.data.tool_code ?? event.data.node_id ?? event.data.model;
  return `${names[event.event_type] ?? event.event_type}${typeof detail === "string" ? ` · ${detail}` : ""}`;
};
