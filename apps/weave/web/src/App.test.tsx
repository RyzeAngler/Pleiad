// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { graphPositions } from "./projection";
import type {
  Attachment,
  Attention,
  ExecutionDetail,
  Graph,
  Message,
  Page,
  Snapshot,
  Space,
} from "./types";

class TestEventSource extends EventTarget {
  static instances: TestEventSource[] = [];
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  sequence = 1;
  constructor(public readonly url: string) {
    super();
    TestEventSource.instances.push(this);
    this.sequence = Number(
      new URL(url, "http://localhost").searchParams.get("after_sequence"),
    );
    queueMicrotask(() => {
      if (!this.closed) this.onopen?.(new Event("open"));
    });
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data?: unknown) {
    this.dispatchEvent(
      new MessageEvent(type, {
        data: JSON.stringify(
          data ?? {
            sequence: ++this.sequence,
            type: "space.updated",
            data: {},
            event_id: "event",
            subject: "space",
            created_at: "2026-11-07T00:00:00Z",
          },
        ),
      }),
    );
  }
}
function page<T>(items: T[], sequence = 1): Page<T> {
  return {
    items,
    total: items.length,
    next_cursor: null,
    read_sequence: sequence,
  };
}
function stepPage<T extends { run_sequence: number }>(items: T[]) {
  const ordered = [...items].sort((a, b) => b.run_sequence - a.run_sequence);
  return {
    items: ordered,
    total: items.length,
    next_before_sequence: null,
    latest_sequence: ordered[0]?.run_sequence ?? null,
    read_sequence: 1,
  };
}
function runPage<T extends { attempt: number; run_id: string | null }>(
  items: T[],
) {
  const ordered = [...items].sort((a, b) => b.attempt - a.attempt);
  return {
    items: ordered,
    total: items.length,
    next_before_sequence: null,
    latest_sequence: ordered[0]?.attempt ?? null,
    latest_run_id: ordered[0]?.run_id ?? null,
    read_sequence: 1,
  };
}
const space: Space = {
  space_id: "research",
  revision: 1,
  status: "active",
  name: "研究空间",
  purpose: "调查资料并核对来源",
  instructions: "引用真实证据",
  model_role: "text",
  tool_codes: [],
  skill_codes: [],
  resource_bindings: [],
  deletion_pending: false,
  deleted: false,
  created_at: "2026-11-07T00:00:00Z",
  active_execution_count: 1,
  pending_attention_count: 0,
  available_actions: ["update", "archive", "delete"],
  disabled_reasons: {},
};
const execution: ExecutionDetail = {
  execution_id: "work",
  space_id: space.space_id,
  parent_execution_id: null,
  source_execution_id: null,
  objective: "研究 Agent 架构",
  delivery_contract: {
    require_text: true,
    required_artifact_ids: [],
    required_verifications: [],
    required_children: [],
  },
  session_id: "sdk-session",
  revision: 1,
  status: "running",
  control_intent: "none",
  reason: null,
  config_snapshot: space,
  input_watermark: 1,
  budget_limit: 1000,
  budget_consumed: 100,
  budget_reserved: 0,
  result: null,
  unknown_effects: [],
  checkpoint_ref: null,
  current_run_id: "sdk-run",
  run_attempt: 1,
  created_at: "2026-11-07T00:00:00Z",
  pause_requested: false,
  pending_prerequisites: [],
  available_actions: ["pause", "cancel", "supplement"],
  disabled_reasons: {},
  read_sequence: 1,
  session_read_sequence: 0,
  inputs: [],
  deliveries: [],
  dispositions: [],
  handbacks: [],
  handback_resolutions: [],
  reports: [],
  children: [],
  artifacts: [],
  attentions: [],
  runs: [],
};
const attention: Attention = {
  attention_id: "question",
  execution_objective: execution.objective,
  space_name: space.name,
  execution_id: "work",
  kind: "clarification",
  question: "需要调查哪一个地区？",
  options: [],
  action_digest: null,
  action: null,
  checkpoint_ref: null,
  revision: 1,
  status: "pending",
  expires_at: null,
  resolution: null,
  available_actions: ["resolve"],
  disabled_reasons: {},
};
const attachment: Attachment = {
  attachment_id: "attachment",
  client_attachment_id: "upload",
  name: "notes.txt",
  mime: "text/plain",
  size: 5,
  digest: "sha",
  status: "ready",
  expires_at: "2099-01-01T00:00:00Z",
};
const message = (id: string, sequence: number, body: string): Message => ({
  message_id: id,
  chat_sequence: sequence,
  role: "assistant",
  body,
  source_key: id,
  created_at: "2026-11-07T00:00:00Z",
  attachment_ids: [],
  target_execution_ids: [],
  context_refs: [],
  reply_to_message_id: null,
  report_ids: [],
  reports: [],
  artifacts: [],
  input_dispositions: [],
  handbacks: [],
  handback_resolutions: [],
});
const initialSnapshot = (): Snapshot => ({
  snapshot_sequence: 1,
  routing: { state: "idle", pending_input_count: 0 },
  messages: page([message("hello", 1, "工作正在独立推进。")]),
  spaces: page([space]),
  executions: page([execution]),
  attentions: page([]),
  capabilities: {
    csrf_token: "host-csrf",
    max_message_bytes: 65536,
    max_attachment_bytes: 25 * 1024 * 1024,
    max_message_attachment_bytes: 100 * 1024 * 1024,
    max_attachments: 20,
    max_context_refs: 20,
    max_target_executions: 20,
  },
});
interface Submission {
  path: string;
  body: Record<string, unknown>;
  headers: Headers;
}
function setup(
  initial = initialSnapshot(),
  override?: (
    path: string,
    options: RequestInit,
  ) => Promise<Response> | Response | undefined,
) {
  const state = { snapshot: initial, submissions: [] as Submission[] };
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, options: RequestInit = {}) => {
      const path = new URL(String(input), "http://localhost").pathname;
      const custom = override?.(String(input), options);
      if (custom) return Promise.resolve(custom);
      if (options.method && options.method !== "GET") {
        const body =
          typeof options.body === "string" ? JSON.parse(options.body) : {};
        state.submissions.push({
          path,
          body,
          headers: new Headers(options.headers),
        });
        if (path === "/v1/chat/messages")
          return Promise.resolve(
            Response.json({
              message_id: "accepted",
              sequence: 2,
              accepted: true,
            }),
          );
        if (path.endsWith("/resolve"))
          return Promise.resolve(
            Response.json({
              message_id: "answer-message",
              attention_revision: 2,
              execution,
            }),
          );
        if (path === "/v1/attachments")
          return Promise.resolve(Response.json(attachment));
        if (path.endsWith("/pause"))
          return Promise.resolve(
            Response.json({ ...execution, status: "paused", revision: 2 }),
          );
        if (path.endsWith("/cancel"))
          return Promise.resolve(
            Response.json({ ...execution, status: "cancelling", revision: 2 }),
          );
        if (path.endsWith("/archive"))
          return Promise.resolve(
            Response.json({ ...space, status: "archived", revision: 2 }),
          );
      }
      if (path === "/v1/snapshot")
        return Promise.resolve(Response.json(state.snapshot));
      if (path === "/v1/spaces")
        return Promise.resolve(
          Response.json(page(state.snapshot.spaces.items)),
        );
      if (path === "/v1/spaces/research")
        return Promise.resolve(Response.json(space));
      if (path === "/v1/executions")
        return Promise.resolve(Response.json(page([execution])));
      if (path === "/v1/executions/work")
        return Promise.resolve(Response.json(execution));
      if (path === "/v1/attentions/question")
        return Promise.resolve(Response.json(attention));
      if (path === "/v1/attachments/attachment")
        return Promise.resolve(Response.json(attachment));
      if (path === "/v1/resources")
        return Promise.resolve(Response.json(page([])));
      if (path === "/healthz")
        return Promise.resolve(Response.json({ status: "ok" }));
      if (path === "/readyz")
        return Promise.resolve(Response.json({ ready: true }));
      if (path === "/v1/settings")
        return Promise.resolve(
          Response.json({
            connection: "ready",
            configuration_location: ".env",
          }),
        );
      return Promise.resolve(
        Response.json({ message: "Unknown test endpoint" }, { status: 404 }),
      );
    }),
  );
  return state;
}
async function ready() {
  await waitFor(() => expect(screen.getByText("持续协作")).toBeTruthy());
}
function composer() {
  return screen.getByRole("textbox", {
    name: "给 Weave 的消息",
  }) as HTMLTextAreaElement;
}
function openRoute(hash: string) {
  act(() => {
    window.location.hash = hash;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}
beforeEach(() => {
  window.location.hash = "#view=spaces";
  localStorage.clear();
  TestEventSource.instances = [];
  vi.stubGlobal("EventSource", TestEventSource);
  HTMLElement.prototype.scrollTo = vi.fn();
  HTMLElement.prototype.scrollIntoView = vi.fn();
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("keeps one mounted composer and its unsent draft while right-side navigation changes", async () => {
  setup();
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.change(input, { target: { value: "继续调查这个目标" } });
  fireEvent.click(screen.getByRole("button", { name: "资源" }));
  await screen.findByText("资源目录");
  expect(composer()).toBe(input);
  expect(input.value).toBe("继续调查这个目标");
  fireEvent.click(screen.getByRole("button", { name: "返回来源" }));
  await screen.findByRole("heading", { name: "我的空间" });
  expect(composer()).toBe(input);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  await screen.findByText("显示偏好");
  expect(composer()).toBe(input);
  expect(
    document.querySelectorAll('form[aria-label="主聊天输入"] textarea'),
  ).toHaveLength(1);
  expect(screen.queryByText("新建对话")).toBeNull();
});

it("submits an attention answer exactly once through resolve with no ordinary-message duplicate", async () => {
  const snapshot = initialSnapshot();
  snapshot.attentions = page([attention]);
  const state = setup(snapshot);
  render(<App />);
  await ready();
  fireEvent.change(composer(), { target: { value: "华北地区" } });
  fireEvent.click(screen.getByRole("button", { name: "在主聊天回答" }));
  const input = await screen.findByRole("textbox", { name: "回答事项" });
  expect((input as HTMLTextAreaElement).value).toBe("华北地区");
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "提交回答" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
  await waitFor(() => expect(state.submissions).toHaveLength(1));
  expect(state.submissions[0].path).toBe("/v1/attentions/question/resolve");
  expect(state.submissions[0].body.answer).toBe("华北地区");
  expect(state.submissions[0].headers.get("X-Weave-CSRF")).toBe("host-csrf");
  expect(
    state.submissions.some((item) => item.path === "/v1/chat/messages"),
  ).toBe(false);
});

it("switches an answer to a readonly space reference while retaining its text", async () => {
  const snapshot = initialSnapshot();
  snapshot.attentions = page([attention]);
  const state = setup(snapshot);
  render(<App />);
  await ready();
  fireEvent.change(composer(), { target: { value: "结合现有资料继续研究" } });
  fireEvent.click(screen.getByRole("button", { name: "在主聊天回答" }));
  fireEvent.click(screen.getByRole("button", { name: "使用此空间" }));
  expect(composer().value).toBe("结合现有资料继续研究");
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => expect(state.submissions).toHaveLength(1));
  expect(state.submissions[0].path).toBe("/v1/chat/messages");
  expect(state.submissions[0].body.target_execution_ids).toEqual([]);
  expect(state.submissions[0].body.context_refs).toEqual([
    { kind: "space", id: "research" },
  ]);
});

it("preserves the draft after a failed send and reuses the original idempotency key", async () => {
  const attempts: Record<string, unknown>[] = [];
  setup(initialSnapshot(), (path, options) => {
    if (path === "/v1/chat/messages" && options.method === "POST") {
      const payload = JSON.parse(String(options.body));
      attempts.push(payload);
      return attempts.length === 1
        ? Promise.reject(new TypeError("connection lost"))
        : Response.json({
            message_id: "accepted",
            sequence: 2,
            accepted: true,
          });
    }
  });
  render(<App />);
  await ready();
  fireEvent.change(composer(), { target: { value: "保存这条原始请求" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText(/无法连接 Weave 服务/);
  expect(composer().value).toBe("保存这条原始请求");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => expect(attempts).toHaveLength(2));
  expect(attempts[0].client_message_id).toBe(attempts[1].client_message_id);
});

it("does not submit Enter while a Chinese input method is composing", async () => {
  const state = setup();
  render(<App />);
  await ready();
  fireEvent.change(composer(), { target: { value: "中文输入中" } });
  fireEvent.keyDown(composer(), { key: "Enter", isComposing: true });
  expect(state.submissions).toHaveLength(0);
  expect(composer().value).toBe("中文输入中");
});

it("uploads a file through multipart and sends only the validated attachment reference", async () => {
  const state = setup();
  render(<App />);
  await ready();
  const upload = document.querySelector('input[type="file"]')!;
  fireEvent.change(upload, {
    target: {
      files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
    },
  });
  await screen.findByText("notes.txt");
  fireEvent.change(composer(), { target: { value: "根据附件调查" } });
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() =>
    expect(
      state.submissions.some((item) => item.path === "/v1/chat/messages"),
    ).toBe(true),
  );
  expect(
    state.submissions.find((item) => item.path === "/v1/chat/messages")!.body
      .attachment_ids,
  ).toEqual(["attachment"]);
  const fetchMock = vi.mocked(fetch);
  const call = fetchMock.mock.calls.find(
    ([path]) => String(path) === "/v1/attachments",
  )!;
  expect((call[1]!.body as FormData).get("file")).toBeInstanceOf(File);
});

it("blocks stale writes when SSE disconnects while keeping the draft editable", async () => {
  setup();
  render(<App />);
  await ready();
  fireEvent.change(composer(), { target: { value: "保留这份草稿" } });
  act(() => TestEventSource.instances.at(-1)!.onerror?.(new Event("error")));
  expect(
    (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(composer().value).toBe("保留这份草稿");
  fireEvent.change(composer(), { target: { value: "离线时继续编辑" } });
  expect(composer().value).toBe("离线时继续编辑");
});

it("discards an old chat page that arrives after resync establishes a new cache generation", async () => {
  let resolvePage: ((response: Response) => void) | undefined;
  const deferred = new Promise<Response>((resolve) => {
    resolvePage = resolve;
  });
  const snapshot = initialSnapshot();
  snapshot.messages = {
    ...page([message("recent", 100, "当前消息")]),
    total: 100,
    next_cursor: 100,
  };
  const state = setup(snapshot, (path) =>
    path.startsWith("/v1/chat/messages?") ? deferred : undefined,
  );
  render(<App />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "读取更早的消息" }));
  state.snapshot = {
    ...initialSnapshot(),
    snapshot_sequence: 5,
    messages: page([message("new", 101, "同步后的消息")], 5),
  };
  act(() => TestEventSource.instances.at(-1)!.emit("resync"));
  await screen.findByText("同步后的消息");
  await act(async () => {
    resolvePage!(Response.json(page([message("late", 50, "旧代次晚到页面")])));
    await deferred;
  });
  expect(screen.queryByText("旧代次晚到页面")).toBeNull();
});

it("keeps the current selected job invalid after its detail read fails", async () => {
  window.location.hash = "view=execution&execution_id=missing";
  setup(initialSnapshot(), (path) =>
    path === "/v1/executions/missing"
      ? Response.json({ message: "这项工作已删除" }, { status: 404 })
      : undefined,
  );
  render(<App />);
  await ready();
  await screen.findByText("这项工作已删除");
  expect(screen.queryByRole("button", { name: "取消" })).toBeNull();
  expect(composer()).toBeTruthy();
});

it("does not let a late previous execution detail replace the selected job", async () => {
  let resolveOld: ((response: Response) => void) | undefined;
  const old = new Promise<Response>((resolve) => {
    resolveOld = resolve;
  });
  window.location.hash = "view=execution&execution_id=old";
  setup(initialSnapshot(), (path) =>
    path === "/v1/executions/old" ? old : undefined,
  );
  render(<App />);
  await ready();
  openRoute("view=execution&execution_id=work");
  await screen.findByRole("heading", { name: execution.objective });
  await act(async () => {
    resolveOld!(
      Response.json({
        ...execution,
        execution_id: "old",
        objective: "旧选择的工作",
      }),
    );
    await old;
  });
  expect(screen.queryByRole("heading", { name: "旧选择的工作" })).toBeNull();
  expect(
    screen.getByRole("heading", { name: execution.objective }),
  ).toBeTruthy();
});

it("submits explicit approval with its digest and revision without using the composer", async () => {
  const approval: Attention = {
    ...attention,
    attention_id: "question",
    kind: "approval",
    action_digest: "fixed-action",
    action: {
      tool_code: "write_file",
      tool_name: "write_file",
      arguments: { path: "approved.txt", content: "Approved content" },
    },
    checkpoint_ref: "checkpoint",
    question: "批准这一次写入？",
  };
  const snapshot = initialSnapshot();
  snapshot.attentions = page([approval]);
  const state = setup(snapshot);
  render(<App />);
  await ready();
  expect(screen.getByText(/"path": "approved.txt"/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "批准此操作" }));
  await waitFor(() => expect(state.submissions).toHaveLength(1));
  expect(state.submissions[0].body).toMatchObject({
    decision: "approve",
    expected_revision: 1,
    action_digest: "fixed-action",
  });
  expect(state.submissions[0].body.answer).toBeUndefined();
});

describe("actual graph dependency layout", () => {
  const graph: Graph = {
    graph_id: "real",
    revision: 2,
    objective: "goal",
    nodes: [
      {
        node_id: "one",
        objective: "Collect",
        prompt: "",
        tool_codes: [],
        skill_codes: [],
      },
      {
        node_id: "two",
        objective: "Review",
        prompt: "",
        tool_codes: [],
        skill_codes: [],
      },
    ],
    edges: [{ source_node_id: "one", target_node_id: "two" }],
  };
  it("positions a dependency before its actual successor", () => {
    const positions = graphPositions(graph);
    expect(positions.get("one")!.x).toBeLessThan(positions.get("two")!.x);
  });
  it("rejects a fabricated edge whose node does not exist", () => {
    expect(() =>
      graphPositions({
        ...graph,
        edges: [{ source_node_id: "one", target_node_id: "missing" }],
      }),
    ).toThrow("不存在");
  });
  it("rejects a cyclic graph instead of drawing misleading progress", () => {
    expect(() =>
      graphPositions({
        ...graph,
        edges: [
          ...graph.edges,
          { source_node_id: "two", target_node_id: "one" },
        ],
      }),
    ).toThrow("循环");
  });
});

it("retries a failed dropped attachment with its original upload identity", async () => {
  const identities: FormDataEntryValue[] = [];
  setup(initialSnapshot(), (path, options) => {
    if (path === "/v1/attachments" && options.method === "POST") {
      identities.push((options.body as FormData).get("client_attachment_id")!);
      if (identities.length === 1)
        return Promise.reject(new TypeError("Connection lost"));
      return Response.json(attachment);
    }
  });
  render(<App />);
  await ready();
  fireEvent.change(composer(), { target: { value: "Use this file" } });
  fireEvent.drop(screen.getByRole("form", { name: "主聊天输入" }), {
    dataTransfer: {
      files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
    },
  });
  const retry = await screen.findByRole("button", {
    name: "重试上传 notes.txt",
  });
  expect(
    (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(retry);
  await screen.findByRole("button", { name: "移除附件 notes.txt" });
  expect(identities).toHaveLength(2);
  expect(identities[0]).toBe(identities[1]);
  expect(composer().value).toBe("Use this file");
});

it("exits supplement mode without sending or cancelling work and preserves the draft", async () => {
  const state = setup();
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work");
  await screen.findByRole("heading", { name: execution.objective });
  fireEvent.change(composer(), { target: { value: "Still editing" } });
  fireEvent.click(screen.getByRole("button", { name: "补充这项工作" }));
  const cancel = await screen.findByRole("button", {
    name: `取消补充：${execution.objective}`,
  });
  fireEvent.click(cancel);
  expect(state.submissions).toEqual([]);
  expect(screen.queryByRole("button", { name: /取消此任务/ })).toBeNull();
  expect(
    screen.queryByRole("button", { name: `取消补充：${execution.objective}` }),
  ).toBeNull();
  expect(composer().value).toBe("Still editing");
});

it("keeps a long supplement target separate from editable message text until explicit send", async () => {
  const long = {
    ...execution,
    objective: "调研最新模型并逐一核对来源。".repeat(40),
  };
  const state = setup(initialSnapshot(), (path) =>
    path === "/v1/executions/work" ? Response.json(long) : undefined,
  );
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work");
  await screen.findByRole("heading", { name: "调研最新模型并逐一核对来源" });
  fireEvent.change(composer(), {
    target: { value: "My editable requirement" },
  });
  fireEvent.click(screen.getByRole("button", { name: "补充这项工作" }));
  expect(composer().value).toBe("My editable requirement");
  expect(state.submissions).toEqual([]);
  const cancel = await screen.findByRole("button", {
    name: `取消补充：${long.objective}`,
  });
  fireEvent.change(composer(), { target: { value: "" } });
  expect(cancel.isConnected).toBe(true);
  expect(state.submissions).toEqual([]);
  fireEvent.click(cancel);
  fireEvent.change(composer(), { target: { value: "Ordinary chat" } });
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => expect(state.submissions).toHaveLength(1));
  expect(state.submissions[0].path).toBe("/v1/chat/messages");
  expect(state.submissions[0].body.target_execution_ids).toEqual([]);
  expect(state.submissions[0].body.text).toBe("Ordinary chat");
});

it("keeps newest-first step pages stable during live updates until latest progress is requested", async () => {
  let newest = 52;
  const reads: number[] = [];
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/executions/work/graph-runs")
      return Response.json(page([]));
    if (url.pathname === "/v1/executions/work/runs")
      return Response.json(
        runPage([
          {
            attempt: 1,
            run_id: "sdk-run",
            invocation_id: "invocation",
            state: "running",
          },
        ]),
      );
    if (url.pathname === "/v1/executions/work/runs/sdk-run") {
      const before =
        Number(url.searchParams.get("before_sequence")) || newest + 1;
      reads.push(before);
      const end = Math.min(newest, before - 1);
      const items = Array.from({ length: Math.min(50, end) }, (_, index) => {
        const sequence = end - index;
        return {
          step_id: `step-${sequence}`,
          run_id: "sdk-run",
          sequence,
          run_sequence: sequence,
          agent_id: "worker",
          input_ids: [],
          state: "completed",
          messages: [
            {
              message_id: `reply-${sequence}`,
              role: "assistant",
              content: `Result ${sequence}`,
            },
          ],
        };
      });
      return Response.json({
        run: { state: "running" },
        steps: {
          ...stepPage(items),
          total: newest,
          latest_sequence: newest,
          next_before_sequence: end > 50 ? end - 49 : null,
        },
        operations: [],
        tool_results: [],
      });
    }
  });
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work&tab=process");
  await screen.findByText("Result 52");
  const steps = () => [
    ...document.querySelectorAll<HTMLDetailsElement>(".timeline-step"),
  ];
  expect(steps().map((step) => step.dataset.stepId)).toEqual(
    Array.from({ length: 50 }, (_, i) => `step-${52 - i}`),
  );
  expect(steps()[0].open).toBe(true);
  fireEvent.click(steps()[1].querySelector("summary")!);
  const reading = steps()[1];
  newest = 53;
  act(() => TestEventSource.instances[0].emit("product"));
  await screen.findByRole("button", { name: "查看最新进展" });
  expect(steps()[0].dataset.stepId).toBe("step-52");
  expect(reading.open).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "查看更早步骤" }));
  await screen.findByText("Result 2");
  expect(steps().map((step) => step.dataset.stepId)).toEqual([
    "step-2",
    "step-1",
  ]);
  expect(reads).toContain(3);
  fireEvent.click(screen.getByRole("button", { name: "查看最新进展" }));
  await screen.findByText("Result 53");
  expect(steps()[0].dataset.stepId).toBe("step-53");
  expect(steps()[0].open).toBe(true);
});

