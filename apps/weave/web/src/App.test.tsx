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
import App, { ExecutionNotice } from "./App";
import { ResultValue } from "./Workbench";
import { SpaceSettings } from "./SpaceSettings";
import { FailureDetails } from "./FailureDetails";
import type { Conversation, Execution, Space } from "./types";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

it.each(["failed", "cancelled"] as const)(
  "keeps the submitted request visible after a %s Turn is absent from SDK history",
  (status) => {
    const execution: Execution = {
      execution_id: "attempt",
      conversation_id: "task",
      objective: "执行刚才的两节点编排",
      status,
      graph_id: null,
      graph_revision: null,
      output: null,
      error:
        status === "failed"
          ? {
              code: "model.request.invalid",
              message: "请求被拒绝",
              type: "system",
            }
          : null,
      created_at: "2026-11-04T00:00:00Z",
      updated_at: "2026-11-04T00:00:00Z",
    };
    render(<ExecutionNotice execution={execution} />);
    expect(screen.getByRole("status").textContent).toContain(
      "本次请求：执行刚才的两节点编排",
    );
    expect(screen.getByRole("status").textContent).toContain(
      status === "failed" ? "请求被拒绝" : "执行已取消",
    );
  },
);

it("renders a generated Markdown table as readable result columns", () => {
  render(
    <ResultValue
      value={{ text: "| 日期 | 计划 |\n| --- | --- |\n| 周一 | 参观博物馆 |" }}
    />,
  );
  const table = screen.getByRole("table");
  expect(
    within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent),
  ).toEqual(["日期", "计划"]);
  expect(within(table).getByRole("cell", { name: "参观博物馆" })).toBeDefined();
});

it.each([
  {
    code: "sandbox.runtime.unavailable",
    message: "docker info failed: connection refused",
    explanation: "沙箱容器引擎不可用。",
    action: "恢复后重新提交请求",
  },
  {
    code: "sandbox.cleanup.failed",
    message: "Container creation failed; container lookup failed",
    explanation: "沙箱容器清理失败。",
    action: "残留容器",
  },
])("explains $code while preserving the original diagnosis", (scenario) => {
  render(<FailureDetails error={{ ...scenario, type: "system" }} />);
  expect(screen.getByText(scenario.explanation)).toBeDefined();
  expect(screen.getByText(new RegExp(scenario.action))).toBeDefined();
  fireEvent.click(screen.getByText("诊断信息"));
  expect(screen.getByText(scenario.message)).toBeDefined();
  expect(screen.getByText(scenario.code)).toBeDefined();
});

it.each([false, true])(
  "saves space preferences while preserving or explicitly clearing the saved Graph (clear=%s)",
  async (clearGraph) => {
    const space: Space = {
      space_id: "space-a",
      name: "研究",
      instructions: "核实来源",
      preferred_tool_codes: ["search"],
      preferred_skill_codes: [],
      saved_graph: { graph_id: "research-graph", revision: 4 },
      revision: 7,
      created_at: "2026-11-03T00:00:00Z",
      updated_at: "2026-11-03T00:00:00Z",
    };
    let submitted: unknown;
    vi.stubGlobal(
      "fetch",
      async (input: RequestInfo | URL, init?: RequestInit) => {
        if (
          String(input) !== "/v1/spaces/space-a" ||
          init?.method !== "PATCH"
        ) {
          throw new Error(`Unexpected endpoint: ${input}`);
        }
        submitted = JSON.parse(String(init.body));
        return Response.json({
          ...space,
          name: "旅行研究",
          saved_graph: clearGraph ? null : space.saved_graph,
          revision: 8,
        });
      },
    );
    render(
      <SpaceSettings
        space={space}
        resources={null}
        onSaved={() => undefined}
        onClose={() => undefined}
        onViewGraph={() => undefined}
      />,
    );
    fireEvent.change(screen.getByRole("textbox", { name: "空间名称" }), {
      target: { value: "旅行研究" },
    });
    if (clearGraph)
      fireEvent.click(screen.getByRole("button", { name: "取消默认编排" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(screen.getByText("已保存，对后续提交生效")).toBeDefined(),
    );
    expect(submitted).toEqual({
      name: "旅行研究",
      instructions: "核实来源",
      preferred_tool_codes: ["search"],
      preferred_skill_codes: [],
      saved_graph: clearGraph
        ? null
        : { graph_id: "research-graph", revision: 4 },
      expected_revision: 7,
    });
    if (clearGraph) expect(screen.queryByText("research-graph")).toBeNull();
    else expect(screen.getByText("research-graph")).toBeDefined();
  },
);

it("keeps a newly selected space and its draft when a previous space request finishes late", async () => {
  let finishCreation: (response: Response) => void = () => {
    throw new Error("Creation not started");
  };
  const creation = new Promise<Response>((resolve) => {
    finishCreation = resolve;
  });
  const spaces = ["a", "b"].map((id) => ({
    space_id: `space-${id}`,
    name: `研究${id.toUpperCase()}`,
    instructions: "核实来源",
    preferred_tool_codes: [],
    preferred_skill_codes: [],
    saved_graph: null,
    revision: 1,
    created_at: "2026-11-03T00:00:00Z",
    updated_at: "2026-11-03T00:00:00Z",
  }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/v1/spaces") return Response.json(spaces);
      if (path === "/v1/resources")
        return Response.json({
          models: [],
          tools: [],
          skills: [],
          workspace_path: "/workspace",
          capability_file: "/workspace/capabilities.yaml",
        });
      if (
        path === "/v1/spaces/space-a/conversations" &&
        init?.method === "POST"
      )
        return creation;
      if (path.endsWith("/conversations")) return Response.json([]);
      if (path === "/v1/executions" && init?.method === "POST")
        return Response.json(
          { detail: "provider unavailable" },
          { status: 503 },
        );
      throw new Error(`Unexpected endpoint: ${path}`);
    }),
  );
  Element.prototype.scrollIntoView = vi.fn();
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /^研究A/ }));
  const composer = await screen.findByRole("textbox", { name: "发送消息" });
  fireEvent.change(composer, { target: { value: "调查一项事实" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  fireEvent.click(screen.getByRole("button", { name: "切换空间" }));
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "空间列表" })).getByRole(
      "button",
      { name: /研究B/ },
    ),
  );
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), {
    target: { value: "另一个空间的未发送草稿" },
  });
  await act(async () =>
    finishCreation(
      Response.json({
        conversation_id: "old-space-task",
        space_id: "space-a",
        title: "调查一项事实",
        created_at: "2026-11-03T00:00:00Z",
        updated_at: "2026-11-03T00:00:00Z",
      }),
    ),
  );
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toContain(
      "其他空间的消息提交失败",
    ),
  );
  expect(window.location.hash).toBe("#space=space-b");
  expect(
    (screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement)
      .value,
  ).toBe("另一个空间的未发送草稿");
  expect(screen.getByRole("heading", { name: "开始新的任务" })).toBeDefined();
});

