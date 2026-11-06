// @vitest-environment jsdom
import { useState } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { Workbench } from "./Workbench";
import type { ExecutionDetails, ExecutionEvent, GraphReference } from "./types";

const details: ExecutionDetails = {
  execution: {
    execution_id: "trip-run",
    conversation_id: "trip-task",
    objective: "安排两天行程",
    status: "succeeded",
    graph_id: "trip",
    graph_revision: 2,
    output: { text: "已完成两天行程。" },
    error: null,
    created_at: "2026-11-04T00:00:00Z",
    updated_at: "2026-11-04T00:02:00Z",
  },
  graphs: [1, 2].map((revision) => ({
    graph_id: "trip",
    revision,
    objective: "安排两天行程",
    nodes: [
      {
        node_id: "plan",
        objective:
          revision === 1 ? "规划景点顺序" : "核对开放时间后规划景点顺序",
        prompt: "按照公共交通安排两天行程",
        model_role: "text",
        tool_codes: ["search"],
        skill_codes: [],
        input_schema: {},
        output_schema: {},
      },
    ],
    edges: [],
  })),
  node_runs: [
    {
      node_run_id: "plan-attempt",
      graph_run_id: "trip-graph-run",
      execution_id: "trip-run",
      graph_id: "trip",
      graph_revision: 2,
      node_id: "plan",
      status: "succeeded",
      input: { days: 2 },
      model_name: "test-model",
      artifact_id: "itinerary",
      error: null,
      sdk_run_id: "sdk-run",
      sdk_run_status: "completed",
      started_at: "2026-11-04T00:01:00Z",
      finished_at: "2026-11-04T00:02:00Z",
    },
  ],
  graph_runs: [
    {
      graph_run_id: "trip-graph-run",
      graph_id: "trip",
      graph_revision: 2,
      status: "succeeded",
      started_at: "2026-11-04T00:01:00Z",
      state: { node_states: [{ node_id: "plan", status: "succeeded" }] },
    },
  ],
  artifacts: [
    {
      artifact_id: "itinerary",
      node_run_id: "plan-attempt",
      media_type: "application/json",
      value: { text: "第一天参观博物馆。" },
    },
  ],
};
const events: ExecutionEvent[] = [
  {
    event_id: "finished",
    execution_id: "trip-run",
    event_type: "execution.completed",
    sequence: 1,
    timestamp: "2026-11-04T00:02:00Z",
    data: { status: "succeeded" },
    trace_id: null,
    span_id: null,
  },
];

function WorkbenchPage() {
  const [question, setQuestion] = useState("");
  const [open, setOpen] = useState(true);
  const [savedGraph, setSavedGraph] = useState<GraphReference | null>(null);
  return (
    <>
      <textarea
        aria-label="发送消息"
        value={question}
        onChange={(event) => setQuestion(event.target.value)}
      />
      {open && (
        <Workbench
          details={details}
          events={events}
          onClose={() => setOpen(false)}
          onAsk={(node) => setQuestion(`解释 ${node.node_id}`)}
          savedGraph={savedGraph}
          savingGraph={false}
          onSaveGraph={setSavedGraph}
        />
      )}
      {savedGraph && (
        <output>
          空间默认编排：{savedGraph.graph_id} v{savedGraph.revision}
        </output>
      )}
    </>
  );
}

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it.each(["Enter", " "])(
  "opens graph node details with %s and closes them with Escape",
  (key) => {
    render(<WorkbenchPage />);
    const node = screen.getByTestId("rf__node-plan");
    fireEvent.keyDown(node, { key });
    const panel = screen.getByRole("region", { name: "节点详情" });
    expect(within(panel).getByText("test-model").textContent).toBe(
      "test-model",
    );
    expect(panel.textContent).toContain("第一天参观博物馆。");
    fireEvent.keyDown(node, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "节点详情" })).toBeNull();
  },
);

it("returns a selected node to the conversation and closes its detail overlay", () => {
  render(<WorkbenchPage />);
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  fireEvent.click(screen.getByRole("button", { name: "询问节点详情" }));
  expect(screen.queryByRole("region", { name: "节点详情" })).toBeNull();
  expect(
    (screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement)
      .value,
  ).toBe("解释 plan");
});

it("reopens the same node after closing its details", () => {
  render(<WorkbenchPage />);
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  fireEvent.click(screen.getByRole("button", { name: "关闭节点详情" }));
  expect(screen.queryByRole("region", { name: "节点详情" })).toBeNull();
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  expect(
    screen.getByRole("region", { name: "节点详情" }).textContent,
  ).toContain("核对开放时间后规划景点顺序");
  fireEvent.keyDown(screen.getByRole("region", { name: "节点详情" }), {
    key: "Escape",
  });
  expect(screen.queryByRole("region", { name: "节点详情" })).toBeNull();
});