it("reads the next resource page without retaining members from the earlier page", async () => {
  setup(initialSnapshot(), (path) => {
    if (path.startsWith("/v1/resources?")) {
      const cursor = new URL(path, "http://localhost").searchParams.get(
        "cursor",
      );
      return Response.json({
        items: [
          {
            kind: "tools",
            code: cursor === "50" ? "last" : "first",
            name: cursor === "50" ? "最后一项能力" : "第一项能力",
            description: "Host metadata",
            available: true,
          },
        ],
        total: 51,
        next_cursor: cursor === "50" ? null : 50,
        read_sequence: 1,
      });
    }
  });
  render(<App />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "资源" }));
  await screen.findByText("第一项能力");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByText("最后一项能力");
  expect(screen.queryByText("第一项能力")).toBeNull();
  fireEvent.keyDown(screen.getByRole("tab", { name: "工具" }), {
    key: "ArrowRight",
  });
  await waitFor(() =>
    expect(
      screen.getByRole("tab", { name: "技能" }).getAttribute("aria-selected"),
    ).toBe("true"),
  );
});

it("translates system control receipts without rewriting user or assistant messages", async () => {
  const copy = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: copy } });
  const snapshot = initialSnapshot();
  snapshot.messages = page([
    {
      ...message("pause", 1, "pause requested"),
      role: "system",
      source_key: "control/pause",
    },
    {
      ...message("original-user", 2, "pause requested"),
      role: "user",
      source_key: "client/message",
    },
    message("original-assistant", 3, "pause requested"),
  ]);
  setup(snapshot);
  render(<App />);
  await ready();
  expect(await screen.findByText("已请求暂停")).toBeTruthy();
  expect(screen.getAllByText("pause requested")).toHaveLength(2);
  fireEvent.click(screen.getByRole("button", { name: "复制消息 1" }));
  await waitFor(() => expect(copy).toHaveBeenCalledWith("已请求暂停"));
});