function installNavigationApi(
  deleteResult?: (path: string) => Promise<Response>,
) {
  const now = new Date().toISOString();
  const older = "2020-01-01T12:00:00Z";
  const spaces: Space[] = [
    ["travel", "旅行规划"],
    ["writing", "文案工作室"],
  ].map(([space_id, name]) => ({
    space_id,
    name,
    instructions: "核实来源",
    preferred_tool_codes: [],
    preferred_skill_codes: [],
    saved_graph: null,
    revision: 1,
    created_at: older,
    updated_at: now,
  }));
  const conversations: Conversation[] = [
    {
      conversation_id: "kyoto",
      space_id: "travel",
      title: "京都五日旅行",
      created_at: now,
      updated_at: now,
    },
    {
      conversation_id: "hangzhou",
      space_id: "travel",
      title: "杭州周末行程",
      created_at: older,
      updated_at: older,
    },
    {
      conversation_id: "post",
      space_id: "writing",
      title: "京都旅行文案",
      created_at: now,
      updated_at: now,
    },
  ];
  const writes: string[] = [];
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (init?.method && init.method !== "GET") {
        writes.push(path);
        if (init.method === "DELETE" && deleteResult) {
          const result = await deleteResult(path);
          if (result.ok) {
            const spaceId = path.startsWith("/v1/spaces/")
              ? path.split("/").at(-1)
              : null;
            const taskId = path.startsWith("/v1/conversations/")
              ? path.split("/").at(-1)
              : null;
            for (let index = conversations.length - 1; index >= 0; index--) {
              if (
                conversations[index].space_id === spaceId ||
                conversations[index].conversation_id === taskId
              )
                conversations.splice(index, 1);
            }
            const spaceIndex = spaces.findIndex(
              (space) => space.space_id === spaceId,
            );
            if (spaceIndex >= 0) spaces.splice(spaceIndex, 1);
          }
          return result;
        }
        throw new Error(`Unexpected write: ${path}`);
      }
      if (path === "/v1/spaces") return Response.json(spaces);
      if (path === "/v1/resources")
        return Response.json({
          models: [{ role: "text", model: "test-model" }],
          tools: [],
          skills: [],
          workspace_path: "/workspace",
          capability_file: "/workspace/capabilities.yaml",
        });
      for (const space of spaces) {
        if (path === `/v1/spaces/${space.space_id}/conversations`)
          return Response.json(
            conversations.filter((item) => item.space_id === space.space_id),
          );
      }
      if (path.startsWith("/v1/executions?conversation_id="))
        return Response.json([]);
      if (path.startsWith("/v1/conversations/") && path.endsWith("/messages"))
        return Response.json([]);
      throw new Error(`Unexpected endpoint: ${path}`);
    },
  );
  Element.prototype.scrollIntoView = vi.fn();
  return { writes, spaces, conversations };
}

