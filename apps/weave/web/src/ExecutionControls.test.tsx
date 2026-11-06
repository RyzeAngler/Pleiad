// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import App from "./App";
import type { Approval, Conversation, Execution, Space } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

function execution(id: string, status: Execution["status"]): Execution {
  return {
    execution_id: id,
    conversation_id: "task",
    objective: `请求 ${id}`,
    status,
    graph_id: null,
    graph_revision: null,
    output: status === "succeeded" ? { text: `结果 ${id}` } : null,
    error: null,
    created_at:
      id === "first" ? "2026-11-01T00:00:00Z" : "2026-11-02T00:00:00Z",
    updated_at: "2026-11-02T00:00:00Z",
  };
}

function installApi(records: Execution[] = [], withGraph = false) {
  const space: Space = {
    space_id: "research",
    name: "执行检查",
    instructions: "核实来源",
    preferred_tool_codes: [],
    preferred_skill_codes: [],
    saved_graph: null,
    revision: 1,
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
  };
  const conversation: Conversation = {
    conversation_id: "task",
    space_id: space.space_id,
    title: "已有任务",
    created_at: space.created_at,
    updated_at: space.updated_at,
  };
  const state = {
    spaces: [space],
    conversations: records.length ? [conversation] : [],
    records,
    failLoad: false,
    failSubmit: false,
    failDecision: false,
    failDraft: false,
    writes: [] as { path: string; body: unknown }[],
  };
  const approval: Approval = {
    execution_id: "first",
    deadline: "2099-01-01T00:00:00Z",
    interruption: {
      interruption_id: "review",
      tool_code: "publish",
      tool_name: "发布文章",
      reason: "发布前需要确认",
      arguments: { title: "待审阅稿件" },
      details: { destination: "测试发布目标" },
    },
  };
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      close = vi.fn();
    },
  );
  Element.prototype.scrollIntoView = vi.fn();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const body: Record<string, string> | null = init?.body
        ? JSON.parse(String(init.body))
        : null;
      if (init?.method === "POST") {
        state.writes.push({ path, body });
        if (path === "/v1/spaces/draft") {
          if (state.failDraft)
            return Response.json(
              { detail: "空间草稿生成失败" },
              { status: 502 },
            );
          return Response.json({
            reply: "已整理规则。",
            draft: {
              name: "内容编辑",
              instructions: "发布前审阅稿件",
              preferred_tool_codes: [],
              preferred_skill_codes: [],
            },
          });
        }
        if (path === "/v1/spaces") {
          const created = {
            ...space,
            space_id: "created",
            name: body!.name,
            instructions: body!.instructions,
          };
          state.spaces.push(created);
          return Response.json(created, { status: 201 });
        }
        if (path === "/v1/spaces/research/conversations") {
          const created = { ...conversation, title: body!.title };
          state.conversations.push(created);
          return Response.json(created, { status: 201 });
        }
        if (path === "/v1/executions") {
          if (state.failSubmit)
            return Response.json({ detail: "模型暂时不可用" }, { status: 503 });
          const created = {
            ...execution("submitted", "succeeded"),
            objective: body!.objective,
          };
          state.records.push(created);
          return Response.json({ execution: created }, { status: 202 });
        }
        if (path === "/v1/executions/first/cancel") {
          state.records[0] = { ...state.records[0], status: "cancelled" };
          return Response.json({ execution: state.records[0] });
        }
        if (path === "/v1/executions/first/approvals/review") {
          if (state.failDecision)
            return Response.json({ detail: "审批已过期" }, { status: 409 });
          state.records[0] = {
            ...state.records[0],
            status: body!.decision === "approve" ? "succeeded" : "cancelled",
          };
          return Response.json(
            { execution: state.records[0] },
            { status: 202 },
          );
        }
      }
      if (path === "/v1/spaces") {
        if (state.failLoad)
          return Response.json({ detail: "工作区读取失败" }, { status: 503 });
        return Response.json(state.spaces);
      }
      if (path === "/v1/resources")
        return Response.json({
          models: [],
          tools: [],
          skills: [],
          workspace_path: "/workspace",
          capability_file: "/workspace/capabilities.yaml",
        });
      if (path.endsWith("/conversations"))
        return Response.json(state.conversations);
      if (path === "/v1/executions?conversation_id=task")
        return Response.json(state.records);
      if (path === "/v1/conversations/task/messages") return Response.json([]);
      for (const record of state.records) {
        if (path === `/v1/executions/${record.execution_id}/details`)
          return Response.json({
            execution: record,
            graphs: withGraph
              ? [
                  {
                    graph_id: "plan",
                    revision: 1,
                    objective: "安排任务",
                    nodes: [
                      {
                        node_id: "step",
                        objective: "核对资料",
                        prompt: "核实来源",
                        model_role: "text",
                        tool_codes: [],
                        skill_codes: [],
                        input_schema: {},
                        output_schema: {},
                      },
                    ],
                    edges: [],
                  },
                ]
              : [],
            node_runs: [],
            graph_runs: [],
            artifacts: [],
          });
        if (path === `/v1/executions/${record.execution_id}/events`)
          return Response.json([]);
        if (path === `/v1/executions/${record.execution_id}/approvals`)
          return Response.json(
            record.status === "waiting_approval" ? [approval] : [],
          );
      }
      throw new Error(`Unexpected endpoint: ${path}`);
    },
  );
  return state;
}