it.each([
  ["models", "模型"],
  ["tools", "工具"],
  ["skills", "技能"],
  ["environments", "环境"],
])(
  "opens complete %s resource information and copies the masked source",
  async (kind, label) => {
    const copy = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: { writeText: copy },
    });
    const resource = {
      kind,
      code: "text",
      name: "Configured resource",
      description: "Original resource description",
      available: true,
      version: "1",
      disabled_reason: null,
      configuration_location: ".env",
      metadata: { license: "MIT", compatibility: "Python 3.12" },
      configuration: {
        provider: "example",
        api_key: "********",
        headers: { Authorization: "********" },
      },
      input_schema: {
        type: "object",
        properties: {
          api_key: { type: "string", description: "Credential parameter" },
        },
      },
      output_schema: {
        type: "object",
        properties: { result: { type: "string" } },
      },
      content:
        kind === "skills" ? "# Complete skill\nFull instructions." : null,
      files: kind === "skills" ? ["SKILL.md", "references/guide.md"] : [],
    };
    setup(initialSnapshot(), (path) => {
      const url = new URL(path, "http://localhost");
      if (url.pathname === "/v1/resources")
        return Response.json(page([resource]));
      if (url.pathname === `/v1/resources/${kind}/text`)
        return Response.json(resource);
    });
    render(<App />);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "资源" }));
    fireEvent.click(await screen.findByRole("tab", { name: label }));
    fireEvent.click(
      await screen.findByRole("button", { name: /Configured resource/ }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Configured resource",
    });
    await within(dialog).findByText("MIT");
    expect(within(dialog).getByText("Python 3.12")).toBeTruthy();
    expect(within(dialog).getByText("example")).toBeTruthy();
    expect(within(dialog).getByText("输入参数")).toBeTruthy();
    expect(within(dialog).getByText("返回内容")).toBeTruthy();
    if (kind === "skills") {
      expect(
        within(dialog).getByRole("heading", { name: "Complete skill" }),
      ).toBeTruthy();
      expect(within(dialog).getByText("references/guide.md")).toBeTruthy();
    }
    fireEvent.click(
      within(dialog).getByRole("button", { name: "复制原始信息" }),
    );
    await waitFor(() =>
      expect(copy).toHaveBeenCalledWith(JSON.stringify(resource, null, 2)),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭弹窗" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  },
);

it("keeps unsaved Space settings until the user explicitly discards them", async () => {
  setup();
  render(<App />);
  await ready();
  openRoute("#view=space&space_id=research&tab=settings");
  const name = await screen.findByRole("textbox", { name: "名称" });
  fireEvent.change(name, { target: { value: "未保存名称" } });
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  await screen.findByRole("dialog", { name: "空间配置尚未保存" });
  fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
  expect(
    (screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value,
  ).toBe("未保存名称");
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "放弃修改并离开" }),
  );
  await screen.findByRole("heading", { name: "设置" });
  openRoute("#view=space&space_id=research&tab=settings");
  await waitFor(() =>
    expect(
      (screen.getByRole("textbox", { name: "名称" }) as HTMLInputElement).value,
    ).toBe(space.name),
  );
});

it("retries a failed snapshot automatically before re-enabling writes", async () => {
  let attempts = 0;
  setup(initialSnapshot(), (path) => {
    if (path === "/v1/snapshot" && ++attempts === 1)
      return Promise.reject(new TypeError("offline"));
  });
  vi.useFakeTimers();
  try {
    render(<App />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(
      (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    expect(attempts).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(attempts).toBe(2);
    expect(screen.getByText("持续协作")).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

it("opens the real artifact referenced by a formal Worker report without replacing the composer", async () => {
  const snapshot = initialSnapshot();
  const report = message("report-message", 2, "已完成核验，请查看报告。");
  report.report_ids = ["ready-report"];
  report.reports = [
    {
      report_id: "ready-report",
      execution_id: "work",
      kind: "ready",
      summary: "证据已核验",
      artifact_ids: ["report-artifact"],
    },
  ];
  report.artifacts = [
    {
      artifact_id: "report-artifact",
      created_at: "2026-11-07T00:00:00Z",
      name: "核验报告.md",
      mime: "text/markdown",
      size: 17,
      digest: "digest",
      owner_execution_id: "work",
      graph_run_id: null,
      node_attempt_id: null,
      previewable: true,
      downloadable: true,
      deletion_state: "active",
    },
  ];
  snapshot.messages = page([report]);
  setup(snapshot, (path) => {
    if (path === "/v1/artifacts/report-artifact")
      return Response.json(report.artifacts[0]);
    if (path.startsWith("/v1/artifacts/report-artifact/text"))
      return Response.json({
        id: "report-artifact",
        text: "# 实际报告\n已核验。",
        offset: 0,
        next_offset: null,
        total_bytes: 32,
        read_sequence: 1,
        read_scope: "product",
      });
  });
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.change(input, { target: { value: "保留后续问题" } });
  fireEvent.click(screen.getByRole("button", { name: /核验报告.md/ }));
  const heading = await screen.findByRole("heading", { name: "核验报告.md" });
  await waitFor(() => expect(document.activeElement).toBe(heading));
  await screen.findByRole("heading", { name: "实际报告" });
  expect(composer()).toBe(input);
  expect(input.value).toBe("保留后续问题");
  expect(window.location.hash).toContain("artifact_id=report-artifact");
});

it("reconnects after an event-triggered snapshot refresh fails while the stream remains open", async () => {
  let attempts = 0;
  setup(initialSnapshot(), (path) => {
    if (path === "/v1/snapshot" && ++attempts === 2)
      return Promise.reject(new TypeError("offline refresh"));
  });
  render(<App />);
  await ready();
  vi.useFakeTimers();
  try {
    act(() => TestEventSource.instances[0].emit("product"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(attempts).toBe(2);
    expect(screen.queryByText("持续协作")).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1500);
    });
    expect(attempts).toBe(3);
    expect(screen.getByText("持续协作")).toBeTruthy();
    expect(TestEventSource.instances[0].closed).toBe(true);
  } finally {
    vi.useRealTimers();
  }
});

it("refreshes committed facts during continuous events without waiting for the burst to end", async () => {
  let reads = 0;
  setup(initialSnapshot(), (path) => {
    if (path === "/v1/snapshot") reads += 1;
    return undefined;
  });
  render(<App />);
  await ready();
  vi.useFakeTimers();
  try {
    const source = TestEventSource.instances[0];
    for (let index = 0; index < 8; index++) {
      act(() => source.emit("product"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(25);
      });
    }
    expect(reads).toBeGreaterThanOrEqual(3);
  } finally {
    vi.useRealTimers();
  }
});

it("keeps all committed SSE messages when a burst exceeds the snapshot's fifty-message window", async () => {
  const state = setup();
  render(<App />);
  await ready();
  const source = TestEventSource.instances[0];
  const burst = Array.from({ length: 100 }, (_, index) =>
    message(`burst-${index}`, index + 2, `已提交突发消息 ${index}`),
  );
  act(() => {
    for (const [index, value] of burst.entries())
      source.emit("product", {
        sequence: index + 2,
        event_id: `event-${index}`,
        subject: value.message_id,
        type: "chat.message.created",
        data: value,
        created_at: value.created_at,
      });
  });
  expect(screen.getByText("已提交突发消息 0")).toBeTruthy();
  expect(screen.getByText("已提交突发消息 99")).toBeTruthy();
  state.snapshot = {
    ...initialSnapshot(),
    snapshot_sequence: 101,
    messages: {
      items: burst.slice(-50),
      total: 101,
      next_cursor: 52,
      read_sequence: 101,
    },
  };
  await waitFor(() =>
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([path]) => String(path) === "/v1/snapshot").length,
    ).toBeGreaterThanOrEqual(2),
  );
  expect(document.querySelectorAll(".message")).toHaveLength(101);
  act(() =>
    source.emit("product", {
      sequence: 101,
      event_id: "duplicate",
      subject: burst[99].message_id,
      type: "chat.message.created",
      data: burst[99],
      created_at: burst[99].created_at,
    }),
  );
  expect(document.querySelectorAll(".message")).toHaveLength(101);
});

it("opens the actual owning work when two pending questions have identical wording", async () => {
  const snapshot = initialSnapshot();
  snapshot.attentions = page([
    attention,
    {
      ...attention,
      attention_id: "other-question",
      execution_id: "other-work",
      execution_objective: "第二项工作的真实目标",
    },
  ]);
  setup(snapshot, (path) =>
    path === "/v1/executions/other-work"
      ? Response.json({
          ...execution,
          execution_id: "other-work",
          objective: "第二项工作的真实目标",
        })
      : undefined,
  );
  render(<App />);
  await ready();
  expect(screen.getAllByText(attention.question)).toHaveLength(2);
  fireEvent.click(
    screen.getByRole("button", {
      name: `${space.name} · 第二项工作的真实目标`,
    }),
  );
  await screen.findByRole("heading", { name: "第二项工作的真实目标" });
  expect(window.location.hash).toContain("execution_id=other-work");
});

it("retries the original failed delivery with one generation-bound identity while keeping SDK and responsibility facts separate", async () => {
  let current: ExecutionDetail = {
    ...execution,
    inputs: [
      {
        input_id: "original-input",
        origin_input_id: "origin",
        target_sequence: 1,
        source: "weave.message",
        content: "Inspect the original request",
        causation_id: null,
        status: "claimed",
        sdk_consumption: {
          sequence: 7,
          accepted_at: "2026-11-07T00:00:00Z",
          state: "consumed",
          run_id: "actual-run",
          step_id: "actual-step",
        },
      },
    ],
    dispositions: [
      {
        input_id: "original-input",
        origin_input_id: "origin",
        target_execution_id: "work",
        revision: 1,
        state: "owned",
        outcome_refs: [],
        handback_id: null,
      },
    ],
    deliveries: [
      {
        delivery_id: "original-delivery",
        input_id: "original-input",
        target_kind: "execution",
        target_id: "work",
        state: "failed",
        generation: 3,
        attempts: 2,
        sdk_sequence: null,
        last_error: "Receipt confirmation interrupted",
        available_actions: ["retry"],
        disabled_reasons: {},
      },
    ],
  };
  const requests: Record<string, unknown>[] = [];
  setup(initialSnapshot(), (path, options) => {
    if (path === "/v1/executions/work") return Response.json(current);
    if (path === "/v1/deliveries/original-delivery/retry") {
      requests.push(JSON.parse(String(options.body)));
      if (requests.length === 1)
        return Response.json(
          { message: "Retry transport failed" },
          { status: 503 },
        );
      current = {
        ...current,
        deliveries: [
          {
            ...current.deliveries[0],
            state: "delivered",
            last_error: null,
            generation: 4,
            sdk_sequence: 7,
            available_actions: [],
          },
        ],
      };
      return Response.json(current.deliveries[0]);
    }
  });
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work&tab=materials");
  const original = await screen.findByText("Inspect the original request");
  const input = original.closest("article")!;
  expect(within(input).getByText("已送达")).toBeTruthy();
  expect(within(input).getByText("助手已读取")).toBeTruthy();
  expect(within(input).getByText("由本任务处理")).toBeTruthy();
  expect(
    within(input).getByText("Receipt confirmation interrupted"),
  ).toBeTruthy();
  expect(screen.queryByText("actual-step")).toBeNull();
  expect(screen.queryByText("original-input")).toBeNull();
  expect(screen.queryByText(/技术详情/)).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "重新发送" }));
  await screen.findByText("Retry transport failed");
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "重新发送" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "重新发送" }));
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "重新发送" })).toBeNull(),
  );
  expect(within(input).getByText("已送达")).toBeTruthy();
  expect(
    within(input).queryByText("Receipt confirmation interrupted"),
  ).toBeNull();
  expect(requests).toHaveLength(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(requests[0].expected_generation).toBe(3);
  expect(screen.queryByRole("button", { name: "重新发送" })).toBeNull();
  expect(screen.queryByText("actual-step")).toBeNull();
});