async function selectTravelSpace() {
  fireEvent.click(await screen.findByRole("button", { name: /^旅行规划/ }));
  await screen.findByRole("heading", { name: "开始新的任务" });
}

it("keeps the space switcher limited to space names and opens settings independently", async () => {
  const { writes } = installNavigationApi();
  render(<App />);
  await selectTravelSpace();
  const composer = screen.getByRole("textbox", { name: "发送消息" });
  fireEvent.change(composer, { target: { value: "尚未发送的行程" } });
  fireEvent.click(screen.getByRole("button", { name: "切换空间" }));
  const dialog = screen.getByRole("dialog", { name: "切换空间" });
  expect(
    within(dialog)
      .getAllByRole("button")
      .map((button) => button.textContent),
  ).toEqual(["旅行规划", "文案工作室"]);
  fireEvent.keyDown(dialog, { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  expect(screen.getByRole("heading", { name: "空间设置" })).toBeDefined();
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .value,
  ).toBe("核实来源");
  expect(screen.queryByRole("button", { name: "切换空间" })).toBeNull();
  expect(window.location.hash).toBe("#space=travel&view=space-settings");
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(screen.getByRole("heading", { name: "开始新的任务" })).toBeDefined();
  expect(
    (screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement)
      .value,
  ).toBe("尚未发送的行程");
  expect(window.location.hash).toBe("#space=travel");
  expect(writes).toEqual([]);
});

it("starts a new task in the same space without creating a conversation until a message is sent", async () => {
  const { writes } = installNavigationApi();
  render(<App />);
  await selectTravelSpace();
  expect(screen.queryByRole("button", { name: "新任务" })).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "京都五日旅行" }));
  await screen.findByRole("heading", { name: "京都五日旅行" });
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), {
    target: { value: "旧任务的未发送内容" },
  });
  fireEvent.click(screen.getByRole("button", { name: "新任务" }));
  const composer = screen.getByRole("textbox", { name: "发送消息" });
  await waitFor(() => expect(document.activeElement).toBe(composer));
  expect(window.location.hash).toBe("#space=travel");
  expect(screen.getByRole("heading", { name: "开始新的任务" })).toBeDefined();
  expect(screen.queryByRole("button", { name: "新任务" })).toBeNull();
  expect((composer as HTMLTextAreaElement).value).toBe("");
  expect(writes).toEqual([]);
});

it("switches spaces from the context menu and reads the new space history", async () => {
  installNavigationApi();
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(screen.getByRole("button", { name: "切换空间" }));
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "空间列表" })).getByRole(
      "button",
      { name: "文案工作室" },
    ),
  );
  await screen.findByRole("button", { name: "京都旅行文案" });
  await waitFor(() =>
    expect(document.activeElement).toBe(
      screen.getByRole("textbox", { name: "发送消息" }),
    ),
  );
  expect(window.location.hash).toBe("#space=writing");
  expect(screen.queryByRole("button", { name: "京都五日旅行" })).toBeNull();
  expect(screen.queryByRole("button", { name: "杭州周末行程" })).toBeNull();
});

it("opens task history while resource discovery is still pending", async () => {
  installNavigationApi();
  const originalFetch = globalThis.fetch;
  let finishResources!: (response: Response) => void;
  const resources = new Promise<Response>((resolve) => {
    finishResources = resolve;
  });
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
    String(input) === "/v1/resources" ? resources : originalFetch(input, init),
  );
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(await screen.findByRole("button", { name: "京都五日旅行" }));
  await screen.findByRole("heading", { name: "京都五日旅行" });
  fireEvent.click(screen.getByRole("button", { name: "资源库" }));
  expect(screen.getByText("正在加载资源…")).toBeDefined();
  await act(async () => finishResources(await originalFetch("/v1/resources")));
  expect(screen.queryByText("正在加载资源…")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "模型" }));
  expect(screen.getByText("test-model")).toBeDefined();
});

it("keeps stored messages readable and reports resource discovery failure until retry succeeds", async () => {
  installNavigationApi();
  const originalFetch = globalThis.fetch;
  let failing = true;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/v1/resources" && failing)
      return Promise.resolve(
        Response.json({ detail: "MCP 服务不可用" }, { status: 503 }),
      );
    if (String(input) === "/v1/conversations/kyoto/messages")
      return Promise.resolve(
        Response.json([
          {
            message_id: "saved-answer",
            turn_id: "saved-turn",
            turn_sequence: 1,
            agent_id: "supervisor",
            turn_completed: true,
            role: "assistant",
            content: "已保存的京都行程",
            tool_calls: [],
            tool_call_id: null,
            refusal: null,
          },
        ]),
      );
    return originalFetch(input, init);
  });
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(await screen.findByRole("button", { name: "京都五日旅行" }));
  await screen.findByText("已保存的京都行程");
  expect(screen.getByRole("alert").textContent).toContain(
    "资源目录加载失败：MCP 服务不可用",
  );
  failing = false;
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(screen.getByText("已保存的京都行程")).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "资源库" }));
  fireEvent.click(screen.getByRole("tab", { name: "模型" }));
  expect(screen.getByText("test-model")).toBeDefined();
});

