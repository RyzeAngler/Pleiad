import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import {
  ArrowDown,
  ArrowsOutSimple,
  Check,
  CheckCircle,
  Clock,
  Code,
  Cube,
  DownloadSimple,
  FloppyDisk,
  FlowArrow,
  Minus,
  Plus,
  SpinnerGap,
  Stack,
  WarningCircle,
  Wrench,
  X,
} from "@phosphor-icons/react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FailureDetails } from "./FailureDetails";
import {
  eventLabel,
  graphKey,
  graphPositions,
  graphNodeStatus,
  latestAttempts,
  statusLabel,
} from "./projection";
import type {
  ExecutionDetails,
  ExecutionEvent,
  Graph,
  GraphNode,
  GraphReference,
  Json,
  NodeRun,
  Resources,
} from "./types";
import "./Workbench.css";

export type TaskNode = Node<
  {
    spec: GraphNode;
    status: string;
    selected: boolean;
    modelName: string;
    dependencies: GraphNode[];
    resources?: Resources | null;
    tone: "source" | "work" | "alternate" | "sink";
  },
  "task"
>;

export function TaskGraphNode({ data }: NodeProps<TaskNode>) {
  const completed = ["succeeded", "completed"].includes(data.status);
  const active = ["running", "ready"].includes(data.status);
  const failed = data.status === "failed";
  const StatusIcon = completed
    ? CheckCircle
    : active
      ? SpinnerGap
      : failed
        ? WarningCircle
        : Clock;
  return (
    <div
      className={`task-node tone-${data.tone} ${data.selected ? "selected" : ""} node-${data.status}`}
    >
      {data.dependencies.length > 0 && (
        <Handle type="target" position={Position.Left} />
      )}
      <div className="node-heading">
        <FlowArrow size={21} weight="bold" />
        <h3 title={data.spec.objective}>{data.spec.objective}</h3>
        <span className="node-state">
          <StatusIcon size={17} className={active ? "node-spinner" : ""} />
          {statusLabel(data.status)}
        </span>
      </div>
      <div className="node-body">
        <p className="node-summary">{data.spec.prompt}</p>
        <div className="node-resource-group">
          <h4>配置资源</h4>
          <div className="node-chips">
            <span className="node-chip" title={`模型：${data.modelName}`}>
              <Cube size={15} />
              <span>{data.modelName}</span>
            </span>
            {data.spec.tool_codes.map((code) => (
              <span
                className="node-chip tool-chip"
                title={`Tool · ${code}`}
                key={`tool:${code}`}
              >
                <Wrench size={14} />
                <span>
                  {data.resources?.tools.find((item) => item.code === code)
                    ?.name ?? code}
                </span>
              </span>
            ))}
            {data.spec.skill_codes.map((code) => (
              <span
                className="node-chip skill-chip"
                title={`Skill · ${code}`}
                key={`skill:${code}`}
              >
                <Stack size={15} />
                <span>
                  {data.resources?.skills.find((item) => item.code === code)
                    ?.name ?? code}
                </span>
              </span>
            ))}
          </div>
        </div>
        {data.dependencies.length > 0 && (
          <div className="node-resource-group">
            <h4>依赖步骤</h4>
            <div className="node-chips">
              {data.dependencies.map((dependency) => (
                <span
                  className="node-chip dependency-chip"
                  key={dependency.node_id}
                  title={`${dependency.node_id}：${dependency.objective}`}
                >
                  <FlowArrow size={14} />
                  <span>{dependency.objective}</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
      {data.tone !== "sink" && (
        <Handle type="source" position={Position.Right} />
      )}
    </div>
  );
}

const nodeTypes = { task: TaskGraphNode };

export function ResultValue({ value }: { value: Json }) {
  if (typeof value === "string")
    return (
      <div className="markdown">
        <Markdown remarkPlugins={[remarkGfm]}>{value}</Markdown>
      </div>
    );
  if (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    typeof value.text === "string" &&
    Object.keys(value).length === 1
  )
    return (
      <div className="markdown">
        <Markdown remarkPlugins={[remarkGfm]}>{value.text}</Markdown>
      </div>
    );
  return <pre className="json-value">{JSON.stringify(value, null, 2)}</pre>;
}

function download(value: Json, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `${name.replace(/[^\p{L}\p{N}._-]/gu, "_")}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

function NodeDetails({
  node,
  run,
  status,
  hasGraphRun,
  details,
  onClose,
  onAsk,
}: {
  node: GraphNode;
  run?: NodeRun;
  status: string;
  hasGraphRun: boolean;
  details: ExecutionDetails;
  onClose: () => void;
  onAsk: (node: GraphNode) => void;
}) {
  const artifact = run
    ? details.artifacts.find((item) => item.artifact_id === run.artifact_id)
    : undefined;
  return (
    <section
      className="node-details"
      aria-label="节点详情"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <div className="section-heading">
        <div>
          <span className="eyebrow">节点详情</span>
          <h3>{node.objective}</h3>
        </div>
        <button
          className="icon-button"
          aria-label="关闭节点详情"
          title="关闭节点详情"
          onClick={onClose}
        >
          <X />
        </button>
      </div>
      <p className="node-identity">
        <code>{node.node_id}</code>· {statusLabel(status)}
      </p>
      <dl className="node-bindings">
        <div>
          <dt>模型角色</dt>
          <dd>{node.model_role}</dd>
        </div>
        {run && (
          <div>
            <dt>执行模型</dt>
            <dd>{run.model_name ?? "未记录"}</dd>
          </div>
        )}
        <div>
          <dt>Tools</dt>
          <dd>{node.tool_codes.join(", ") || "未单独配置"}</dd>
        </div>
        <div>
          <dt>Skills</dt>
          <dd>{node.skill_codes.join(", ") || "未单独配置"}</dd>
        </div>
      </dl>
      <div className="node-io">
        <section>
          <h4>输入</h4>
          {run?.input ? (
            <ResultValue value={run.input} />
          ) : (
            <p className="muted">
              {run
                ? "未记录输入"
                : hasGraphRun
                  ? "此轮无节点执行记录"
                  : "此版本暂无执行记录"}
            </p>
          )}
        </section>
        <section>
          <h4>输出</h4>
          {artifact ? (
            <ResultValue value={artifact.value} />
          ) : (
            <p className="muted">
              {run ? "暂无输出记录" : hasGraphRun ? "此轮无输出记录" : "未执行"}
            </p>
          )}
        </section>
      </div>
      {run?.error && <FailureDetails error={run.error} />}
      <details className="disclosure">
        <summary>节点指令与结构</summary>
        <pre>
          {JSON.stringify(
            {
              prompt: node.prompt,
              input_schema: node.input_schema,
              output_schema: node.output_schema,
              ...(run
                ? {
                    attempt: run.node_run_id,
                    sdk_run_id: run.sdk_run_id,
                    status: run.status,
                  }
                : {}),
            },
            null,
            2,
          )}
        </pre>
      </details>
      <button className="text-button" onClick={() => onAsk(node)}>
        询问节点详情 <ArrowDown size={13} />
      </button>
    </section>
  );
}

export function Workbench({
  details,
  events,
  onClose,
  onAsk,
  savedGraph,
  savingGraph,
  onSaveGraph,
  resources,
}: {
  details: ExecutionDetails;
  events: ExecutionEvent[];
  onClose: () => void;
  onAsk: (node: GraphNode) => void;
  savedGraph: GraphReference | null;
  savingGraph: boolean;
  onSaveGraph: (graph: GraphReference) => void;
  resources?: Resources | null;
}) {
  const [tab, setTab] = useState<"graph" | "result" | "activity">(
    details.graphs.length ? "graph" : "result",
  );
  const tabSelected = useRef(false);
  useEffect(() => {
    if (details.graphs.length && !tabSelected.current) setTab("graph");
  }, [details.graphs.length]);
  const [selectedGraph, setSelectedGraph] = useState("");
  const [selectedGraphRunId, setSelectedGraphRunId] = useState("");
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [sizes, setSizes] = useState(
    new Map<string, { width: number; height: number }>(),
  );
  const [zoom, setZoom] = useState(1);
  const flow = useRef<ReactFlowInstance<TaskNode> | null>(null);
  const fittedRevision = useRef("");
  const graph: Graph | undefined =
    details.graphs.find((item) => graphKey(item) === selectedGraph) ??
    details.graphs.at(-1);
  const graphRuns = useMemo(
    () =>
      details.graph_runs
        .filter(
          (run) =>
            run.graph_id === graph?.graph_id &&
            run.graph_revision === graph.revision,
        )
        .sort((a, b) => a.started_at.localeCompare(b.started_at)),
    [graph, details.graph_runs],
  );
  const graphRun =
    graphRuns.find((run) => run.graph_run_id === selectedGraphRunId) ??
    graphRuns.at(-1);
  const attempts = useMemo(
    () => latestAttempts(graphRun, details.node_runs),
    [graphRun, details.node_runs],
  );
  const positions = useMemo(
    () =>
      graph
        ? graphPositions(graph, sizes)
        : new Map<string, { x: number; y: number }>(),
    [graph, sizes],
  );
  const nodes: TaskNode[] = useMemo(
    () =>
      graph?.nodes.map((spec, index) => ({
        id: spec.node_id,
        type: "task",
        selected: selectedNode === spec.node_id,
        ariaLabel: `${spec.node_id}：${spec.objective}`,
        position: positions.get(spec.node_id) ?? { x: 0, y: 0 },
        data: {
          spec,
          modelName:
            attempts.get(spec.node_id)?.model_name ??
            resources?.models.find((model) => model.role === spec.model_role)
              ?.model ??
            `模型角色 · ${spec.model_role}`,
          status: graphNodeStatus(
            spec.node_id,
            graphRun,
            attempts.get(spec.node_id),
          ),
          selected: selectedNode === spec.node_id,
          dependencies: graph.nodes.filter((parent) =>
            graph.edges.some(
              (edge) =>
                edge.source_node_id === parent.node_id &&
                edge.target_node_id === spec.node_id,
            ),
          ),
          resources,
          tone: !graph.edges.some(
            (edge) => edge.target_node_id === spec.node_id,
          )
            ? "source"
            : !graph.edges.some((edge) => edge.source_node_id === spec.node_id)
              ? "sink"
              : index % 2
                ? "work"
                : "alternate",
        },
      })) ?? [],
    [graph, graphRun, positions, attempts, selectedNode, resources],
  );
  const edges = useMemo<Edge[]>(
    () =>
      graph?.edges.map((edge, i) => ({
        id: `${edge.source_node_id}:${edge.target_node_id}:${i}`,
        source: edge.source_node_id,
        target: edge.target_node_id,
        type: "default",
        selectable: false,
        animated: attempts.get(edge.target_node_id)?.status === "running",
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 16,
          height: 16,
          color:
            attempts.get(edge.target_node_id)?.status === "running"
              ? "#4650ed"
              : "#b0b7ca",
        },
        style: {
          strokeWidth: 1.7,
          strokeDasharray: "6 6",
          stroke:
            attempts.get(edge.target_node_id)?.status === "running"
              ? "#4650ed"
              : "#b0b7ca",
        },
      })) ?? [],
    [graph, attempts],
  );
  const layoutKey = JSON.stringify([...positions]);
  const revisionKey = graph ? graphKey(graph) : "";
  useEffect(() => {
    if (
      !graph ||
      tab !== "graph" ||
      fittedRevision.current === revisionKey ||
      graph.nodes.some((item) => !sizes.has(item.node_id))
    )
      return;
    const frame = requestAnimationFrame(() => {
      void flow.current?.fitView({ padding: 0.06, maxZoom: 1, duration: 0 });
      fittedRevision.current = revisionKey;
    });
    return () => cancelAnimationFrame(frame);
  }, [graph, layoutKey, revisionKey, sizes, tab]);
  const node = graph?.nodes.find((item) => item.node_id === selectedNode);
  const graphSaved =
    graph?.graph_id === savedGraph?.graph_id &&
    graph?.revision === savedGraph?.revision;
  const lastEvent = events
    .filter((event) => !event.event_type.includes("delta"))
    .at(-1);
  return (
    <aside
      className={`workbench workbench-view-${tab}`}
      aria-label="任务工作画布"
    >
      <div className="workbench-toolbar">
        <div className="tabs" role="tablist" aria-label="工作画布视图">
          {(["graph", "result", "activity"] as const).map((id) => (
            <button
              key={id}
              role="tab"
              aria-selected={tab === id}
              onClick={() => {
                tabSelected.current = true;
                setTab(id);
              }}
            >
              {id === "graph" ? "编排" : id === "result" ? "结果" : "执行记录"}
            </button>
          ))}
        </div>
        <div className="row">
          {tab === "graph" && graph && (
            <>
              <select
                aria-label="Graph 版本"
                value={graphKey(graph)}
                onChange={(event) => {
                  setSelectedGraph(event.target.value);
                  setSelectedGraphRunId("");
                  setSelectedNode(null);
                  setSizes(new Map());
                }}
              >
                {details.graphs.map((item) => (
                  <option key={graphKey(item)} value={graphKey(item)}>
                    {item.graph_id} · v{item.revision}
                  </option>
                ))}
              </select>
              {graphRuns.length > 1 && graphRun && (
                <select
                  aria-label="编排执行轮次"
                  value={graphRun.graph_run_id}
                  onChange={(event) => {
                    setSelectedGraphRunId(event.target.value);
                    setSelectedNode(null);
                  }}
                >
                  {graphRuns.map((run, index) => (
                    <option key={run.graph_run_id} value={run.graph_run_id}>
                      第 {index + 1} 次 · {statusLabel(run.status)}
                    </option>
                  ))}
                </select>
              )}
              <button
                className="text-button graph-save-button"
                disabled={savingGraph || graphSaved}
                onClick={() =>
                  onSaveGraph({
                    graph_id: graph.graph_id,
                    revision: graph.revision,
                  })
                }
              >
                {graphSaved ? <Check size={17} /> : <FloppyDisk size={17} />}
                {savingGraph
                  ? "保存中…"
                  : graphSaved
                    ? "已保存到空间"
                    : "保存到空间"}
              </button>
            </>
          )}
          <button
            className="icon-button"
            aria-label="收起工作画布"
            title="收起工作画布"
            onClick={onClose}
          >
            <X />
          </button>
        </div>
      </div>
      {tab === "graph" && (
        <>
          {graph ? (
            <div className="graph-stage">
              <ReactFlow
                key={graphKey(graph)}
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                onInit={(instance) => {
                  flow.current = instance;
                }}
                onViewportChange={(viewport) => setZoom(viewport.zoom)}
                onMoveStart={(event) => {
                  if (event) fittedRevision.current = revisionKey;
                }}
                nodesDraggable={false}
                nodesConnectable={false}
                edgesReconnectable={false}
                edgesFocusable={false}
                deleteKeyCode={null}
                multiSelectionKeyCode={null}
                elementsSelectable
                onNodesChange={(changes) => {
                  const measured = changes.filter(
                    (change) =>
                      change.type === "dimensions" && change.dimensions,
                  );
                  if (measured.length)
                    setSizes((previous) => {
                      const next = new Map(previous);
                      let changed = false;
                      for (const measurement of measured) {
                        if (
                          measurement.type !== "dimensions" ||
                          !measurement.dimensions
                        )
                          continue;
                        const { width, height } = measurement.dimensions;
                        const old = previous.get(measurement.id);
                        if (old?.width !== width || old?.height !== height) {
                          next.set(measurement.id, { width, height });
                          changed = true;
                        }
                      }
                      return changed ? next : previous;
                    });
                  const selection = changes.find(
                    (change) => change.type === "select" && change.selected,
                  );
                  if (selection?.type === "select")
                    setSelectedNode(selection.id);
                  else if (
                    changes.some(
                      (change) =>
                        change.type === "select" &&
                        !change.selected &&
                        change.id === selectedNode,
                    )
                  )
                    setSelectedNode(null);
                }}
                ariaLabelConfig={{
                  "controls.zoomIn.ariaLabel": "放大编排",
                  "controls.zoomOut.ariaLabel": "缩小编排",
                  "controls.fitView.ariaLabel": "适应画布",
                  "node.a11yDescription.default":
                    "按 Enter 或空格查看节点详情，按 Escape 关闭详情。编排修改通过对话完成。",
                  "node.a11yDescription.keyboardDisabled":
                    "按 Enter 或空格查看节点详情，按 Escape 关闭详情。编排修改通过对话完成。",
                }}
                fitView
                fitViewOptions={{ padding: 0.06, maxZoom: 1 }}
                minZoom={0.08}
                maxZoom={1.7}
                colorMode="light"
              >
                <Background
                  variant={BackgroundVariant.Dots}
                  gap={24}
                  size={1}
                />
              </ReactFlow>
            </div>
          ) : (
            <div className="canvas-empty">
              <FlowArrow size={27} weight="light" />
              <h3>暂无编排</h3>
              <p>可在对话中要求生成编排。</p>
            </div>
          )}
          {graph && (
            <div className="graph-controls" aria-label="画布缩放">
              <button
                aria-label="缩小编排"
                title="缩小编排"
                disabled={zoom <= 0.08}
                onClick={() => {
                  fittedRevision.current = revisionKey;
                  void flow.current?.zoomOut({ duration: 160 });
                }}
              >
                <Minus size={17} />
              </button>
              <span>{Math.round(zoom * 100)}%</span>
              <button
                aria-label="放大编排"
                title="放大编排"
                disabled={zoom >= 1.7}
                onClick={() => {
                  fittedRevision.current = revisionKey;
                  void flow.current?.zoomIn({ duration: 160 });
                }}
              >
                <Plus size={17} />
              </button>
              <button
                aria-label="适应画布"
                title="适应画布"
                onClick={() => {
                  fittedRevision.current = revisionKey;
                  void flow.current?.fitView({
                    padding: 0.06,
                    maxZoom: 1,
                    duration: 160,
                  });
                }}
              >
                <ArrowsOutSimple size={17} />
              </button>
            </div>
          )}
          {node && graph && (
            <NodeDetails
              node={node}
              run={attempts.get(node.node_id)}
              status={graphNodeStatus(
                node.node_id,
                graphRun,
                attempts.get(node.node_id),
              )}
              hasGraphRun={graphRun !== undefined}
              details={details}
              onClose={() => setSelectedNode(null)}
              onAsk={(item) => {
                setSelectedNode(null);
                onAsk(item);
              }}
            />
          )}
        </>
      )}
      {tab === "result" && (
        <div className="results">
          <div className="section-heading">
            <div>
              <span className="eyebrow">本次回复</span>
              <h3>
                {details.execution.output !== null ? "执行结果" : "暂无结果"}
              </h3>
            </div>
            {details.execution.output !== null && (
              <button
                className="icon-button"
                aria-label="下载执行结果"
                title="下载执行结果"
                onClick={() =>
                  download(
                    details.execution.output,
                    details.execution.execution_id,
                  )
                }
              >
                <DownloadSimple />
              </button>
            )}
          </div>
          {details.execution.output !== null ? (
            <ResultValue value={details.execution.output} />
          ) : details.execution.error ? (
            <FailureDetails error={details.execution.error} />
          ) : (
            <p className="muted">当前执行暂无结果。</p>
          )}
          {details.artifacts.length > 0 && (
            <section className="artifact-list">
              <h4>节点产物 · 本次执行</h4>
              {details.artifacts.map((artifact) => (
                <details key={artifact.artifact_id} className="artifact">
                  <summary>
                    <Code size={16} />
                    {details.node_runs.find(
                      (run) => run.node_run_id === artifact.node_run_id,
                    )?.node_id ?? artifact.artifact_id}
                    <span className="muted">
                      {(() => {
                        const attempt = details.node_runs.find(
                          (run) => run.node_run_id === artifact.node_run_id,
                        );
                        return attempt
                          ? `${attempt.graph_id} · v${attempt.graph_revision}`
                          : "JSON";
                      })()}
                    </span>
                  </summary>
                  <p className="artifact-origin">
                    {(() => {
                      const attempt = details.node_runs.find(
                        (run) => run.node_run_id === artifact.node_run_id,
                      );
                      return attempt
                        ? `${attempt.graph_id} · v${attempt.graph_revision} · ${new Date(attempt.started_at).toLocaleString("zh-CN")} · 尝试 ${attempt.node_run_id.slice(0, 10)}`
                        : `尝试 ${artifact.node_run_id}`;
                    })()}
                  </p>
                  <ResultValue value={artifact.value} />
                  <button
                    className="text-button"
                    onClick={() =>
                      download(artifact.value, artifact.artifact_id)
                    }
                  >
                    <DownloadSimple size={14} /> 下载产物
                  </button>
                </details>
              ))}
            </section>
          )}
        </div>
      )}
      {tab === "activity" && (
        <div className="activity-list">
          <div className="section-heading">
            <h3>执行记录</h3>
            <span className="muted">
              {events.filter((event) => event.sequence > 0).length} 条记录
            </span>
          </div>
          {events
            .filter((event) => event.sequence > 0)
            .map((event) => (
              <details key={event.event_id} className="activity-row">
                <summary>
                  <time>
                    {new Date(event.timestamp).toLocaleTimeString("zh-CN", {
                      hour12: false,
                    })}
                  </time>
                  <span>{eventLabel(event)}</span>
                </summary>
                <pre>
                  {JSON.stringify(
                    {
                      sequence: event.sequence,
                      type: event.event_type,
                      data: event.data,
                      trace_id: event.trace_id,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            ))}
        </div>
      )}
      <div className="canvas-status">
        <span className="row">
          {details.execution.status === "succeeded" ? (
            <Check size={14} />
          ) : (
            <Clock size={14} />
          )}{" "}
          {statusLabel(details.execution.status)}
        </span>
        <span
          className="canvas-event"
          title={lastEvent ? eventLabel(lastEvent) : undefined}
        >
          {lastEvent ? eventLabel(lastEvent) : "暂无执行记录"}
        </span>
      </div>
    </aside>
  );
}