async function openSpace() {
  fireEvent.click(
    await screen.findByRole("button", { name: "执行检查，进入空间" }),
  );
  await screen.findByRole("textbox", { name: "发送消息" });
}

async function openTask() {
  await openSpace();
  fireEvent.click(await screen.findByRole("button", { name: "已有任务" }));
  await screen.findByRole("combobox", { name: "查看执行记录" });
}

it.each(["button", "Enter"])(
  "submits the first message with %s and shows the backend execution result",
  async (method) => {
    const state = installApi();
    render(<App />);
    await openSpace();
    const composer = screen.getByRole("textbox", { name: "发送消息" });
    expect(
      (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.change(composer, { target: { value: "   " } });
    fireEvent.keyDown(composer, { key: "Enter" });
    expect(state.writes).toEqual([]);
    fireEvent.change(composer, { target: { value: "  整理资料  " } });
    if (method === "button")
      fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    else fireEvent.keyDown(composer, { key: "Enter" });
    await screen.findAllByText("结果 submitted");
    expect(state.writes).toEqual([
      {
        path: "/v1/spaces/research/conversations",
        body: { title: "整理资料" },
      },
      {
        path: "/v1/executions",
        body: { conversation_id: "task", objective: "整理资料", input: {} },
      },
    ]);
    expect(window.location.hash).toBe("#space=research&conversation=task");
    expect(
      (screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement)
        .value,
    ).toBe("");
  },
);

it.each([{ shiftKey: true }, { isComposing: true }])(
  "keeps Enter inside the composer without submission for %j",
  async (keys) => {
    const state = installApi();
    render(<App />);
    await openSpace();
    const composer = screen.getByRole("textbox", { name: "发送消息" });
    fireEvent.change(composer, { target: { value: "继续输入" } });
    expect(fireEvent.keyDown(composer, { key: "Enter", ...keys })).toBe(true);
    expect(state.writes).toEqual([]);
    expect((composer as HTMLTextAreaElement).value).toBe("继续输入");
  },
);

it("preserves a failed message draft and retries in the already created conversation", async () => {
  const state = installApi();
  state.failSubmit = true;
  render(<App />);
  await openSpace();
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), {
    target: { value: "稍后重试这条消息" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("模型暂时不可用");
  expect(
    (screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement)
      .value,
  ).toBe("稍后重试这条消息");
  state.failSubmit = false;
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findAllByText("结果 submitted");
  expect(
    state.writes.filter((write) => write.path.endsWith("/conversations")),
  ).toHaveLength(1);
  expect(
    state.writes.filter((write) => write.path === "/v1/executions"),
  ).toHaveLength(2);
});

it("stops the active execution and removes the stop control after cancellation", async () => {
  const state = installApi([execution("first", "running")]);
  render(<App />);
  await openTask();
  fireEvent.click(await screen.findByRole("button", { name: "停止当前执行" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "停止当前执行" })).toBeNull(),
  );
  expect(state.writes).toEqual([
    { path: "/v1/executions/first/cancel", body: null },
  ]);
  expect(screen.getByRole("status").textContent).toContain("执行已取消");
});

it.each(["approve", "reject"] as const)(
  "reviews approval parameters and submits %s for the displayed interruption",
  async (decision) => {
    const state = installApi([execution("first", "waiting_approval")]);
    render(<App />);
    await openTask();
    const parameters = await screen.findByRole("button", {
      name: "操作与参数",
    });
    expect(screen.getByText(/待审阅稿件/).textContent).toContain(
      "测试发布目标",
    );
    fireEvent.click(parameters);
    expect(screen.queryByText(/待审阅稿件/)).toBeNull();
    expect(parameters.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(parameters);
    expect(screen.getByText(/待审阅稿件/).textContent).toContain(
      "测试发布目标",
    );
    fireEvent.click(
      screen.getByRole("button", {
        name: decision === "approve" ? "批准执行" : "拒绝",
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "批准执行" })).toBeNull(),
    );
    expect(state.writes).toEqual([
      { path: "/v1/executions/first/approvals/review", body: { decision } },
    ]);
    expect(
      (
        screen.getByRole("combobox", {
          name: "查看执行记录",
        }) as HTMLSelectElement
      ).selectedOptions[0].textContent,
    ).toContain(decision === "approve" ? "完成" : "取消");
  },
);

it("shows an approval conflict instead of reporting a successful decision", async () => {
  const state = installApi([execution("first", "waiting_approval")]);
  state.failDecision = true;
  render(<App />);
  await openTask();
  fireEvent.click(await screen.findByRole("button", { name: "批准执行" }));
  expect((await screen.findByRole("alert")).textContent).toContain(
    "审批已过期",
  );
  expect(screen.getByRole("button", { name: "批准执行" })).toBeDefined();
  expect(state.records[0].status).toBe("waiting_approval");
});

it("switches execution results and reopens a closed work canvas", async () => {
  installApi(
    [execution("first", "succeeded"), execution("second", "succeeded")],
    true,
  );
  render(<App />);
  await openTask();
  const canvas = await screen.findByRole("complementary", {
    name: "任务工作画布",
  });
  fireEvent.click(within(canvas).getByRole("tab", { name: "结果" }));
  await within(canvas).findByText("结果 second");
  fireEvent.change(screen.getByRole("combobox", { name: "查看执行记录" }), {
    target: { value: "first" },
  });
  const previousCanvas = await screen.findByRole("complementary", {
    name: "任务工作画布",
  });
  fireEvent.click(within(previousCanvas).getByRole("tab", { name: "结果" }));
  await within(previousCanvas).findByText("结果 first");
  fireEvent.click(
    within(
      screen.getByRole("complementary", { name: "任务工作画布" }),
    ).getByRole("button", { name: "收起工作画布" }),
  );
  expect(
    screen.queryByRole("complementary", { name: "任务工作画布" }),
  ).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "打开工作画布" }));
  const reopened = screen.getByRole("complementary", { name: "任务工作画布" });
  fireEvent.click(within(reopened).getByRole("tab", { name: "结果" }));
  expect(within(reopened).getByText("结果 first")).toBeDefined();
});