it("ignores an obsolete resource failure after a newer retry has loaded the catalog", async () => {
  installNavigationApi();
  const originalFetch = globalThis.fetch;
  let resourceRequests = 0;
  let finishObsolete!: (response: Response) => void;
  const obsolete = new Promise<Response>((resolve) => {
    finishObsolete = resolve;
  });
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/v1/resources") {
      resourceRequests++;
      if (resourceRequests === 1)
        return Promise.resolve(
          Response.json({ detail: "首次资源请求失败" }, { status: 503 }),
        );
      if (resourceRequests === 2) return obsolete;
    }
    return originalFetch(input, init);
  });
  render(<App />);
  await screen.findByText("资源目录加载失败：首次资源请求失败");
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  fireEvent.click(screen.getByRole("button", { name: "资源库" }));
  fireEvent.click(screen.getByRole("tab", { name: "模型" }));
  expect(screen.getByText("test-model")).toBeDefined();
  await act(async () =>
    finishObsolete(
      Response.json({ detail: "过时的资源请求失败" }, { status: 503 }),
    ),
  );
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("test-model")).toBeDefined();
});

it.each(["space", "task"])(
  "returns from the global resource library to the original %s with its unsent message intact",
  async (origin) => {
    const { writes } = installNavigationApi();
    render(<App />);
    await selectTravelSpace();
    if (origin === "task") {
      fireEvent.click(
        await screen.findByRole("button", { name: "京都五日旅行" }),
      );
      await screen.findByRole("heading", { name: "京都五日旅行" });
    }
    const originalHash = window.location.hash;
    fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), {
      target: { value: "保留这段未发送内容" },
    });
    fireEvent.click(screen.getByRole("button", { name: "资源库" }));
    expect(screen.getByRole("heading", { name: "资源库" })).toBeDefined();
    expect(screen.queryByRole("textbox", { name: "空间名称" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: "默认指令" })).toBeNull();
    expect(screen.queryByRole("button", { name: "切换空间" })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "模型" }));
    expect(screen.getByText("test-model")).toBeDefined();
    expect(window.location.hash).toBe(`${originalHash}&view=resources`);
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    expect(
      screen.getByRole("heading", {
        name: origin === "task" ? "京都五日旅行" : "开始新的任务",
      }),
    ).toBeDefined();
    expect(
      (screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement)
        .value,
    ).toBe("保留这段未发送内容");
    expect(window.location.hash).toBe(originalHash);
    expect(writes).toEqual([]);
  },
);

it.each(["home", "space"])(
  "searches all spaces from %s and opens the result in its owning space",
  async (origin) => {
    const { writes } = installNavigationApi();
    render(<App />);
    if (origin === "space") await selectTravelSpace();
    else await screen.findByRole("button", { name: /^旅行规划/ });
    fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
    const dialog = screen.getByRole("dialog", { name: "搜索任务" });
    const query = within(dialog).getByRole("textbox", {
      name: "搜索全部空间的任务",
    });
    expect((query as HTMLInputElement).disabled).toBe(false);
    expect(document.activeElement).toBe(query);
    await within(dialog).findByRole("button", { name: /京都旅行文案/ });
    fireEvent.change(query, { target: { value: " 京都 " } });
    expect(
      within(dialog).getByRole("button", { name: /京都五日旅行.*旅行规划/ }),
    ).toBeDefined();
    expect(
      within(dialog).queryByRole("button", { name: /杭州周末行程/ }),
    ).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "清除搜索" }));
    expect(document.activeElement).toBe(query);
    expect((query as HTMLInputElement).value).toBe("");
    expect(
      within(dialog).getByRole("button", { name: /杭州周末行程/ }),
    ).toBeDefined();
    fireEvent.click(
      within(dialog).getByRole("button", { name: /京都旅行文案.*文案工作室/ }),
    );
    await screen.findByRole("heading", { name: "京都旅行文案" });
    expect(window.location.hash).toBe("#space=writing&conversation=post");
    expect(screen.queryByRole("button", { name: "打开工作画布" })).toBeNull();
    expect(screen.queryByRole("button", { name: "收起工作画布" })).toBeNull();
    expect(writes).toEqual([]);
  },
);