it("opens the new root returned by confirmed re-execution and preserves its source record", async () => {
  const source = {
    ...execution,
    status: "failed" as const,
    available_actions: ["retry"],
    revision: 9,
  };
  const requests: Record<string, unknown>[] = [];
  setup(initialSnapshot(), (path, options) => {
    if (path === "/v1/executions/work") return Response.json(source);
    if (path === "/v1/executions/work/retry") {
      requests.push(JSON.parse(String(options.body)));
      return Response.json({
        new_execution_id: "new-root",
        execution: {
          ...execution,
          execution_id: "new-root",
          source_execution_id: "work",
          objective: "Repeated objective",
        },
      });
    }
    if (path === "/v1/executions/new-root")
      return Response.json({
        ...execution,
        execution_id: "new-root",
        source_execution_id: "work",
        objective: "Repeated objective",
      });
  });
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work");
  fireEvent.click(await screen.findByRole("button", { name: "重新执行" }));
  fireEvent.change(screen.getByRole("textbox", { name: "补充要求" }), {
    target: { value: "Use the new evidence" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认重新执行" }));
  await screen.findByRole("heading", { name: "Repeated objective" });
  expect(location.hash).toContain("execution_id=new-root");
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({
    expected_revision: 9,
    supplement: "Use the new evidence",
  });
});

it("restores the persisted chat reading anchor through older-message pages after a hard reload", async () => {
  localStorage.setItem(
    "weave-chat-position-v2",
    JSON.stringify({
      following: false,
      anchor: { messageId: "old-message", offset: 40 },
    }),
  );
  const historyReads: string[] = [];
  const snapshot = {
    ...initialSnapshot(),
    messages: {
      ...page([message("latest", 100, "Newest committed text")]),
      next_cursor: 100,
      total: 2,
    },
  };
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      return new DOMRect(
        0,
        this.id === "message-old-message"
          ? 100
          : this.classList.contains("chat-history")
            ? 20
            : 0,
        300,
        100,
      );
    },
  );
  setup(snapshot, (path) => {
    if (path.startsWith("/v1/chat/messages?")) {
      historyReads.push(path);
      return Response.json(
        page([message("old-message", 20, "Earlier anchored text")]),
      );
    }
  });
  render(<App />);
  await ready();
  const anchored = await screen.findByText("Earlier anchored text");
  await waitFor(() =>
    expect(anchored.closest(".chat-history")?.scrollTop).toBe(40),
  );
  expect(historyReads).toHaveLength(1);
  expect(historyReads[0]).toContain("before_sequence=100");
  expect(HTMLElement.prototype.scrollTo).not.toHaveBeenCalled();
});

it("shows Router processing as temporary state without adding an uncommitted reply", async () => {
  const initial = {
    ...initialSnapshot(),
    routing: { state: "processing" as const, pending_input_count: 2 },
  };
  const state = setup(initial);
  render(<App />);
  await ready();
  await screen.findByText("Weave 正在处理 2 条输入…");
  expect(document.querySelectorAll(".message")).toHaveLength(1);
  state.snapshot = {
    ...state.snapshot,
    snapshot_sequence: 2,
    routing: { state: "idle", pending_input_count: 0 },
  };
  act(() => TestEventSource.instances.at(-1)!.emit("product"));
  await waitFor(() =>
    expect(screen.queryByText("Weave 正在处理 2 条输入…")).toBeNull(),
  );
  expect(document.querySelectorAll(".message")).toHaveLength(1);
});

it("preserves the selected historical GraphRun and node attempt through an artifact preview and tab changes", async () => {
  vi.stubGlobal("CSS", { escape: (value: string) => value });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const graph = {
    graph_id: "history",
    revision: 1,
    objective: "Saved graph",
    nodes: [
      {
        node_id: "review",
        objective: "Historical review",
        prompt: "",
        tool_codes: [],
        skill_codes: [],
      },
    ],
    edges: [],
  };
  const attempt = {
    node_attempt_id: "actual-attempt",
    execution_id: "work",
    graph_run_id: "historical-graph",
    node_id: "review",
    attempt: 2,
    input: { source: "actual input" },
    status: "succeeded",
    session_id: "node-session",
    sdk_run_id: "node-run",
    model_name: "fake",
    usage: null,
    artifact_ids: ["historical-artifact"],
    error: null,
    created_at: "2026-11-07T00:00:00Z",
    finished_at: "2026-11-07T00:00:00Z",
  };
  const graphRun = {
    graph_run_id: "historical-graph",
    execution_id: "work",
    graph_id: "history",
    graph_revision: 1,
    parent_run_id: "sdk-run",
    root_run_id: "sdk-run",
    status: "succeeded",
    revision: 1,
    created_at: "2026-11-07T00:00:00Z",
    state: {
      node_states: [{ node_id: "review", status: "succeeded", attempt: 2 }],
    },
  };
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/executions/work/graph-runs")
      return Response.json(
        page([graphRun, { ...graphRun, graph_run_id: "newer-graph" }]),
      );
    if (url.pathname === "/v1/executions/work/graph-runs/historical-graph")
      return Response.json({
        graph_run: graphRun,
        graph,
        node_attempts: [attempt],
        total: 1,
        next_cursor: null,
        read_sequence: 1,
      });
    if (url.pathname === "/v1/executions/work/node-attempts/actual-attempt")
      return Response.json(attempt);
    if (url.pathname === "/v1/executions/work/runs")
      return Response.json(
        runPage([
          {
            execution_id: "work",
            attempt: 1,
            invocation_id: "root",
            run_id: "sdk-run",
            state: "succeeded",
            checkpoint_ref: null,
            usage: {},
          },
        ]),
      );
    if (url.pathname === "/v1/executions/work/runs/sdk-run")
      return Response.json({
        run: {},
        steps: stepPage([]),
        operations: [],
        tool_results: [],
      });
    if (url.pathname === "/v1/artifacts/historical-artifact")
      return Response.json({
        artifact_id: "historical-artifact",
        created_at: "2026-11-07T00:00:00Z",
        name: "Historical evidence",
        mime: "text/plain",
        size: 6,
        digest: "digest",
        owner_execution_id: "work",
        graph_run_id: "historical-graph",
        node_attempt_id: "actual-attempt",
        previewable: true,
        downloadable: true,
        deletion_state: "active",
      });
    if (url.pathname === "/v1/artifacts/historical-artifact/text")
      return Response.json({
        id: "historical-artifact",
        text: "source",
        offset: 0,
        next_offset: null,
        total_bytes: 6,
        read_sequence: 1,
        read_scope: "product",
      });
  });
  render(<App />);
  await ready();
  openRoute(
    "#view=execution&execution_id=work&tab=process&run_id=sdk-run&graph_run_id=historical-graph&node_id=review&node_attempt_id=actual-attempt",
  );
  await screen.findByRole("combobox", { name: "选择节点尝试" });
  expect(
    (screen.getByRole("combobox", { name: "选择图运行" }) as HTMLSelectElement)
      .value,
  ).toBe("historical-graph");
  expect(
    (
      screen.getByRole("combobox", {
        name: "选择节点尝试",
      }) as HTMLSelectElement
    ).value,
  ).toBe("actual-attempt");
  expect(screen.getByText("有新的运行记录")).toBeTruthy();
  const input = composer();
  fireEvent.change(input, { target: { value: "Keep my current draft" } });
  fireEvent.click(screen.getByRole("button", { name: /查看节点产物/ }));
  await screen.findByRole("heading", { name: "Historical evidence" });
  fireEvent.click(screen.getByRole("button", { name: "返回来源" }));
  await screen.findByRole("combobox", { name: "选择节点尝试" });
  expect(location.hash).toContain("node_attempt_id=actual-attempt");
  await waitFor(() =>
    expect((document.activeElement as HTMLElement).dataset.contentId).toBe(
      "artifact/historical-artifact",
    ),
  );
  fireEvent.click(screen.getByRole("tab", { name: "概览" }));
  fireEvent.click(screen.getByRole("tab", { name: "过程" }));
  await screen.findByRole("combobox", { name: "选择节点尝试" });
  expect(
    (screen.getByRole("combobox", { name: "选择图运行" }) as HTMLSelectElement)
      .value,
  ).toBe("historical-graph");
  expect(
    (
      screen.getByRole("combobox", {
        name: "选择节点尝试",
      }) as HTMLSelectElement
    ).value,
  ).toBe("actual-attempt");
  expect(composer()).toBe(input);
  expect(composer().value).toBe("Keep my current draft");
  const close = screen.getByRole("button", { name: "关闭节点详情" });
  close.focus();
  fireEvent.keyDown(close, { key: "Escape" });
  await waitFor(() =>
    expect((document.activeElement as HTMLElement).dataset.id).toBe("review"),
  );
  fireEvent.keyDown(document.activeElement!, { key: "Enter" });
  await waitFor(() =>
    expect(
      (document.activeElement as HTMLElement).classList.contains(
        "node-inspector",
      ),
    ).toBe(true),
  );
  fireEvent.keyDown(document.activeElement!, { key: "Escape" });
  await waitFor(() =>
    expect((document.activeElement as HTMLElement).dataset.id).toBe("review"),
  );
});