it("reloads failed workspace data when the user retries", async () => {
  const state = installApi();
  state.failLoad = true;
  render(<App />);
  await screen.findByText("工作区读取失败");
  state.failLoad = false;
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByRole("button", { name: "执行检查，进入空间" });
  expect(screen.queryByRole("alert")).toBeNull();
  expect(state.writes).toEqual([]);
});

it("previews and edits a conversational space draft before creating it without an execution", async () => {
  const state = installApi();
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "创建空间" }));
  const dialog = screen.getByRole("dialog", { name: "创建空间" });
  expect(
    (
      within(dialog).getByRole("button", {
        name: "创建并进入",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  fireEvent.change(
    within(dialog).getByRole("textbox", { name: "描述空间用途" }),
    { target: { value: "发布前审阅稿件" } },
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "整理空间配置" }));
  const name = await within(dialog).findByRole("textbox", { name: "空间名称" });
  expect(state.spaces).toHaveLength(1);
  expect(state.records).toHaveLength(0);
  fireEvent.change(name, { target: { value: "内容运营" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "创建并进入" }));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "创建空间" })).toBeNull(),
  );
  expect(window.location.hash).toBe("#space=created");
  expect(state.writes).toEqual([
    {
      path: "/v1/spaces/draft",
      body: {
        messages: [{ role: "user", content: "发布前审阅稿件" }],
        draft: null,
      },
    },
    {
      path: "/v1/spaces",
      body: {
        name: "内容运营",
        instructions: "发布前审阅稿件",
        preferred_tool_codes: [],
        preferred_skill_codes: [],
      },
    },
  ]);
  expect(screen.getByRole("heading", { name: "开始新的任务" })).toBeDefined();
});