it("reports a failed task directory and retries from inside search", async () => {
  installNavigationApi();
  const originalFetch = globalThis.fetch;
  let failing = true;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/v1/spaces/writing/conversations" && failing)
      return Promise.resolve(
        Response.json({ detail: "任务目录暂时不可用" }, { status: 503 }),
      );
    return originalFetch(input, init);
  });
  render(<App />);
  await screen.findByRole("button", { name: /^旅行规划/ });
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  expect((await within(dialog).findByRole("alert")).textContent).toContain(
    "任务目录暂时不可用",
  );
  expect(within(dialog).queryByText("没有找到匹配的任务。")).toBeNull();
  failing = false;
  fireEvent.click(within(dialog).getByRole("button", { name: "重新加载任务" }));
  await within(dialog).findByRole("button", { name: /京都旅行文案/ });
  expect(within(dialog).queryByRole("alert")).toBeNull();
});

it("opens the chosen space composer from home and can return without creating data", async () => {
  const { writes } = installNavigationApi();
  render(<App />);
  await screen.findByRole("button", { name: /^旅行规划/ });
  expect(screen.queryByRole("button", { name: "新任务" })).toBeNull();
  await selectTravelSpace();
  const firstComposer = screen.getByRole("textbox", { name: "发送消息" });
  await waitFor(() => expect(document.activeElement).toBe(firstComposer));
  fireEvent.change(firstComposer, { target: { value: "未提交的旅行需求" } });
  fireEvent.click(screen.getByRole("button", { name: "返回工作区" }));
  expect(screen.getByRole("heading", { name: "空间" })).toBeDefined();
  fireEvent.click(await screen.findByRole("button", { name: /^文案工作室/ }));
  const composer = screen.getByRole("textbox", { name: "发送消息" });
  await waitFor(() => expect(document.activeElement).toBe(composer));
  expect((composer as HTMLTextAreaElement).value).toBe("");
  expect(window.location.hash).toBe("#space=writing");
  expect(writes).toEqual([]);
});

it("returns from the global resource library to space settings when opened there", async () => {
  installNavigationApi();
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  fireEvent.click(screen.getByRole("button", { name: "资源库" }));
  expect(screen.getByRole("heading", { name: "资源库" })).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(screen.getByRole("heading", { name: "空间设置" })).toBeDefined();
  expect(window.location.hash).toBe("#space=travel&view=space-settings");
});

it("keeps unsaved space edits when leaving is cancelled and discards them only after confirmation", async () => {
  const { writes } = installNavigationApi();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  fireEvent.change(screen.getByRole("textbox", { name: "默认指令" }), {
    target: { value: "新增但尚未保存的指令" },
  });
  fireEvent.click(screen.getByRole("button", { name: "资源库" }));
  expect(screen.getByRole("heading", { name: "空间设置" })).toBeDefined();
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .value,
  ).toBe("新增但尚未保存的指令");
  expect(confirm).toHaveBeenCalledWith("有未保存的修改，确定离开？");
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(screen.getByRole("heading", { name: "开始新的任务" })).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .value,
  ).toBe("核实来源");
  expect(writes).toEqual([]);
});

it("returns focus to search when an unsaved settings guard cancels opening a task", async () => {
  installNavigationApi();
  vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  fireEvent.change(screen.getByRole("textbox", { name: "默认指令" }), {
    target: { value: "未保存的默认指令" },
  });
  const search = screen.getByRole("button", { name: "搜索任务" });
  fireEvent.click(search);
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  fireEvent.click(
    await within(dialog).findByRole("button", { name: /京都旅行文案/ }),
  );
  expect(screen.queryByRole("dialog", { name: "搜索任务" })).toBeNull();
  expect(screen.getByRole("heading", { name: "空间设置" })).toBeDefined();
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .value,
  ).toBe("未保存的默认指令");
  await waitFor(() => expect(document.activeElement).toBe(search));
});

it("blocks top navigation while saving space settings and keeps a late failure visible for retry", async () => {
  const { spaces } = installNavigationApi();
  const originalFetch = globalThis.fetch;
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  let finishSave: (response: Response) => void = () => {
    throw new Error("Save not started");
  };
  const pending = new Promise<Response>((resolve) => {
    finishSave = resolve;
  });
  let saves = 0;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/v1/spaces/travel" && init?.method === "PATCH") {
      saves++;
      return saves === 1
        ? pending
        : Promise.resolve(
            Response.json({
              ...spaces[0],
              instructions: "保留引用来源",
              revision: 2,
            }),
          );
    }
    return originalFetch(input, init);
  });
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  fireEvent.change(screen.getByRole("textbox", { name: "默认指令" }), {
    target: { value: "保留引用来源" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  for (const destination of ["资源库", "返回工作区"]) {
    fireEvent.click(screen.getByRole("button", { name: destination }));
    expect(screen.getByRole("heading", { name: "空间设置" })).toBeDefined();
    expect(window.location.hash).toBe("#space=travel&view=space-settings");
    expect(screen.getByText("空间配置正在保存，请稍候。")).toBeDefined();
  }
  expect(confirm).not.toHaveBeenCalled();
  await act(async () => {
    finishSave(Response.json({ detail: "配置暂时无法写入" }, { status: 503 }));
  });
  expect(screen.getByRole("heading", { name: "空间设置" })).toBeDefined();
  expect(screen.getByRole("alert").textContent).toBe("配置暂时无法写入");
  expect(screen.queryByText("空间配置正在保存，请稍候。")).toBeNull();
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .value,
  ).toBe("保留引用来源");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("已保存，对后续提交生效");
  expect(saves).toBe(2);
  fireEvent.click(screen.getByRole("button", { name: "资源库" }));
  expect(screen.getByRole("heading", { name: "资源库" })).toBeDefined();
  expect(confirm).not.toHaveBeenCalled();
});