it("keeps a Space settings draft mounted while reading resources and returning to its source", async () => {
  const state = setup();
  render(<App />);
  await ready();
  openRoute("#view=space&space_id=research&tab=settings");
  const name = await screen.findByRole("textbox", { name: "名称" });
  fireEvent.change(name, {
    target: { value: "Draft before resource inspection" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查看资源目录" }));
  const resourceHeading = await screen.findByRole("heading", {
    name: "资源目录",
  });
  await waitFor(() => expect(document.activeElement).toBe(resourceHeading));
  expect(screen.queryByRole("dialog", { name: "空间配置尚未保存" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "返回来源" }));
  const restored = await screen.findByRole("textbox", { name: "名称" });
  expect(restored).toBe(name);
  expect((restored as HTMLInputElement).value).toBe(
    "Draft before resource inspection",
  );
  expect(state.submissions).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  await screen.findByRole("dialog", { name: "空间配置尚未保存" });
});

it("requires an explicit configuration conflict choice before saving the preserved draft against a new revision", async () => {
  let current = space;
  const saves: Record<string, unknown>[] = [];
  setup(initialSnapshot(), (path, options) => {
    if (path === "/v1/spaces/research" && options.method === "PATCH") {
      const body = JSON.parse(String(options.body));
      saves.push(body);
      if (saves.length === 1) {
        current = { ...space, name: "Another editor's name", revision: 2 };
        return Response.json(
          { message: "Space revision changed" },
          { status: 409 },
        );
      }
      current = { ...space, name: body.name, revision: 3 };
      return Response.json(current);
    }
    if (path === "/v1/spaces/research") return Response.json(current);
  });
  render(<App />);
  await ready();
  openRoute("#view=space&space_id=research&tab=settings");
  fireEvent.change(await screen.findByRole("textbox", { name: "名称" }), {
    target: { value: "My retained draft" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await screen.findByRole("region", { name: "空间配置版本冲突" });
  expect(screen.getByText('"Another editor\'s name"')).toBeTruthy();
  expect(screen.getByText('"My retained draft"')).toBeTruthy();
  expect(
    (screen.getByRole("button", { name: "保存配置" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(
    screen.getByRole("button", { name: "保留草稿，基于新版本继续编辑" }),
  );
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "保存配置" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(saves).toHaveLength(2));
  expect(saves[0].expected_revision).toBe(1);
  expect(saves[1]).toMatchObject({
    name: "My retained draft",
    expected_revision: 2,
  });
  expect(saves[0].request_id).not.toBe(saves[1].request_id);
});

it("shows a returned message target's actual owner and pending reassignment before opening its responsibility records", async () => {
  const original: Message = {
    ...message("request", 1, "Reassign the remaining request"),
    role: "user",
    target_execution_ids: ["work"],
    input_dispositions: [
      {
        input_id: "original",
        origin_input_id: "origin",
        target_execution_id: "work",
        revision: 2,
        state: "returned",
        outcome_refs: [],
        handback_id: "handback",
        execution_objective: "研究原始问题",
        space_name: "真实研究空间",
        reassignment_pending: true,
      },
    ],
  };
  const state = setup({ ...initialSnapshot(), messages: page([original]) });
  render(<App />);
  await ready();
  const target = await screen.findByRole("button", {
    name: /真实研究空间 · 研究原始问题等待安排任务/,
  });
  fireEvent.click(target);
  await screen.findByRole("heading", { name: "收到的要求" });
  expect(location.hash).toContain("tab=materials");
  state.snapshot = {
    ...state.snapshot,
    snapshot_sequence: 2,
    messages: page(
      [
        {
          ...original,
          input_dispositions: [
            { ...original.input_dispositions[0], reassignment_pending: false },
            {
              ...original.input_dispositions[0],
              input_id: "reassigned",
              target_execution_id: "new-owner",
              state: "handled",
              handback_id: null,
              execution_objective: "数据汇总",
              space_name: "数据空间",
              reassignment_pending: false,
            },
          ],
        },
      ],
      2,
    ),
  };
  act(() => TestEventSource.instances.at(-1)!.emit("product"));
  await screen.findByRole("button", { name: /数据空间 · 数据汇总已处理/ });
  expect(screen.queryByText("等待安排任务")).toBeNull();
  expect(
    screen.getByRole("button", { name: /真实研究空间 · 研究原始问题已退回/ }),
  ).toBeTruthy();
});

it("reports a clipboard failure and confirms the subsequent copy of the actual committed reply", async () => {
  const copy = vi
    .fn()
    .mockRejectedValueOnce(new Error("Clipboard denied"))
    .mockResolvedValueOnce(undefined);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: copy } });
  setup();
  render(<App />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "复制消息 1" }));
  await screen.findByText(/复制失败，请选择原文手动复制/);
  fireEvent.click(screen.getByRole("button", { name: "复制消息 1" }));
  await screen.findByText("已复制");
  expect(copy.mock.calls).toEqual([
    ["工作正在独立推进。"],
    ["工作正在独立推进。"],
  ]);
  expect(screen.queryByText(/复制失败，请选择原文手动复制/)).toBeNull();
});

it("reads HTTP liveness and workspace readiness independently and refreshes their actual connection state", async () => {
  let readyState = false;
  const requests: string[] = [];
  const state = setup(initialSnapshot(), (path) => {
    if (path === "/healthz") {
      requests.push(path);
      return Response.json({ status: "ok" });
    }
    if (path === "/readyz") {
      requests.push(path);
      return Response.json(
        { ready: readyState },
        { status: readyState ? 200 : 503 },
      );
    }
  });
  render(<App />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  await screen.findByText("HTTP 进程 · 运行中");
  await screen.findByText("工作区接收能力 · 尚未就绪（HTTP 503）");
  readyState = true;
  fireEvent.click(screen.getByRole("button", { name: "重新检查连接" }));
  await screen.findByText("工作区接收能力 · 已就绪（HTTP 200）");
  expect(requests).toEqual(["/healthz", "/readyz", "/healthz", "/readyz"]);
  expect(state.submissions).toHaveLength(0);
});

it("selects resources beyond the first binding page and preserves selections while searching and clearing with Escape", async () => {
  const tools = Array.from({ length: 51 }, (_, index) => ({
    code: `tool-${index + 1}`,
    name: `Bindable tool ${index + 1}`,
    kind: "tools",
    description: "Host-authorized tool",
    version: null,
    available: true,
    disabled_reason: null,
    configuration_location: null,
  }));
  const updates: Record<string, unknown>[] = [];
  setup(initialSnapshot(), (path, options) => {
    const url = new URL(path, "http://localhost");
    if (
      url.pathname === "/v1/resources" &&
      url.searchParams.get("kind") === "tools"
    ) {
      const query = url.searchParams.get("query") ?? "";
      const cursor = Number(url.searchParams.get("cursor"));
      const found = tools.filter((tool) => tool.name.includes(query));
      return Response.json({
        ...page(found.slice(cursor, cursor + 50)),
        total: found.length,
        next_cursor: cursor + 50 < found.length ? cursor + 50 : null,
      });
    }
    if (path === "/v1/spaces/research" && options.method === "PATCH") {
      updates.push(JSON.parse(String(options.body)));
      return Response.json({ ...space, revision: 2 });
    }
  });
  render(<App />);
  await ready();
  openRoute("#view=space&space_id=research&tab=settings");
  const group = await screen.findByRole("group", { name: "工具" });
  fireEvent.click(
    await within(group).findByRole("checkbox", { name: "Bindable tool 1" }),
  );
  fireEvent.click(within(group).getByRole("button", { name: "下一页" }));
  fireEvent.click(
    await within(group).findByRole("checkbox", { name: "Bindable tool 51" }),
  );
  expect(updates).toHaveLength(0);
  const search = within(group).getByRole("textbox", { name: "搜索可绑定工具" });
  fireEvent.change(search, { target: { value: "Bindable tool 51" } });
  await waitFor(() =>
    expect(
      (
        within(group).getByRole("checkbox", {
          name: "Bindable tool 51",
        }) as HTMLInputElement
      ).checked,
    ).toBe(true),
  );
  fireEvent.keyDown(search, { key: "Escape" });
  await within(group).findByRole("checkbox", { name: "Bindable tool 1" });
  expect((search as HTMLInputElement).value).toBe("");
  expect(
    (
      within(group).getByRole("checkbox", {
        name: "Bindable tool 1",
      }) as HTMLInputElement
    ).checked,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(updates).toHaveLength(1));
  expect(updates[0].tool_codes).toEqual(["tool-1", "tool-51"]);
});

it("clears Space search with Escape without changing the permanent composer draft", async () => {
  setup();
  render(<App />);
  await ready();
  fireEvent.change(composer(), { target: { value: "Keep my draft" } });
  const search = screen.getByRole("textbox", { name: "搜索空间" });
  fireEvent.change(search, { target: { value: "Research query" } });
  fireEvent.keyDown(search, { key: "Escape" });
  expect((search as HTMLInputElement).value).toBe("");
  expect(composer().value).toBe("Keep my draft");
});

it("removes a revoked binding even when the host no longer returns that resource in its catalog", async () => {
  const updates: Record<string, unknown>[] = [];
  setup(initialSnapshot(), (path, options) => {
    if (path === "/v1/spaces/research" && options.method === "PATCH") {
      updates.push(JSON.parse(String(options.body)));
      return Response.json({ ...space, revision: 2 });
    }
    if (path === "/v1/spaces/research")
      return Response.json({ ...space, tool_codes: ["revoked-tool"] });
  });
  render(<App />);
  await ready();
  openRoute("#view=space&space_id=research&tab=settings");
  fireEvent.click(
    await screen.findByRole("button", { name: "移除已绑定工具 revoked-tool" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() => expect(updates).toHaveLength(1));
  expect(updates[0].tool_codes).toEqual([]);
});

it("shows trusted handback settlement separately from unresolved external effects in chat and work details", async () => {
  const action = {
    run_id: "sdk-run",
    call_id: "write",
    operation_id: "original-operation",
  };
  const returned = {
    handback_id: "returned",
    origin_input_id: "origin",
    target_execution_id: "work",
    input_id: "source",
    reason: "Confirm the original write",
    already_handled_refs: [],
    effect_refs: [],
    unknown_effects: true,
    run_id: "sdk-run",
    action_refs: [action],
    reroute_round: 0,
  };
  const resolution = {
    handback_id: returned.handback_id,
    run_id: "sdk-run",
    action_refs: [action],
    reconciliation_id: "trusted-reconciliation",
    evidence_ids: ["host-evidence"],
    reconciled_revision: 7,
    resolved_at: "2026-11-07T00:00:00Z",
  };
  const original = {
    ...message("reconciled-request", 1, "Read the original outcome"),
    handbacks: [
      returned,
      {
        ...returned,
        handback_id: "unresolved",
        target_execution_id: "other-work",
      },
    ],
    handback_resolutions: [resolution],
  };
  setup({ ...initialSnapshot(), messages: page([original]) }, (path) => {
    if (path === "/v1/executions/work")
      return Response.json({
        ...execution,
        inputs: [
          {
            input_id: "source",
            origin_input_id: "origin",
            target_sequence: 1,
            source: "weave.chat",
            content: "Original write",
            causation_id: null,
            status: "handled",
            sdk_consumption: null,
          },
        ],
        dispositions: [
          {
            origin_input_id: "origin",
            target_execution_id: "work",
            input_id: "source",
            revision: 2,
            state: "returned",
            outcome_refs: [],
            handback_id: returned.handback_id,
          },
        ],
        handbacks: [returned],
        handback_resolutions: [resolution],
      });
  });
  render(<App />);
  await ready();
  expect(await screen.findByText("操作影响已核实")).toBeTruthy();
  expect(screen.getByText("有些操作的结果尚未确认")).toBeTruthy();
  fireEvent.click(screen.getAllByRole("button", { name: "查看核实记录" })[0]);
  await screen.findByRole("heading", { name: "收到的要求" });
  const work = screen.getByRole("main", { name: "工作区" });
  expect(within(work).getByText("操作影响已核实")).toBeTruthy();
  expect(
    within(work).getByText(
      new Date(resolution.resolved_at).toLocaleString("zh-CN"),
    ),
  ).toBeTruthy();
  expect(
    within(work).queryByText(/host-evidence|trusted-reconciliation/),
  ).toBeNull();
  expect(within(work).queryByText("有些操作的结果尚未确认")).toBeNull();
});

it("restores the prior right-side route with browser Back while preserving the mounted composer", async () => {
  setup();
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.change(input, {
    target: { value: "Navigate back with this draft" },
  });
  fireEvent.click(await screen.findByRole("button", { name: space.name }));
  fireEvent.click(
    await screen.findByRole("button", { name: execution.objective }),
  );
  await screen.findByRole("heading", { name: execution.objective });
  const workspace = screen.getByRole("main", { name: "工作区" });
  workspace.scrollTop = 240;
  fireEvent.scroll(workspace);
  fireEvent.click(screen.getByRole("button", { name: "资源" }));
  await screen.findByRole("heading", { name: "资源目录" });
  await act(async () => {
    window.history.back();
  });
  await screen.findByRole("heading", { name: execution.objective });
  expect(workspace.scrollTop).toBe(240);
  expect(composer()).toBe(input);
  expect(composer().value).toBe("Navigate back with this draft");
});

it("lists actual child work and its current cancellation impact before cancelling a parent", async () => {
  const parent = {
    ...execution,
    status: "waiting_children",
    children: [
      {
        ...execution,
        execution_id: "first-child",
        parent_execution_id: "work",
        objective: "Research sources",
        status: "running",
      },
      {
        ...execution,
        execution_id: "second-child",
        parent_execution_id: "work",
        objective: "Finished comparison",
        status: "succeeded",
      },
    ],
  };
  setup(initialSnapshot(), (path) =>
    path === "/v1/executions/work" ? Response.json(parent) : undefined,
  );
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work");
  fireEvent.click(await screen.findByRole("button", { name: "取消" }));
  const dialog = await screen.findByRole("dialog", { name: "取消这项工作" });
  expect(
    within(dialog).getByText("Research sources").parentElement?.textContent,
  ).toContain("将请求取消");
  expect(
    within(dialog).getByText("Finished comparison").parentElement?.textContent,
  ).toContain("已结束");
});

it("starts a deletion preview on its safe return action and restores the trigger after native dialog cancellation", async () => {
  setup(initialSnapshot(), (path) =>
    path === "/v1/spaces/research/deletion-preview"
      ? Response.json({
          space_id: "research",
          space_revision: 1,
          preview_token: "preview",
          expires_at: "2099-01-01T00:00:00Z",
          execution_ids: [],
          artifact_ids: [],
          shared_artifact_ids: [],
          blockers: [],
          scopes: {
            sdk_session_count: 0,
            task_file_count: 0,
            space_fact_count: 0,
            digest: "scope",
            blockers: [],
          },
        })
      : undefined,
  );
  render(<App />);
  await ready();
  openRoute("#view=space&space_id=research&tab=settings");
  const opener = await screen.findByRole("button", { name: "删除预览" });
  opener.focus();
  fireEvent.click(opener);
  const dialog = await screen.findByRole("dialog", {
    name: "核对空间删除影响",
  });
  expect(document.activeElement).toBe(
    within(dialog).getByRole("button", { name: "返回" }),
  );
  fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
  await waitFor(() =>
    expect(
      screen.queryByRole("dialog", { name: "核对空间删除影响" }),
    ).toBeNull(),
  );
  expect(document.activeElement).toBe(opener);
});

it("returns to the permanent composer after clearing an answer tag without sending or losing its draft", async () => {
  const state = setup({ ...initialSnapshot(), attentions: page([attention]) });
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.click(await screen.findByRole("button", { name: "在主聊天回答" }));
  fireEvent.change(input, {
    target: { value: "Keep this answer as a draft" },
  });
  const clear = screen.getByRole("button", { name: "退出回答模式" });
  clear.focus();
  fireEvent.click(clear);
  expect(document.activeElement).toBe(composer());
  expect(composer().value).toBe("Keep this answer as a draft");
  expect(state.submissions).toHaveLength(0);
});

it("returns to the permanent composer after removing the last temporary attachment", async () => {
  setup();
  render(<App />);
  await ready();
  fireEvent.change(document.querySelector('input[type="file"]')!, {
    target: {
      files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
    },
  });
  const remove = await screen.findByRole("button", {
    name: "移除附件 notes.txt",
  });
  remove.focus();
  fireEvent.click(remove);
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "移除附件 notes.txt" }),
    ).toBeNull(),
  );
  expect(document.activeElement).toBe(composer());
});

it("shows an unscheduled graph node as unknown until actual scheduling or attempt facts exist", async () => {
  const graphRun = {
    graph_run_id: "starting-graph",
    execution_id: "work",
    graph_id: "starting",
    graph_revision: 1,
    parent_run_id: "sdk-run",
    root_run_id: "sdk-run",
    status: "running",
    revision: 1,
    created_at: "2026-11-07T00:00:00Z",
    state: null,
  };
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/executions/work/graph-runs")
      return Response.json(page([graphRun]));
    if (url.pathname === "/v1/executions/work/graph-runs/starting-graph")
      return Response.json({
        graph_run: graphRun,
        graph: {
          graph_id: "starting",
          revision: 1,
          objective: "Starting graph",
          nodes: [
            {
              node_id: "waiting-for-facts",
              objective: "No scheduling fact yet",
              prompt: "",
              tool_codes: [],
              skill_codes: [],
            },
          ],
          edges: [],
        },
        node_attempts: [],
        total: 0,
        next_cursor: null,
        read_sequence: 1,
      });
    if (url.pathname === "/v1/executions/work/runs")
      return Response.json(runPage([]));
    if (url.pathname === "/v1/executions/work/runs/sdk-run")
      return Response.json({
        run: {},
        steps: stepPage([]),
        operations: [],
        tool_results: [],
      });
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  render(<App />);
  await ready();
  openRoute(
    "#view=execution&execution_id=work&tab=process&run_id=sdk-run&graph_run_id=starting-graph",
  );
  await screen.findByText("No scheduling fact yet");
  expect(screen.getByText("待核实")).toBeTruthy();
  expect(screen.queryByText("等待中")).toBeNull();
});

it("shows original requests and readable task results without exposing runtime envelopes", async () => {
  const requests: string[] = [];
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (
      url.pathname === "/v1/executions/work/graph-runs" ||
      url.pathname === "/v1/executions/work/runs"
    )
      return Response.json(page([]));
    if (url.pathname === "/v1/executions/work/runs/sdk-run")
      return Response.json({
        run: {},
        steps: stepPage([
          {
            step_id: "lookup-step",
            run_id: "sdk-run",
            sequence: 1,
            run_sequence: 1,
            agent_id: "worker",
            input_ids: [],
            state: "completed",
            messages: [
              {
                message_id: "known-input",
                role: "user",
                content: JSON.stringify({
                  input_id: "known-input",
                  kind: "user",
                  source: "weave.message",
                  content: "用户的原始要求。",
                  causation_id: "hidden-causation",
                }),
              },
              {
                message_id: "business-input",
                role: "user",
                content:
                  '{"id":"order-123","source":"customer","content":"实际业务数据"}',
              },
              {
                message_id: "operation:op-1:result",
                role: "user",
                content: JSON.stringify({
                  input_id: "operation:op-1:result",
                  kind: "operation_result",
                  source: "pleiad.operation",
                  content: {
                    reference: "operation:op-1:result",
                    digest: "a".repeat(64),
                    summary: JSON.stringify({
                      ok: true,
                      preview: "partial raw envelope",
                      read_tool_result: {
                        operation_id: "op-1",
                        start_char: 0,
                        max_chars: 4000,
                      },
                    }),
                  },
                  causation_id: null,
                }),
              },
              {
                message_id: "assistant",
                role: "assistant",
                content: null,
                tool_calls: [
                  {
                    id: "lookup-call",
                    name: "query_work",
                    arguments: JSON.stringify({
                      kind: "objective",
                      id: "internal-task-id",
                      start_char: 0,
                      max_chars: 4000,
                      expected_digest: "internal-digest",
                    }),
                  },
                ],
              },
              {
                message_id: "lookup-result",
                role: "tool",
                tool_call_id: "lookup-call",
                content: "Stored result",
              },
            ],
          },
        ]),
        operations: [],
        tool_results: [
          {
            kind: "tool_message",
            tool_result_id: "sdk-run/lookup-result",
            step_id: "lookup-step",
            call_id: "lookup-call",
          },
        ],
      });
    if (url.pathname.includes("/tool-results/")) {
      requests.push(url.searchParams.get("view") ?? "");
      return Response.json({
        id: "sdk-run/lookup-result",
        format: "markdown",
        text: "## 调查目标\n\n核对模型发布公告。\n\n- 保留来源链接\n- 标出发布日期",
        offset: 0,
        next_offset: null,
        total_bytes: 110,
        read_scope: "session",
        read_sequence: 2,
      });
    }
  });
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work&tab=process&run_id=sdk-run");
  await screen.findByText(/步骤 1 · .*已完成/);
  const step = document.querySelector<HTMLDetailsElement>(
    '[data-step-id="lookup-step"]',
  )!;
  expect(step.open).toBe(true);
  expect(within(step).getByText("用户的原始要求。")).toBeTruthy();
  expect(
    within(step).getByText(
      '{"id":"order-123","source":"customer","content":"实际业务数据"}',
    ),
  ).toBeTruthy();
  expect(within(step).getByText("已收到运行结果")).toBeTruthy();
  const call = within(step).getByRole("article", { name: "阅读任务要求" });
  await within(call).findByRole("heading", { name: "调查目标" });
  expect(within(call).getByText("核对模型发布公告。")).toBeTruthy();
  expect(
    within(call)
      .getAllByRole("listitem")
      .map((item) => item.textContent),
  ).toEqual(["保留来源链接", "标出发布日期"]);
  expect(within(step).queryByText(/助手输出|助手说明/)).toBeNull();
  expect(step.textContent).not.toMatch(
    /query_work|internal-task-id|internal-digest|expected_digest|Stored result|known-input|hidden-causation|operation:op-1:result|partial raw envelope/,
  );
  expect(requests).toEqual(["display"]);
});

it("shows a paused unfinished step without claiming it is still running", async () => {
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/executions/work")
      return Response.json({ ...execution, status: "paused" });
    if (url.pathname === "/v1/executions/work/graph-runs")
      return Response.json(page([]));
    if (url.pathname === "/v1/executions/work/runs")
      return Response.json(
        runPage([{ run_id: "sdk-run", attempt: 1, state: "paused" }]),
      );
    if (url.pathname === "/v1/executions/work/runs/sdk-run")
      return Response.json({
        run: { state: "paused" },
        steps: stepPage([
          {
            step_id: "unfinished-step",
            run_id: "sdk-run",
            sequence: 1,
            run_sequence: 1,
            agent_id: "worker",
            input_ids: [],
            state: "active",
            messages: [
              {
                message_id: "assistant",
                role: "assistant",
                content: null,
                tool_calls: [
                  {
                    id: "read-call",
                    name: "read_tool_result",
                    arguments: { operation_id: "operation-id" },
                  },
                ],
              },
            ],
          },
        ]),
        operations: [],
        tool_results: [],
      });
  });
  render(<App />);
  await ready();
  openRoute("#view=execution&execution_id=work&tab=process&run_id=sdk-run");
  await screen.findByText("步骤 1 · 已暂停");
  const step = document.querySelector<HTMLDetailsElement>(
    '[data-step-id="unfinished-step"]',
  )!;
  expect(step.textContent).toContain("读取运行结果");
  expect(step.textContent).not.toMatch(/进行中|read_tool_result|operation-id/);
  expect(screen.queryByRole("combobox", { name: "选择处理记录" })).toBeNull();
});

