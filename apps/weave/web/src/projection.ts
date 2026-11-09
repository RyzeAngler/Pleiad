import type { Graph, Route } from "./types";
export const statusLabel = (value: string) =>
  ({
    queued: "排队中",
    pending: "等待中",
    running: "进行中",
    waiting_input: "待补充",
    waiting_approval: "待审批",
    waiting_children: "等待子工作",
    paused: "已暂停",
    blocked: "待核实",
    cancelling: "正在取消",
    succeeded: "已完成",
    failed: "失败",
    cancelled: "已取消",
    skipped: "已跳过",
    unknown: "待核实",
    active: "可用",
    archived: "已归档",
    resolved: "已处理",
    expired: "已过期",
    withdrawn: "已撤回",
    ready: "已就绪",
    completed: "已完成",
  })[value] ?? value;
export function readRoute(): Route {
  const parameters = new URLSearchParams(window.location.hash.slice(1));
  const view = parameters.get("view");
  if (view === "space" && parameters.get("space_id"))
    return {
      view,
      space_id: parameters.get("space_id")!,
      tab: parameters.get("tab") ?? "overview",
    };
  if (view === "artifact" && parameters.get("artifact_id"))
    return { view, artifact_id: parameters.get("artifact_id")! };
  if (view === "attachment" && parameters.get("attachment_id"))
    return { view, attachment_id: parameters.get("attachment_id")! };
  if (view === "execution" && parameters.get("execution_id"))
    return {
      view,
      execution_id: parameters.get("execution_id")!,
      tab: parameters.get("tab") ?? "overview",
      graph_run_id: parameters.get("graph_run_id") ?? undefined,
      run_id: parameters.get("run_id") ?? undefined,
      node_id: parameters.get("node_id") ?? undefined,
      node_attempt_id: parameters.get("node_attempt_id") ?? undefined,
    };
  return {
    view:
      view === "spaces" ||
      view === "resources" ||
      view === "settings" ||
      view === "attentions"
        ? view
        : "chat",
  };
}
export function paneIdentity(route: Route): string {
  switch (route.view) {
    case "space":
      return `space/${route.space_id}`;
    case "execution":
      return `execution/${route.execution_id}`;
    case "artifact":
      return `artifact/${route.artifact_id}`;
    case "attachment":
      return `attachment/${route.attachment_id}`;
    default:
      return route.view;
  }
}
export function routeIdentity(route: Route): string {
  const normalized =
    route.view === "space" || route.view === "execution"
      ? { ...route, tab: route.tab ?? "overview" }
      : route;
  return JSON.stringify(
    Object.entries(normalized)
      .filter(([, value]) => value !== undefined)
      .sort(([left], [right]) => left.localeCompare(right)),
  );
}
export function navigate(route: Route) {
  const parameters = new URLSearchParams();
  Object.entries(route).forEach(([key, value]) => {
    if (value) parameters.set(key, value);
  });
  const hash = `#${parameters.toString()}`;
  if (window.location.hash !== hash) window.history.pushState(null, "", hash);
}
export function graphPositions(
  graph: Graph,
): Map<string, { x: number; y: number }> {
  const rank = new Map(graph.nodes.map((node) => [node.node_id, 0]));
  const incoming = new Map(graph.nodes.map((node) => [node.node_id, 0]));
  const children = new Map(
    graph.nodes.map((node) => [node.node_id, [] as string[]]),
  );
  for (const edge of graph.edges) {
    if (
      !incoming.has(edge.source_node_id) ||
      !incoming.has(edge.target_node_id)
    )
      throw new Error("图依赖指向不存在的节点。");
    incoming.set(edge.target_node_id, incoming.get(edge.target_node_id)! + 1);
    children.get(edge.source_node_id)!.push(edge.target_node_id);
  }
  const queue = graph.nodes
    .filter((node) => incoming.get(node.node_id) === 0)
    .map((node) => node.node_id);
  for (let index = 0; index < queue.length; index++)
    for (const child of children.get(queue[index])!) {
      rank.set(child, Math.max(rank.get(child)!, rank.get(queue[index])! + 1));
      incoming.set(child, incoming.get(child)! - 1);
      if (incoming.get(child) === 0) queue.push(child);
    }
  if (queue.length !== graph.nodes.length) throw new Error("图依赖包含循环。");
  const rows = new Map<number, number>();
  return new Map(
    graph.nodes.map((node) => {
      const column = rank.get(node.node_id)!;
      const row = rows.get(column) ?? 0;
      rows.set(column, row + 1);
      return [node.node_id, { x: column * 300, y: row * 120 }];
    }),
  );
}