it("saves the selected graph revision without showing another revision's node output", () => {
  render(<WorkbenchPage />);
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  fireEvent.change(screen.getByRole("combobox", { name: "Graph 版本" }), {
    target: { value: "trip:1" },
  });
  expect(screen.queryByRole("region", { name: "节点详情" })).toBeNull();
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  const panel = screen.getByRole("region", { name: "节点详情" });
  expect(panel.textContent).toContain("此版本暂无执行记录");
  expect(panel.textContent).not.toContain("第一天参观博物馆。");
  fireEvent.click(screen.getByRole("button", { name: "保存到空间" }));
  expect(screen.getByRole("status").textContent).toBe("空间默认编排：trip v1");
  expect(
    (
      screen.getByRole("button", {
        name: "已保存到空间",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
});

it("opens results and activity before closing the work canvas", () => {
  render(<WorkbenchPage />);
  fireEvent.click(screen.getByRole("tab", { name: "结果" }));
  expect(screen.getByText("已完成两天行程。").textContent).toBe(
    "已完成两天行程。",
  );
  expect(screen.getByRole("button", { name: "下载执行结果" })).toBeDefined();
  fireEvent.click(screen.getByRole("tab", { name: "执行记录" }));
  expect(screen.getByText("1 条记录").textContent).toBe("1 条记录");
  expect(screen.getByText(/"sequence": 1/).textContent).toContain(
    '"type": "execution.completed"',
  );
  fireEvent.click(screen.getByRole("button", { name: "收起工作画布" }));
  expect(
    screen.queryByRole("complementary", { name: "任务工作画布" }),
  ).toBeNull();
});

it.each([
  {
    button: "下载执行结果",
    filename: "trip-run.json",
    value: details.execution.output,
  },
  {
    button: "下载产物",
    filename: "itinerary.json",
    value: details.artifacts[0].value,
  },
])(
  "downloads the selected $button with its own JSON content",
  async ({ button, filename, value }) => {
    const blobs: Blob[] = [];
    const downloads: { href: string; filename: string }[] = [];
    vi.stubGlobal(
      "URL",
      class extends URL {
        static createObjectURL(blob: Blob) {
          blobs.push(blob);
          return "blob:weave-download";
        }
        static revokeObjectURL() {}
      },
    );
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloads.push({ href: this.href, filename: this.download });
    });
    render(<WorkbenchPage />);
    fireEvent.click(screen.getByRole("tab", { name: "结果" }));
    if (button === "下载产物")
      fireEvent.click(screen.getByText("plan", { selector: "summary" }));
    fireEvent.click(screen.getByRole("button", { name: button }));
    expect(downloads).toEqual([{ href: "blob:weave-download", filename }]);
    expect(blobs).toHaveLength(1);
    expect(blobs[0].type).toBe("application/json");
    const content = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () =>
        typeof reader.result === "string"
          ? resolve(reader.result)
          : reject(new Error("Expected JSON text"));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(blobs[0]);
    });
    expect(JSON.parse(content)).toEqual(value);
  },
);

it("shows configured resource names and upstream objectives on the graph itself", () => {
  const source = details.graphs[1].nodes[0];
  const dependent = {
    ...source,
    node_id: "review",
    objective: "核对预算",
    tool_codes: ["search", "custom-tool"],
    skill_codes: ["data-audit"],
  };
  render(
    <Workbench
      details={{
        ...details,
        graph_runs: [],
        node_runs: [],
        graphs: [
          {
            ...details.graphs[1],
            nodes: [source, dependent],
            edges: [{ source_node_id: "plan", target_node_id: "review" }],
          },
        ],
      }}
      events={events}
      resources={{
        models: [{ role: "text", model: "Configured model" }],
        tools: [{ code: "search", name: "检索资料", description: "" }],
        skills: [{ code: "data-audit", name: "数据核对", description: "" }],
        workspace_path: "",
        capability_file: "",
      }}
      onClose={() => {}}
      onAsk={() => {}}
      savedGraph={null}
      savingGraph={false}
      onSaveGraph={() => {}}
    />,
  );
  const node = within(screen.getByTestId("rf__node-review"));
  expect(node.getByText("配置资源")).toBeDefined();
  expect(node.getByText("Configured model")).toBeDefined();
  expect(node.getByText("检索资料")).toBeDefined();
  expect(node.getByText("数据核对")).toBeDefined();
  expect(node.getByText("custom-tool")).toBeDefined();
  expect(node.getByText("依赖步骤")).toBeDefined();
  expect(node.getByText("核对开放时间后规划景点顺序")).toBeDefined();
  expect(node.getByText("未开始")).toBeDefined();
});