it("shows task summaries on space cards without exposing default instructions", async () => {
  installNavigationApi();
  render(<App />);
  const tile = await screen.findByRole("button", { name: /^旅行规划/ });
  expect(tile.textContent).not.toContain("核实来源");
  expect(tile.textContent).toContain("任务");
});

it("creates a space from a conversation draft with empty default instructions", async () => {
  const { spaces } = installNavigationApi();
  const originalFetch = globalThis.fetch;
  const draft = {
    name: "日常整理",
    instructions: "",
    preferred_tool_codes: [],
    preferred_skill_codes: [],
  };
  let created: unknown;
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/v1/spaces/draft" && init?.method === "POST")
        return Response.json({ reply: "空间名称已整理，可直接创建。", draft });
      if (path === "/v1/spaces" && init?.method === "POST") {
        created = JSON.parse(String(init.body));
        const space: Space = { ...spaces[0], ...draft, space_id: "daily" };
        spaces.push(space);
        return Response.json(space);
      }
      return originalFetch(input, init);
    },
  );
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "创建空间" }));
  const dialog = screen.getByRole("dialog", { name: "创建空间" });
  fireEvent.change(
    within(dialog).getByRole("textbox", { name: "描述空间用途" }),
    { target: { value: "建一个日常整理空间" } },
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "整理空间配置" }));
  const instructions = await within(dialog).findByRole("textbox", {
    name: "默认指令",
  });
  expect((instructions as HTMLTextAreaElement).value).toBe("");
  const create = within(dialog).getByRole("button", { name: "创建并进入" });
  await waitFor(() =>
    expect((create as HTMLButtonElement).disabled).toBe(false),
  );
  fireEvent.click(create);
  await screen.findByRole("heading", { name: "开始新的任务" });
  expect(window.location.hash).toBe("#space=daily");
  expect(created).toEqual(draft);
  expect(screen.getByRole("button", { name: "切换空间" }).textContent).toBe(
    "日常整理",
  );
});

it("keeps keyboard focus in create space and cancels it with Escape without creating data", async () => {
  const { writes } = installNavigationApi();
  render(<App />);
  const opener = await screen.findByRole("button", { name: "创建空间" });
  opener.focus();
  fireEvent.click(opener);
  const dialog = screen.getByRole("dialog", { name: "创建空间" });
  const description = within(dialog).getByRole("textbox", {
    name: "描述空间用途",
  });
  expect(document.activeElement).toBe(description);
  fireEvent.change(description, { target: { value: "不提交的草稿" } });
  const send = within(dialog).getByRole("button", { name: "整理空间配置" });
  const close = within(dialog).getByRole("button", { name: "关闭创建空间" });
  expect(
    (
      within(dialog).getByRole("button", {
        name: "创建并进入",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  send.focus();
  fireEvent.keyDown(send, { key: "Tab" });
  expect(document.activeElement).toBe(close);
  fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(send);
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "创建空间" })).toBeNull();
  expect(document.activeElement).toBe(opener);
  expect(writes).toEqual([]);
});

it("keeps a failed global task directory visible after switching to a space with a healthy history", async () => {
  installNavigationApi();
  const originalFetch = globalThis.fetch;
  let failing = true;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/v1/spaces/writing/conversations" && failing)
      return Promise.resolve(
        Response.json({ detail: "全局任务目录未完整加载" }, { status: 503 }),
      );
    return originalFetch(input, init);
  });
  render(<App />);
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toContain(
      "全局任务目录未完整加载",
    ),
  );
  await selectTravelSpace();
  await screen.findByRole("button", { name: "杭州周末行程" });
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  expect(within(dialog).getByRole("alert").textContent).toContain(
    "全局任务目录未完整加载",
  );
  expect(within(dialog).queryByText("暂无任务。进入空间即可开始。")).toBeNull();
  failing = false;
  fireEvent.click(within(dialog).getByRole("button", { name: "重新加载任务" }));
  await within(dialog).findByRole("button", { name: /京都旅行文案/ });
  expect(within(dialog).queryByRole("alert")).toBeNull();
});

