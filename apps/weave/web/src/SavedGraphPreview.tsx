import { useEffect, useMemo, useRef, useState } from "react";
import { Background, Controls, MarkerType, ReactFlow } from "@xyflow/react";
import { ArrowClockwise, X } from "@phosphor-icons/react";
import { api, errorMessage, isAbort } from "./api";
import { graphPositions } from "./projection";
import { TaskGraphNode, type TaskNode } from "./Workbench";
import type { Graph, GraphReference, Resources } from "./types";
import "./SavedGraphPreview.css";

const nodeTypes = { task: TaskGraphNode };

export function SavedGraphPreview({
  spaceId,
  reference,
  resources,
  onClose,
}: {
  spaceId: string;
  reference: GraphReference;
  resources: Resources | null;
  onClose: () => void;
}) {
  const [graph, setGraph] = useState<Graph | null>(null);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement;
    panel.current?.focus();
    function keydown(event: KeyboardEvent) {
      if (event.key === "Escape" && !event.isComposing) {
        event.preventDefault();
        close.current();
      }
      if (event.key === "Tab") {
        const elements = Array.from(
          panel.current?.querySelectorAll<HTMLElement>(
            'button:not(:disabled), a[href], input, summary, [tabindex="0"]',
          ) ?? [],
        );
        const first = elements[0];
        const last = elements.at(-1);
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === panel.current)
        ) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    function containFocus(event: FocusEvent) {
      if (
        event.target instanceof Node &&
        !panel.current?.contains(event.target)
      )
        panel.current?.focus();
    }
    document.addEventListener("keydown", keydown);
    document.addEventListener("focusin", containFocus);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.removeEventListener("focusin", containFocus);
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setGraph(null);
    setError("");
    setStale(false);
    api
      .savedGraph(spaceId, controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        if (
          value.graph_id !== reference.graph_id ||
          value.revision !== reference.revision
        ) {
          setError("空间编排已更新，请关闭预览并重新加载空间。");
          setStale(true);
          return;
        }
        setGraph(value);
      })
      .catch((failure) => {
        if (!isAbort(failure) && !controller.signal.aborted)
          setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [spaceId, reference.graph_id, reference.revision, retry]);
  const nodes = useMemo<TaskNode[]>(() => {
    if (!graph) return [];
    const positions = graphPositions(graph);
    return graph.nodes.map((spec) => ({
      id: spec.node_id,
      type: "task",
      position: positions.get(spec.node_id)!,
      selected: selected === spec.node_id,
      ariaLabel: `${spec.node_id}：${spec.objective}`,
      data: {
        spec,
        status: "pending",
        selected: selected === spec.node_id,
        modelName:
          resources?.models.find((model) => model.role === spec.model_role)
            ?.model ?? spec.model_role,
        dependencies: graph.nodes.filter((parent) =>
          graph.edges.some(
            (edge) =>
              edge.source_node_id === parent.node_id &&
              edge.target_node_id === spec.node_id,
          ),
        ),
        resources,
        tone: !graph.edges.some((edge) => edge.target_node_id === spec.node_id)
          ? "source"
          : !graph.edges.some((edge) => edge.source_node_id === spec.node_id)
            ? "sink"
            : "work",
      },
    }));
  }, [graph, resources, selected]);
  const edges = useMemo(
    () =>
      graph?.edges.map((edge, index) => ({
        id: `${index}`,
        source: edge.source_node_id,
        target: edge.target_node_id,
        selectable: false,
        markerEnd: { type: MarkerType.ArrowClosed, color: "#a4adc2" },
        style: { stroke: "#a4adc2", strokeDasharray: "6 6" },
      })) ?? [],
    [graph],
  );
  const node = graph?.nodes.find((item) => item.node_id === selected);
  return (
    <div
      className="modal-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        className="saved-graph-preview"
        role="dialog"
        aria-modal="true"
        aria-labelledby="saved-graph-preview-title"
        tabIndex={-1}
      >
        <header>
          <div>
            <h2 id="saved-graph-preview-title">已保存的编排</h2>
            <p>
              {graph?.objective ?? reference.graph_id}{" "}
              <span>v{reference.revision}</span>
            </p>
          </div>
          <button
            className="icon-button"
            aria-label="关闭编排预览"
            onClick={onClose}
          >
            <X />
          </button>
        </header>
        {error ? (
          <div className="preview-error" role="alert">
            <p>{error}</p>
            {!stale && (
              <button
                className="text-button"
                onClick={() => setRetry((value) => value + 1)}
              >
                <ArrowClockwise />
                重新加载
              </button>
            )}
          </div>
        ) : !graph ? (
          <p className="preview-error" role="status">
            正在读取编排…
          </p>
        ) : (
          <div className="saved-graph-body">
            <div className="saved-graph-canvas">
              <ReactFlow
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                fitView
                fitViewOptions={{ padding: 0.12, maxZoom: 1 }}
                minZoom={0.08}
                maxZoom={1.7}
                nodesDraggable={false}
                nodesConnectable={false}
                edgesReconnectable={false}
                deleteKeyCode={null}
                ariaLabelConfig={{
                  "controls.zoomIn.ariaLabel": "放大编排",
                  "controls.zoomOut.ariaLabel": "缩小编排",
                  "controls.fitView.ariaLabel": "适应画布",
                }}
                onNodeClick={(_, item) => setSelected(item.id)}
                onNodesChange={(changes) => {
                  const change = changes.find(
                    (item) => item.type === "select" && item.selected,
                  );
                  if (change?.type === "select") setSelected(change.id);
                }}
              >
                <Background gap={24} size={1} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
            {node && (
              <aside className="saved-node-detail" aria-label="节点指令">
                <div className="section-heading">
                  <h3>{node.objective}</h3>
                  <button
                    className="icon-button"
                    aria-label="关闭节点指令"
                    onClick={() => setSelected(null)}
                  >
                    <X />
                  </button>
                </div>
                <p>{node.prompt}</p>
                <dl>
                  <dt>模型角色</dt>
                  <dd>{node.model_role}</dd>
                  <dt>Tools</dt>
                  <dd>{node.tool_codes.join(", ") || "未配置"}</dd>
                  <dt>Skills</dt>
                  <dd>{node.skill_codes.join(", ") || "未配置"}</dd>
                </dl>
              </aside>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