it.each(["running", "cancelled"])(
  "keeps tool results inside their actual calls while another operation is %s",
  async (operationState) => {
    const requests: string[] = [];
    setup(initialSnapshot(), (path) => {
      const url = new URL(path, "http://localhost");
      if (url.pathname === "/v1/executions/work/graph-runs")
        return Response.json(page([]));
      if (url.pathname === "/v1/executions/work/runs")
        return Response.json(
          runPage([
            {
              execution_id: "work",
              attempt: 1,
              invocation_id: "invocation",
              run_id: "backend-root",
              state: "succeeded",
              checkpoint_ref: null,
              usage: {},
            },
          ]),
        );
      if (url.pathname === "/v1/executions/work/runs/backend-root")
        return Response.json({
          run: {},
          steps: stepPage(
            [1, 2].map((sequence) => ({
              step_id: `step-${sequence}`,
              run_id: "backend-root",
              sequence,
              run_sequence: sequence,
              agent_id: "worker",
              input_ids: [],
              state: "completed",
              messages: [
                {
                  message_id: `assistant-${sequence}`,
                  role: "assistant",
                  content: "",
                  tool_calls: [
                    {
                      id: `call-${sequence}`,
                      name: "command",
                      arguments: { command: `research-${sequence}` },
                    },
                  ],
                },
                {
                  message_id: `receipt-${sequence}`,
                  role: "tool",
                  tool_call_id: `call-${sequence}`,
                  content:
                    "Original observation is available through its owned result reference",
                },
              ],
            })),
          ),
          operations: [
            {
              operation_id: "finished-operation",
              step_id: "step-1",
              call_id: "call-1",
              state: "succeeded",
            },
            {
              operation_id: "other-operation",
              step_id: "step-2",
              call_id: "call-2",
              state: operationState,
            },
          ],
          tool_results: [
            ...[1, 2].map((sequence) => ({
              kind: "tool_message",
              tool_result_id: `backend-root/receipt-${sequence}`,
              step_id: `step-${sequence}`,
              call_id: `call-${sequence}`,
            })),
            {
              kind: "operation",
              tool_result_id: "operation/backend-root/finished-operation",
              operation_id: "finished-operation",
              state: "succeeded",
              step_id: "step-1",
              call_id: "call-1",
            },
          ],
        });
      if (url.pathname.includes("/tool-results/")) {
        const identity = decodeURIComponent(
          url.pathname.split("/tool-results/")[1],
        );
        const offset = Number(url.searchParams.get("offset"));
        requests.push(
          `${identity}?view=${url.searchParams.get("view")}&offset=${offset}`,
        );
        const background = identity.startsWith("operation/");
        return Response.json({
          id: identity,
          format: "text",
          text: background
            ? offset === 0
              ? "First background page"
              : "Final background page"
            : "Original acceptance receipt",
          offset,
          next_offset: background && offset === 0 ? 21 : null,
          total_bytes: background ? 42 : 27,
          read_scope: "session",
          read_sequence: 12,
        });
      }
    });
    render(<App />);
    await ready();
    openRoute(
      "#view=execution&execution_id=work&tab=process&run_id=backend-root",
    );
    await screen.findByText(/步骤 1 · .*已完成/);
    const first = document.querySelector<HTMLDetailsElement>(
      '[data-step-id="step-1"]',
    )!;
    const second = document.querySelector<HTMLDetailsElement>(
      '[data-step-id="step-2"]',
    )!;
    expect(second.open).toBe(true);
    const secondCall = await within(second).findByRole("article");
    await within(secondCall).findByText("Original acceptance receipt");
    expect(requests).toEqual(["backend-root/receipt-2?view=display&offset=0"]);
    fireEvent.click(first.querySelector("summary")!);
    const firstCall = await within(first).findByRole("article");
    await within(firstCall).findByText("First background page");
    expect(requests).toEqual([
      "backend-root/receipt-2?view=display&offset=0",
      "backend-root/receipt-1?view=display&offset=0",
      "operation/backend-root/finished-operation?view=display&offset=0",
    ]);
    expect(within(firstCall).getByText("已完成")).toBeTruthy();
    expect(
      within(secondCall).getByText(
        operationState === "running" ? "进行中" : "已取消",
      ),
    ).toBeTruthy();
    expect(
      within(secondCall).getByText(
        operationState === "running" ? "等待结果" : "无返回结果",
      ),
    ).toBeTruthy();
    expect(
      within(secondCall).queryByRole("region", { name: "运行结果" }),
    ).toBeNull();
    expect(
      within(firstCall).getByText("Original acceptance receipt"),
    ).toBeTruthy();
    const background = within(firstCall).getByRole("region", {
      name: "后台执行",
    });
    await within(background).findByText("First background page");
    fireEvent.click(
      within(background).getByRole("button", { name: "下一页内容" }),
    );
    await within(background).findByText("Final background page");
    expect(
      within(firstCall).getByText("Original acceptance receipt"),
    ).toBeTruthy();
    expect(within(secondCall).queryByText("Final background page")).toBeNull();
    expect(
      within(background).queryByText(/SDK Session|读取位置|技术详情/),
    ).toBeNull();
    fireEvent.click(
      within(background).getByRole("button", { name: "上一页内容" }),
    );
    await within(background).findByText("First background page");
    expect(requests).toEqual([
      "backend-root/receipt-2?view=display&offset=0",
      "backend-root/receipt-1?view=display&offset=0",
      "operation/backend-root/finished-operation?view=display&offset=0",
      "operation/backend-root/finished-operation?view=display&offset=21",
      "operation/backend-root/finished-operation?view=display&offset=0",
    ]);
  },
);