function renderSnapshot(snapshot: ExecutionDetails) {
  return render(
    <Workbench
      details={snapshot}
      events={events}
      onClose={() => {}}
      onAsk={() => {}}
      savedGraph={null}
      savingGraph={false}
      onSaveGraph={() => {}}
    />,
  );
}

it("keeps node status and output within the selected execution of the same graph revision", () => {
  const plan = details.graphs[1].nodes[0];
  const first = details.graph_runs[0];
  const second = {
    ...first,
    graph_run_id: "trip-second-run",
    status: "failed",
    started_at: "2026-11-04T00:03:00Z",
    state: {
      node_states: [
        { node_id: "prepare", status: "failed" },
        { node_id: "plan", status: "skipped" },
      ],
    },
  };
  renderSnapshot({
    ...details,
    graphs: [
      {
        ...details.graphs[1],
        nodes: [{ ...plan, node_id: "prepare", objective: "检查资料" }, plan],
        edges: [{ source_node_id: "prepare", target_node_id: "plan" }],
      },
    ],
    graph_runs: [
      {
        ...first,
        state: {
          node_states: [
            { node_id: "prepare", status: "succeeded" },
            { node_id: "plan", status: "succeeded" },
          ],
        },
      },
      second,
    ],
    node_runs: [
      {
        ...details.node_runs[0],
        node_run_id: "prepare-first",
        node_id: "prepare",
        artifact_id: null,
      },
      details.node_runs[0],
      {
        ...details.node_runs[0],
        node_run_id: "prepare-second",
        graph_run_id: second.graph_run_id,
        node_id: "prepare",
        status: "failed",
        artifact_id: null,
        started_at: second.started_at,
      },
    ],
  });
  expect(
    within(screen.getByTestId("rf__node-prepare")).getByText("失败"),
  ).toBeDefined();
  expect(
    within(screen.getByTestId("rf__node-plan")).getByText("已跳过"),
  ).toBeDefined();
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  let panel = screen.getByRole("region", { name: "节点详情" });
  expect(panel.textContent).toContain("已跳过");
  expect(panel.textContent).toContain("此轮无输出记录");
  expect(panel.textContent).not.toContain("第一天参观博物馆。");
  fireEvent.change(screen.getByRole("combobox", { name: "编排执行轮次" }), {
    target: { value: first.graph_run_id },
  });
  expect(
    within(screen.getByTestId("rf__node-prepare")).getByText("已完成"),
  ).toBeDefined();
  expect(
    within(screen.getByTestId("rf__node-plan")).getByText("已完成"),
  ).toBeDefined();
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  panel = screen.getByRole("region", { name: "节点详情" });
  expect(panel.textContent).toContain("第一天参观博物馆。");
});

it("shows a skipped branch without manufacturing a node attempt", () => {
  renderSnapshot({
    ...details,
    graph_runs: [
      {
        ...details.graph_runs[0],
        state: {
          node_states: [{ node_id: "plan", status: "skipped" }],
        },
      },
    ],
    node_runs: [],
    artifacts: [],
  });
  expect(
    within(screen.getByTestId("rf__node-plan")).getByText("已跳过"),
  ).toBeDefined();
  expect(
    within(screen.getByTestId("rf__node-plan")).queryByText("未开始"),
  ).toBeNull();
});

it("keeps historical unassociated node output out of a terminal run with unrecorded state", () => {
  renderSnapshot({
    ...details,
    graph_runs: [{ ...details.graph_runs[0], state: null }],
    node_runs: [{ ...details.node_runs[0], graph_run_id: null }],
  });
  expect(
    within(screen.getByTestId("rf__node-plan")).getByText("结果未确认"),
  ).toBeDefined();
  fireEvent.click(screen.getByTestId("rf__node-plan"));
  const panel = screen.getByRole("region", { name: "节点详情" });
  expect(panel.textContent).not.toContain("第一天参观博物馆。");
  expect(panel.textContent).toContain("结果未确认");
});

it("shows an active node attempt before the graph checkpoint is available", () => {
  renderSnapshot({
    ...details,
    graph_runs: [{ ...details.graph_runs[0], status: "running", state: null }],
    node_runs: [
      {
        ...details.node_runs[0],
        status: "running",
        artifact_id: null,
        finished_at: null,
      },
    ],
  });
  expect(
    within(screen.getByTestId("rf__node-plan")).getByText("执行中"),
  ).toBeDefined();
});