it("keeps a failed space description available for retry without creating records", async () => {
  const state = installApi();
  state.failDraft = true;
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "创建空间" }));
  const dialog = screen.getByRole("dialog", { name: "创建空间" });
  const description = within(dialog).getByRole("textbox", {
    name: "描述空间用途",
  });
  fireEvent.change(description, { target: { value: "写旅行文案" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "整理空间配置" }));
  expect((await within(dialog).findByRole("alert")).textContent).toContain(
    "空间草稿生成失败",
  );
  expect((description as HTMLTextAreaElement).value).toBe("写旅行文案");
  expect(state.spaces).toHaveLength(1);
  expect(
    (
      within(dialog).getByRole("button", {
        name: "创建并进入",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  state.failDraft = false;
  fireEvent.click(within(dialog).getByRole("button", { name: "整理空间配置" }));
  expect(
    (
      (await within(dialog).findByRole("textbox", {
        name: "空间名称",
      })) as HTMLInputElement
    ).value,
  ).toBe("内容编辑");
});

it.each(["close", "Escape"])(
  "dismisses space creation with %s without creating records",
  async (method) => {
    const state = installApi();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "创建空间" }));
    const dialog = screen.getByRole("dialog", { name: "创建空间" });
    fireEvent.change(
      within(dialog).getByRole("textbox", { name: "描述空间用途" }),
      { target: { value: "尚未提交" } },
    );
    if (method === "close")
      fireEvent.click(
        within(dialog).getByRole("button", { name: "关闭创建空间" }),
      );
    else fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "创建空间" })).toBeNull();
    expect(state.writes).toEqual([]);
  },
);

it("keeps a task without graphs in conversation view while switching its execution results", async () => {
  installApi([
    execution("first", "succeeded"),
    execution("second", "succeeded"),
  ]);
  render(<App />);
  await openTask();
  await screen.findByText("结果 second");
  expect(
    screen.queryByRole("complementary", { name: "任务工作画布" }),
  ).toBeNull();
  expect(screen.queryByRole("button", { name: "打开工作画布" })).toBeNull();
  fireEvent.change(screen.getByRole("combobox", { name: "查看执行记录" }), {
    target: { value: "first" },
  });
  await screen.findByText("结果 first");
  expect(screen.queryByText("结果 second")).toBeNull();
});

it("clears execution results after deletion and ignores events queued by the closed stream", async () => {
  const state = installApi([execution("first", "succeeded")]);
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  const streams: EventTarget[] = [];
  vi.stubGlobal(
    "EventSource",
    class extends EventTarget {
      constructor() {
        super();
        streams.push(this);
      }
      close = vi.fn();
    },
  );
  const baseFetch = globalThis.fetch;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (
      String(input) === "/v1/conversations/task" &&
      init?.method === "DELETE"
    ) {
      state.records = [];
      state.conversations = [];
      return Promise.resolve(Response.json({ deleted: true }));
    }
    return baseFetch(input, init);
  });
  render(<App />);
  await openTask();
  await screen.findByText("结果 first");
  fireEvent.click(screen.getByRole("button", { name: "删除任务" }));
  fireEvent.click(
    within(screen.getByRole("alertdialog", { name: "删除任务" })).getByRole(
      "button",
      { name: "确认删除任务" },
    ),
  );
  await screen.findByRole("heading", { name: "开始新的任务" });
  await act(async () => {
    for (const stream of streams)
      stream.dispatchEvent(
        new MessageEvent("execution", { data: "late invalid event" }),
      );
  });
  expect(screen.queryByText("结果 first")).toBeNull();
  expect(screen.queryByRole("combobox", { name: "查看执行记录" })).toBeNull();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(window.location.hash).toBe("#space=research");
});