it.each(["wrong-step", "missing-operation"])(
  "reports a tool result with %s instead of presenting it under an unrelated call",
  async (association) => {
    setup(initialSnapshot(), (path) => {
      const url = new URL(path, "http://localhost");
      if (url.pathname === "/v1/executions/work/graph-runs")
        return Response.json(page([]));
      if (url.pathname === "/v1/executions/work/runs")
        return Response.json(runPage([]));
      if (url.pathname === "/v1/executions/work/runs/backend-root")
        return Response.json({
          run: {},
          steps: stepPage([
            {
              step_id: "actual-step",
              run_id: "backend-root",
              sequence: 1,
              run_sequence: 1,
              agent_id: "worker",
              input_ids: [],
              state: "active",
              messages: [
                {
                  message_id: "assistant",
                  role: "assistant",
                  content: "",
                  tool_calls: [
                    { id: "actual-call", name: "command", arguments: {} },
                  ],
                },
              ],
            },
          ]),
          operations: [],
          tool_results: [
            {
              kind: "operation",
              operation_id: "orphan-operation",
              state: "succeeded",
              tool_result_id: "operation/backend-root/orphan-operation",
              call_id: "actual-call",
              step_id:
                association === "wrong-step" ? "another-step" : "actual-step",
            },
          ],
        });
    });
    render(<App />);
    await ready();
    openRoute(
      "#view=execution&execution_id=work&tab=process&run_id=backend-root",
    );
    await screen.findByText("部分执行记录的所属步骤无法核验，请刷新后查看。");
    const step = document.querySelector<HTMLDetailsElement>(
      '[data-step-id="actual-step"]',
    )!;
    expect(step.open).toBe(true);
    expect(within(step).queryByRole("region", { name: "运行结果" })).toBeNull();
  },
);

it("restores Space filters, loaded pages and the right-pane reading position after opening a result", async () => {
  const values = Array.from({ length: 51 }, (_, index) => ({
    ...space,
    space_id: `review-${index}`,
    name: `Review ${index + 1}`,
    status: "archived" as const,
  }));
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/spaces") {
      const cursor = Number(url.searchParams.get("cursor"));
      return Response.json({
        ...page(values.slice(cursor, cursor + 50)),
        total: values.length,
        next_cursor: cursor === 0 ? 50 : null,
      });
    }
    if (url.pathname === "/v1/spaces/review-50")
      return Response.json(values[50]);
  });
  render(<App />);
  await ready();
  const query = screen.getByRole("textbox", { name: "搜索空间" });
  fireEvent.change(query, { target: { value: "Review" } });
  fireEvent.change(screen.getByRole("combobox", { name: "空间状态" }), {
    target: { value: "archived" },
  });
  fireEvent.click(await screen.findByRole("button", { name: "加载更多空间" }));
  const result = await screen.findByRole("button", { name: "Review 51" });
  const workspace = screen.getByRole("main", { name: "工作区" });
  workspace.scrollTop = 480;
  fireEvent.scroll(workspace);
  fireEvent.click(result);
  await screen.findByRole("heading", { name: "Review 51" });
  fireEvent.click(screen.getByRole("button", { name: "空间" }));
  await screen.findByRole("button", { name: "Review 51" });
  expect(
    (screen.getByRole("textbox", { name: "搜索空间" }) as HTMLInputElement)
      .value,
  ).toBe("Review");
  expect(
    (screen.getByRole("combobox", { name: "空间状态" }) as HTMLSelectElement)
      .value,
  ).toBe("archived");
  expect(workspace.scrollTop).toBe(480);
  await waitFor(() =>
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Review 51" }),
    ),
  );
});

it("restores resource category, search and page after leaving the catalog", async () => {
  const requests: string[] = [];
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname !== "/v1/resources") return;
    requests.push(url.search);
    const cursor = Number(url.searchParams.get("cursor"));
    return Response.json({
      ...page([]),
      total: 51,
      next_cursor: cursor === 0 ? 50 : null,
    });
  });
  render(<App />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "资源" }));
  fireEvent.click(await screen.findByRole("tab", { name: "技能" }));
  fireEvent.change(screen.getByRole("textbox", { name: "搜索资源" }), {
    target: { value: "review" },
  });
  fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() => expect(requests.at(-1)).toContain("cursor=50"));
  fireEvent.click(screen.getByRole("button", { name: "设置" }));
  await screen.findByRole("heading", { name: "设置" });
  fireEvent.click(screen.getByRole("button", { name: "资源" }));
  await screen.findByRole("heading", { name: "资源目录" });
  expect(
    screen.getByRole("tab", { name: "技能" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(
    (screen.getByRole("textbox", { name: "搜索资源" }) as HTMLInputElement)
      .value,
  ).toBe("review");
  await waitFor(() =>
    expect(requests.at(-1)).toBe("?kind=skills&query=review&cursor=50"),
  );
});

it("shows the actual running-work impact and requires confirmation before archiving a Space", async () => {
  const current = { ...space, active_execution_count: 3 };
  const state = setup(initialSnapshot(), (path, options) => {
    if (path === "/v1/spaces/research" && !options.method)
      return Response.json(current);
  });
  render(<App />);
  await ready();
  openRoute("#view=space&space_id=research&tab=settings");
  const trigger = await screen.findByRole("button", { name: "归档空间" });
  fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog", { name: "确认归档空间" });
  expect(within(dialog).getByText(/仍有 3 项进行中的工作/)).toBeTruthy();
  expect(state.submissions).toHaveLength(0);
  fireEvent.click(within(dialog).getByRole("button", { name: "继续使用空间" }));
  expect(state.submissions).toHaveLength(0);
  fireEvent.click(trigger);
  fireEvent.click(await screen.findByRole("button", { name: "确认归档" }));
  await waitFor(() => expect(state.submissions).toHaveLength(1));
  expect(state.submissions[0].path).toBe("/v1/spaces/research/archive");
  expect(state.submissions[0].body.expected_revision).toBe(current.revision);
});

it.each(["space", "resource"] as const)(
  "opens an unambiguous %s search result with Enter and keeps ambiguous results unselected",
  async (kind) => {
    const resource = {
      kind: "tools",
      code: "review",
      name: "Review",
      description: "Checks sources",
      available: true,
      disabled_reason: null,
      version: "1",
      configuration_location: null,
      schema: null,
    };
    setup(initialSnapshot(), (path) => {
      const url = new URL(path, "http://localhost");
      const values = kind === "space" ? [space] : [resource];
      if (
        url.pathname === (kind === "space" ? "/v1/spaces" : "/v1/resources")
      ) {
        const items =
          url.searchParams.get("query") === "unique"
            ? values
            : [
                ...values,
                ...values.map(() =>
                  kind === "space"
                    ? { ...space, space_id: "other", name: "Other" }
                    : { ...resource, code: "other", name: "Other" },
                ),
              ];
        return Response.json(page(items));
      }
      if (url.pathname === "/v1/resources/tools/review")
        return Response.json({
          ...resource,
          metadata: {},
          configuration: {},
          input_schema: null,
          output_schema: null,
          content: null,
          files: [],
        });
    });
    render(<App />);
    await ready();
    if (kind === "resource")
      fireEvent.click(screen.getByRole("button", { name: "资源" }));
    const search = await screen.findByRole("textbox", {
      name: kind === "space" ? "搜索空间" : "搜索资源",
    });
    fireEvent.change(search, { target: { value: "ambiguous" } });
    await screen.findByRole("button", {
      name: kind === "space" ? "Other" : /Other/,
    });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(
      screen.queryByRole("heading", {
        name: kind === "space" ? space.name : resource.name,
      }),
    ).toBeNull();
    fireEvent.change(search, { target: { value: "unique" } });
    await waitFor(() =>
      expect(
        screen.queryByRole("button", {
          name: kind === "space" ? "Other" : /Other/,
        }),
      ).toBeNull(),
    );
    fireEvent.keyDown(search, { key: "Enter" });
    if (kind === "space")
      await screen.findByRole("heading", { name: space.name });
    else await screen.findByRole("dialog", { name: resource.name });
  },
);

it("locates an off-page pending item in the permanent chat without changing the composer draft", async () => {
  const selected = {
    ...attention,
    attention_id: "off-page",
    question: "Which verified source should be used?",
  };
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/attentions")
      return Response.json(page([selected]));
    if (url.pathname === "/v1/attentions/off-page")
      return Response.json(selected);
  });
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.change(input, { target: { value: "Keep my current draft" } });
  fireEvent.click(screen.getByRole("button", { name: /需要处理/ }));
  fireEvent.click(
    await screen.findByRole("button", { name: "定位主聊天事项" }),
  );
  const chat = screen.getByRole("region", { name: "永久主聊天" });
  await within(chat).findByText(selected.question);
  await waitFor(() =>
    expect(document.activeElement?.id).toBe("chat-attention-off-page"),
  );
  expect(chat.contains(document.activeElement)).toBe(true);
  expect(composer()).toBe(input);
  expect(input.value).toBe("Keep my current draft");
});

it("warns near the actual UTF-8 limit while preserving attachments and blocking oversized sends", async () => {
  const snapshot = initialSnapshot();
  snapshot.capabilities.max_message_bytes = 7;
  snapshot.capabilities.max_attachments = 2;
  setup(snapshot);
  render(<App />);
  await ready();
  expect(screen.queryByRole("status", { name: "草稿容量提示" })).toBeNull();
  fireEvent.change(document.querySelector('input[type="file"]')!, {
    target: {
      files: [new File(["notes"], "notes.txt", { type: "text/plain" })],
    },
  });
  await screen.findByText("notes.txt");
  fireEvent.change(composer(), { target: { value: "你好a" } });
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  expect(screen.getByRole("status", { name: "草稿容量提示" }).textContent).toBe(
    "内容接近输入上限。",
  );
  fireEvent.change(composer(), { target: { value: "你好ab" } });
  expect(screen.getByRole("status", { name: "草稿容量提示" }).textContent).toBe(
    "内容已超出输入上限，请删减后发送。",
  );
  expect(
    (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(composer().value).toBe("你好ab");
  expect(screen.getByText("notes.txt")).toBeTruthy();
  fireEvent.change(composer(), { target: { value: "你好" } });
  expect(screen.queryByRole("status", { name: "草稿容量提示" })).toBeNull();
});

it.each(["artifact", "attachment"] as const)(
  "pages through actual UTF-8 %s text without fetching the whole content",
  async (kind) => {
    const requests: string[] = [];
    const identity = "paged-source";
    setup(initialSnapshot(), (path) => {
      const url = new URL(path, "http://localhost");
      if (url.pathname === `/v1/${kind}s/${identity}`)
        return Response.json(
          kind === "attachment"
            ? {
                ...attachment,
                attachment_id: identity,
                name: "分页资料",
                mime: "text/plain",
                size: 18,
              }
            : {
                artifact_id: identity,
                created_at: "2026-11-07T00:00:00Z",
                name: "分页资料",
                mime: "text/plain",
                size: 18,
                digest: "digest",
                owner_execution_id: "work",
                graph_run_id: null,
                node_attempt_id: null,
                previewable: true,
                downloadable: true,
                deletion_state: "active",
              },
        );
      if (url.pathname === `/v1/${kind}s/${identity}/text`) {
        requests.push(url.search);
        const offset = Number(url.searchParams.get("offset"));
        return Response.json({
          id: identity,
          text: offset === 0 ? "第一段" : "第二段",
          offset,
          next_offset: offset === 0 ? 9 : null,
          total_bytes: 18,
          read_scope: "product",
          read_sequence: 7,
        });
      }
    });
    render(<App />);
    await ready();
    const input = composer();
    fireEvent.change(input, { target: { value: "Keep the source question" } });
    openRoute(`#view=${kind}&${kind}_id=${identity}`);
    await screen.findByText("第一段");
    fireEvent.click(screen.getByRole("button", { name: "下一段资料" }));
    await screen.findByText("第二段");
    expect(screen.queryByText("第一段")).toBeNull();
    expect(
      (screen.getByRole("button", { name: "下一段资料" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "上一段资料" }));
    await screen.findByText("第一段");
    expect(requests).toEqual([
      "?offset=0&max_bytes=8192",
      "?offset=9&max_bytes=8192",
      "?offset=0&max_bytes=8192",
    ]);
    expect(composer()).toBe(input);
    expect(input.value).toBe("Keep the source question");
  },
);

it("returns from a work detail to the owning Space work tab and its actual work control", async () => {
  setup();
  render(<App />);
  await ready();
  fireEvent.click(await screen.findByRole("button", { name: space.name }));
  fireEvent.click(
    await screen.findByRole("button", { name: execution.objective }),
  );
  await screen.findByRole("heading", { name: execution.objective });
  fireEvent.click(
    within(screen.getByRole("main", { name: "工作区" })).getByRole("button", {
      name: "空间",
    }),
  );
  await screen.findByRole("heading", { name: space.name });
  expect(
    screen.getByRole("tab", { name: "工作" }).getAttribute("aria-selected"),
  ).toBe("true");
  const source = await screen.findByRole("button", {
    name: execution.objective,
  });
  await waitFor(() => expect(document.activeElement).toBe(source));
});

it("switches to a real chat view while preserving drafts and workspace reading state", async () => {
  setup();
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.change(input, {
    target: { value: "Preserve my reading draft" },
  });
  const history = document.querySelector<HTMLElement>(".chat-history")!;
  Object.defineProperty(history, "scrollHeight", {
    configurable: true,
    value: 1000,
  });
  Object.defineProperty(history, "clientHeight", {
    configurable: true,
    value: 300,
  });
  const scroll = vi.spyOn(history, "scrollTo");
  scroll.mockClear();
  const navigation = screen.getByRole("navigation", { name: "主导航" });
  fireEvent.click(within(navigation).getByRole("button", { name: "资源" }));
  await screen.findByRole("heading", { name: "资源目录" });
  const workspace = screen.getByRole("main");
  workspace.scrollTop = 180;
  fireEvent.scroll(workspace);
  fireEvent.click(screen.getByRole("button", { name: "聊天" }));
  fireEvent.click(screen.getByRole("button", { name: "聊天" }));
  expect(window.location.hash).toBe("#view=chat");
  expect(screen.queryByRole("heading", { name: "资源目录" })).toBeNull();
  expect(workspace.hidden).toBe(true);
  expect(scroll).not.toHaveBeenCalled();
  expect(composer()).toBe(input);
  expect(composer().value).toBe("Preserve my reading draft");
  expect(
    within(navigation)
      .getByRole("button", { name: "聊天" })
      .getAttribute("aria-current"),
  ).toBe("true");
  expect(navigation.querySelectorAll('[aria-current="true"]')).toHaveLength(1);
  fireEvent.click(within(navigation).getByRole("button", { name: "资源" }));
  await screen.findByRole("heading", { name: "资源目录" });
  expect(screen.getByRole("main")).toBe(workspace);
  await waitFor(() => expect(workspace.scrollTop).toBe(180));
  fireEvent.click(within(navigation).getByRole("button", { name: "聊天" }));

  history.scrollTop = 10;
  fireEvent.scroll(history);
  const latest = message("new-reading-anchor", 2, "A newly committed answer");
  act(() =>
    TestEventSource.instances[0].emit("product", {
      sequence: 2,
      event_id: "reading-event",
      subject: latest.message_id,
      type: "chat.message.created",
      data: latest,
      created_at: latest.created_at,
    }),
  );
  fireEvent.click(
    await screen.findByRole("button", { name: /有新消息，回到底部/ }),
  );
  expect(document.activeElement?.id).toBe("message-new-reading-anchor");
  expect(composer().value).toBe("Preserve my reading draft");
});

it("requires restoring an archived Space before using it for new work", async () => {
  const archived = {
    ...space,
    status: "archived" as const,
    available_actions: ["restore", "delete"],
  };
  const snapshot = initialSnapshot();
  snapshot.spaces = page([archived]);
  setup(snapshot, (path) =>
    path === "/v1/spaces/research" ? Response.json(archived) : undefined,
  );
  render(<App />);
  await ready();
  fireEvent.change(screen.getByRole("combobox", { name: "空间状态" }), {
    target: { value: "archived" },
  });
  fireEvent.click(await screen.findByRole("button", { name: "恢复后使用" }));
  await screen.findByRole("button", { name: "恢复空间" });
  expect(
    screen.getByRole("tab", { name: "设置" }).getAttribute("aria-selected"),
  ).toBe("true");
  expect(
    screen.queryByRole("button", { name: "移除引用 research" }),
  ).toBeNull();
});

it("preserves the current draft and targets when the host target limit prevents another association", async () => {
  const snapshot = initialSnapshot();
  snapshot.capabilities.max_target_executions = 1;
  const other = {
    ...execution,
    execution_id: "other-target",
    objective: "A separate target",
  };
  snapshot.executions = page([execution, other]);
  setup(snapshot, (path) =>
    path === "/v1/executions/other-target" ? Response.json(other) : undefined,
  );
  render(<App />);
  await ready();
  fireEvent.change(composer(), {
    target: { value: "Keep this exact supplement" },
  });
  const cards = Array.from(
    document.querySelectorAll<HTMLElement>(".execution-card"),
  );
  fireEvent.click(within(cards[0]).getByRole("button", { name: "补充" }));
  fireEvent.click(within(cards[1]).getByRole("button", { name: "补充" }));
  expect(
    screen.getByText("最多关联 1 项工作，请先移除已有目标。"),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: `取消补充：${execution.objective}` }),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: `取消补充：${other.objective}` }),
  ).toBeNull();
  expect(composer().value).toBe("Keep this exact supplement");
});