it.each([false, true])(
  "keeps a persisted task visible after returning home even when submission fails (fails=%s)",
  async (submissionFails) => {
    installNavigationApi();
    const originalFetch = globalThis.fetch;
    const task: Conversation = {
      conversation_id: "late-task",
      space_id: "travel",
      title: "新提交的衢州任务",
      created_at: "2099-01-01T00:00:00Z",
      updated_at: "2099-01-01T00:00:00Z",
    };
    let finishCreation: (response: Response) => void = () => {
      throw new Error("Creation not started");
    };
    const creation = new Promise<Response>((resolve) => {
      finishCreation = resolve;
    });
    let created = false;
    vi.stubGlobal(
      "fetch",
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (
          path === "/v1/spaces/travel/conversations" &&
          init?.method === "POST"
        ) {
          const response = await creation;
          created = true;
          return response;
        }
        if (path === "/v1/executions" && init?.method === "POST") {
          if (submissionFails)
            return Response.json({ detail: "模型未接收请求" }, { status: 503 });
          const execution: Execution = {
            execution_id: "late-execution",
            conversation_id: task.conversation_id,
            objective: task.title,
            status: "queued",
            graph_id: null,
            graph_revision: null,
            output: null,
            error: null,
            created_at: task.created_at,
            updated_at: task.updated_at,
          };
          return Response.json({ execution });
        }
        const response = await originalFetch(input, init);
        if (path === "/v1/spaces/travel/conversations" && created) {
          const records: Conversation[] = await response.json();
          return Response.json([task, ...records]);
        }
        return response;
      },
    );
    render(<App />);
    await screen.findByRole("button", { name: /京都旅行文案.*文案工作室/ });
    await selectTravelSpace();
    fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), {
      target: { value: task.title },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
    fireEvent.click(screen.getByRole("button", { name: "返回工作区" }));
    await act(async () => finishCreation(Response.json(task)));
    await screen.findByRole("button", { name: /新提交的衢州任务.*旅行规划/ });
    expect(window.location.hash).toBe("");
    expect(screen.getByRole("heading", { name: "空间" })).toBeDefined();
    expect(screen.queryByRole("textbox", { name: "发送消息" })).toBeNull();
    if (submissionFails)
      expect(screen.getByRole("alert").textContent).toContain(
        "其他空间的消息提交失败：模型未接收请求",
      );
    fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
    const dialog = screen.getByRole("dialog", { name: "搜索任务" });
    expect(
      within(dialog).getByRole("button", {
        name: /新提交的衢州任务.*旅行规划/,
      }),
    ).toBeDefined();
    fireEvent.click(
      within(dialog).getByRole("button", {
        name: /京都旅行文案.*文案工作室/,
      }),
    );
    await screen.findByRole("heading", { name: "京都旅行文案" });
    expect(window.location.hash).toBe("#space=writing&conversation=post");
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "搜索任务" })).getByRole(
        "button",
        {
          name: /新提交的衢州任务.*旅行规划/,
        },
      ),
    );
    await screen.findByRole("heading", { name: "新提交的衢州任务" });
    expect(window.location.hash).toBe("#space=travel&conversation=late-task");
  },
);

function installDeleteDialog() {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
}