function historicalResponsibility(): Message {
  return {
    ...message(
      "historical-source",
      1,
      "An older supplement awaiting its real owner",
    ),
    role: "user",
    target_execution_ids: ["work"],
    input_dispositions: [
      {
        origin_input_id: "origin",
        target_execution_id: "work",
        input_id: "input",
        revision: 1,
        state: "owned",
        outcome_refs: [],
        handback_id: null,
        execution_objective: execution.objective,
        space_name: space.name,
        reassignment_pending: false,
      },
    ],
  };
}

it("refreshes a returned responsibility beyond the fifty-message snapshot without moving its reading anchor or draft", async () => {
  const original = historicalResponsibility();
  const returned: Message = {
    ...original,
    input_dispositions: original.input_dispositions.map((value) => ({
      ...value,
      state: "returned",
      revision: 2,
      handback_id: "handback",
      reassignment_pending: true,
    })),
  };
  const history = Array.from({ length: 60 }, (_, index) =>
    message(`new-${index}`, index + 2, `Later message ${index}`),
  );
  const snapshot = initialSnapshot();
  snapshot.messages = page([original]);
  const state = setup(snapshot, (path) =>
    new URL(path, "http://localhost").pathname === "/v1/chat/messages"
      ? Response.json({ ...page([returned], 62), total: 61 })
      : undefined,
  );
  render(<App />);
  await ready();
  fireEvent.change(composer(), {
    target: { value: "Preserve this unsent follow-up" },
  });
  const source = TestEventSource.instances[0];
  act(() =>
    history.forEach((value, index) =>
      source.emit("product", {
        sequence: index + 2,
        event_id: `later-${index}`,
        subject: value.message_id,
        type: "chat.message.created",
        data: value,
        created_at: value.created_at,
      }),
    ),
  );
  state.snapshot = {
    ...snapshot,
    snapshot_sequence: 61,
    messages: { ...page(history.slice(-50), 61), total: 61, next_cursor: 12 },
  };
  await waitFor(() =>
    expect(document.querySelectorAll(".message").length).toBe(61),
  );
  const container = document.querySelector<HTMLElement>(".chat-history")!;
  const row = document.getElementById("message-historical-source")!;
  Object.defineProperty(container, "scrollHeight", {
    configurable: true,
    value: 1000,
  });
  Object.defineProperty(container, "clientHeight", {
    configurable: true,
    value: 300,
  });
  vi.spyOn(container, "getBoundingClientRect").mockReturnValue(
    new DOMRect(0, 100, 440, 300),
  );
  vi.spyOn(row, "getBoundingClientRect").mockReturnValue(
    new DOMRect(0, 113, 400, 60),
  );
  container.scrollTop = 240;
  fireEvent.scroll(container);
  state.snapshot = { ...state.snapshot, snapshot_sequence: 62 };
  act(() =>
    source.emit("product", {
      sequence: 62,
      event_id: "returned",
      subject: "work",
      type: "input.returned",
      data: { handback_id: "handback" },
      created_at: "2026-11-07T00:01:00Z",
    }),
  );
  await screen.findByRole("button", {
    name: /研究空间 · 研究 Agent 架构等待安排任务/,
  });
  expect(screen.queryByText("由此任务处理")).toBeNull();
  expect(container.scrollTop).toBe(240);
  expect(
    row.getBoundingClientRect().top - container.getBoundingClientRect().top,
  ).toBe(13);
  expect(composer().value).toBe("Preserve this unsent follow-up");
});

it("discards a late historical responsibility page after a new cache generation has replaced its source", async () => {
  const original = historicalResponsibility();
  const history = Array.from({ length: 60 }, (_, index) =>
    message(`new-${index}`, index + 2, `Later message ${index}`),
  );
  const snapshot = initialSnapshot();
  snapshot.messages = page([original]);
  let releasePage: ((response: Response) => void) | null = null;
  const state = setup(snapshot, (path) =>
    new URL(path, "http://localhost").pathname === "/v1/chat/messages"
      ? new Promise<Response>((resolve) => {
          releasePage = resolve;
        })
      : undefined,
  );
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.change(input, {
    target: { value: "Keep the new-generation draft" },
  });
  const source = TestEventSource.instances[0];
  act(() =>
    history.forEach((value, index) =>
      source.emit("product", {
        sequence: index + 2,
        event_id: `later-${index}`,
        subject: value.message_id,
        type: "chat.message.created",
        data: value,
        created_at: value.created_at,
      }),
    ),
  );
  state.snapshot = {
    ...snapshot,
    snapshot_sequence: 62,
    messages: { ...page(history.slice(-50), 62), total: 61, next_cursor: 12 },
  };
  act(() =>
    source.emit("product", {
      sequence: 62,
      event_id: "returned",
      subject: "work",
      type: "input.returned",
      data: { handback_id: "handback" },
      created_at: "2026-11-07T00:01:00Z",
    }),
  );
  await waitFor(() => expect(releasePage).not.toBeNull());
  state.snapshot = {
    ...snapshot,
    snapshot_sequence: 1000,
    messages: page(
      [message("fresh-source", 100, "Fresh verified generation")],
      1000,
    ),
  };
  act(() => source.emit("resync"));
  await screen.findByText("Fresh verified generation");
  await act(async () => {
    if (releasePage)
      releasePage(Response.json({ ...page([original], 62), total: 61 }));
  });
  expect(screen.queryByText(original.body)).toBeNull();
  expect(screen.queryByText("由此任务处理")).toBeNull();
  expect(screen.getByText("Fresh verified generation")).toBeTruthy();
  expect(composer()).toBe(input);
  expect(input.value).toBe("Keep the new-generation draft");
});

it("returns from a chat artifact preview to its source button without changing the draft", async () => {
  window.location.hash = "#view=chat";
  const artifact: Message["artifacts"][number] = {
    artifact_id: "chat-report",
    created_at: "2026-11-07T00:00:00Z",
    name: "研究报告",
    mime: "text/plain",
    size: 6,
    digest: "report-digest",
    owner_execution_id: "work",
    graph_run_id: null,
    node_attempt_id: null,
    previewable: true,
    downloadable: true,
    deletion_state: "active",
  };
  const snapshot = initialSnapshot();
  snapshot.messages = page([
    { ...message("report-message", 1, "报告已完成。"), artifacts: [artifact] },
  ]);
  setup(snapshot, (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/artifacts/chat-report")
      return Response.json(artifact);
    if (url.pathname === "/v1/artifacts/chat-report/text")
      return Response.json({
        id: artifact.artifact_id,
        text: "source",
        offset: 0,
        next_offset: null,
        total_bytes: artifact.size,
        read_sequence: 1,
        read_scope: "product",
      });
  });
  render(<App />);
  await ready();
  const input = composer();
  fireEvent.change(input, { target: { value: "Keep my follow-up question" } });
  const source = screen.getByRole("button", { name: /研究报告.*查看产物/ });
  source.focus();
  fireEvent.click(source);
  await screen.findByRole("heading", { name: artifact.name });
  fireEvent.click(screen.getByRole("button", { name: "返回来源" }));
  await waitFor(() => expect(document.activeElement).toBe(source));
  expect(window.location.hash).toBe("#view=chat");
  expect(screen.queryByRole("main", { name: "工作区" })).toBeNull();
  expect(composer()).toBe(input);
  expect(input.value).toBe("Keep my follow-up question");
});

it("closes an open resource dialog when browser navigation returns to chat while preserving the resource search", async () => {
  window.location.hash = "#view=chat";
  const resource = {
    kind: "tools",
    code: "review",
    name: "Review",
    description: "Checks sources",
    available: true,
    disabled_reason: null,
    configuration_location: null,
    metadata: {},
    configuration: {},
    input_schema: null,
    output_schema: null,
    content: null,
    files: [],
  };
  setup(initialSnapshot(), (path) => {
    const url = new URL(path, "http://localhost");
    if (url.pathname === "/v1/resources")
      return Response.json(page([resource]));
    if (url.pathname === "/v1/resources/tools/review")
      return Response.json(resource);
  });
  render(<App />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "资源" }));
  const search = await screen.findByRole("textbox", { name: "搜索资源" });
  fireEvent.change(search, { target: { value: "review" } });
  fireEvent.click(await screen.findByRole("button", { name: /Review/ }));
  await screen.findByRole("dialog", { name: resource.name });
  openRoute("#view=chat");
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { hidden: true })).toBeNull(),
  );
  expect(document.activeElement).toBe(composer());
  fireEvent.click(screen.getByRole("button", { name: "资源" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(
    (screen.getByRole("textbox", { name: "搜索资源" }) as HTMLInputElement)
      .value,
  ).toBe("review");
});