it("keeps space drafts after cancellation or a rejected deletion and only leaves after the retry succeeds", async () => {
  installDeleteDialog();
  let finishDelete!: (response: Response) => void;
  const { writes } = installNavigationApi(
    () =>
      new Promise((resolve) => {
        finishDelete = resolve;
      }),
  );
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  fireEvent.change(screen.getByRole("textbox", { name: "默认指令" }), {
    target: { value: "未保存的默认指令" },
  });
  screen.getByRole("button", { name: "删除空间" }).focus();
  fireEvent.click(screen.getByRole("button", { name: "删除空间" }));
  let dialog = screen.getByRole("alertdialog", { name: "删除空间" });
  expect(dialog.textContent).toContain("旅行规划");
  expect(dialog.textContent).toContain("全部任务、对话和执行记录");
  expect(document.activeElement).toBe(
    within(dialog).getByRole("button", { name: "取消" }),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(writes).toEqual([]);
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .value,
  ).toBe("未保存的默认指令");
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "删除空间" }),
  );

  fireEvent.click(screen.getByRole("button", { name: "删除空间" }));
  dialog = screen.getByRole("alertdialog", { name: "删除空间" });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认删除空间" }));
  expect(
    (within(dialog).getByRole("button", { name: "取消" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent(dialog, new Event("cancel", { cancelable: true }));
  fireEvent.click(within(dialog).getByRole("button", { name: "正在删除…" }));
  fireEvent.click(screen.getByRole("button", { name: "返回工作区" }));
  expect(window.location.hash).toBe("#space=travel&view=space-settings");
  expect(writes).toEqual(["/v1/spaces/travel"]);
  await act(async () =>
    finishDelete(
      Response.json(
        { detail: "空间中有正在执行的任务，请先停止任务。" },
        { status: 409 },
      ),
    ),
  );
  expect(within(dialog).getByRole("alert").textContent).toContain(
    "请先停止任务",
  );
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .value,
  ).toBe("未保存的默认指令");
  fireEvent.click(within(dialog).getByRole("button", { name: "确认删除空间" }));
  await act(async () => finishDelete(Response.json({ deleted: true })));
  await waitFor(() => expect(window.location.hash).toBe(""));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(
    screen.queryByRole("button", { name: "旅行规划，进入空间" }),
  ).toBeNull();
  expect(
    await screen.findByRole("button", { name: "文案工作室，进入空间" }),
  ).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const search = screen.getByRole("dialog", { name: "搜索任务" });
  await within(search).findByRole("button", { name: /京都旅行文案/ });
  expect(
    within(search).queryByRole("button", { name: /京都五日旅行|杭州周末行程/ }),
  ).toBeNull();
});

it("deletes the current task and ignores its late history response while retaining other tasks", async () => {
  installDeleteDialog();
  const { writes, conversations } = installNavigationApi(async () =>
    Response.json({ deleted: true }),
  );
  const fetchBase = globalThis.fetch;
  let historyRequests = 0;
  let finishHistory!: (response: Response) => void;
  const oldHistory = conversations.filter((task) => task.space_id === "travel");
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    if (
      String(input) === "/v1/spaces/travel/conversations" &&
      ++historyRequests === 3
    )
      return new Promise<Response>((resolve) => {
        finishHistory = resolve;
      });
    return fetchBase(input, init);
  });
  render(<App />);
  await selectTravelSpace();
  fireEvent.click(await screen.findByRole("button", { name: "京都五日旅行" }));
  await screen.findByRole("heading", { name: "京都五日旅行" });
  await waitFor(() => expect(finishHistory).toBeTypeOf("function"));
  fireEvent.change(screen.getByRole("textbox", { name: "发送消息" }), {
    target: { value: "旧任务草稿" },
  });
  fireEvent.click(screen.getByRole("button", { name: "删除任务" }));
  const dialog = screen.getByRole("alertdialog", { name: "删除任务" });
  expect(dialog.textContent).toContain("京都五日旅行");
  fireEvent.click(within(dialog).getByRole("button", { name: "确认删除任务" }));
  await screen.findByRole("heading", { name: "开始新的任务" });
  await act(async () => finishHistory(Response.json(oldHistory)));
  expect(window.location.hash).toBe("#space=travel");
  expect(
    (screen.getByRole("textbox", { name: "发送消息" }) as HTMLTextAreaElement)
      .value,
  ).toBe("");
  expect(document.activeElement).toBe(
    screen.getByRole("textbox", { name: "发送消息" }),
  );
  expect(screen.queryByRole("button", { name: "京都五日旅行" })).toBeNull();
  expect(
    await screen.findByRole("button", { name: "杭州周末行程" }),
  ).toBeDefined();
  expect(writes).toEqual(["/v1/conversations/kyoto"]);
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const search = screen.getByRole("dialog", { name: "搜索任务" });
  await within(search).findByRole("button", { name: /杭州周末行程/ });
  expect(
    within(search).queryByRole("button", { name: /京都五日旅行/ }),
  ).toBeNull();
});

it("deletes a task from its list without clearing the unsent new task draft", async () => {
  installDeleteDialog();
  const { writes } = installNavigationApi(async () =>
    Response.json({ deleted: true }),
  );
  render(<App />);
  await selectTravelSpace();
  const composer = screen.getByRole("textbox", {
    name: "发送消息",
  }) as HTMLTextAreaElement;
  fireEvent.change(composer, { target: { value: "保留新任务草稿" } });
  fireEvent.click(
    await screen.findByRole("button", { name: "删除任务：京都五日旅行" }),
  );
  let dialog = screen.getByRole("alertdialog", { name: "删除任务" });
  fireEvent(dialog, new Event("cancel", { cancelable: true }));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  expect(writes).toEqual([]);
  expect(composer.value).toBe("保留新任务草稿");
  fireEvent.click(
    screen.getByRole("button", { name: "删除任务：京都五日旅行" }),
  );
  dialog = screen.getByRole("alertdialog", { name: "删除任务" });
  fireEvent.click(within(dialog).getByRole("button", { name: "确认删除任务" }));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(composer.value).toBe("保留新任务草稿");
  expect(window.location.hash).toBe("#space=travel");
  expect(screen.queryByRole("button", { name: "京都五日旅行" })).toBeNull();
  expect(
    await screen.findByRole("button", { name: "杭州周末行程" }),
  ).toBeDefined();
  expect(writes).toEqual(["/v1/conversations/kyoto"]);
});
