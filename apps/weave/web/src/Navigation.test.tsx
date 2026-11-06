// @vitest-environment jsdom
import { useState } from "react";
import { afterEach, expect, it } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { Navigation } from "./Navigation";
import type { Conversation, Space } from "./types";

afterEach(cleanup);

const spaces: Space[] = [
  { space_id: "travel", name: "旅行规划" },
  { space_id: "writing", name: "文案工作室" },
].map((space) => ({
  ...space,
  instructions: "核实来源",
  preferred_tool_codes: [],
  preferred_skill_codes: [],
  saved_graph: null,
  revision: 1,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
}));
const tasks: Conversation[] = [
  {
    conversation_id: "travel-task",
    space_id: "travel",
    title: "杭州周末行程",
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
  },
  {
    conversation_id: "writing-task",
    space_id: "writing",
    title: "京都旅行文案",
    created_at: "2026-11-02T00:00:00Z",
    updated_at: "2026-11-02T00:00:00Z",
  },
];

function Workspace({
  initialSpace = "travel",
  initialTask = "travel-task",
  initialError = "",
  loading = false,
  conversations = tasks,
}: {
  initialSpace?: string | null;
  initialTask?: string | null;
  initialError?: string;
  loading?: boolean;
  conversations?: Conversation[];
}) {
  const [spaceId, setSpaceId] = useState(initialSpace);
  const [taskId, setTaskId] = useState(initialTask);
  const [error, setError] = useState(initialError);
  const [view, setView] = useState("task");
  const space = spaces.find((item) => item.space_id === spaceId);
  const task = tasks.find((item) => item.conversation_id === taskId);
  return (
    <>
      <Navigation
        spaces={spaces}
        space={view === "task" ? space : undefined}
        conversations={conversations}
        conversationId={taskId}
        loading={loading}
        error={error}
        onRetry={() => setError("")}
        onSelectSpace={(id) => {
          setSpaceId(id);
          setTaskId(null);
          setView("task");
        }}
        onSelectConversation={(selected) => {
          setSpaceId(selected.space_id);
          setTaskId(selected.conversation_id);
          setView("task");
        }}
        onNewTask={(id) => {
          setSpaceId(id);
          setTaskId(null);
          setView("task");
        }}
        resourcesActive={view === "resources"}
        onSettings={setView}
      />
      <main>
        {view === "task" ? (
          <h1>
            {space
              ? `${space.name} / ${task?.title ?? "新任务"}`
              : "工作区首页"}
          </h1>
        ) : (
          <h1>{view === "space" ? "空间设置" : "资源库"}</h1>
        )}
        <button>页面中的操作</button>
      </main>
    </>
  );
}

it("shows new task only in an existing conversation and opens its owning space composer", () => {
  render(<Workspace />);
  fireEvent.click(screen.getByRole("button", { name: "新任务" }));
  expect(
    screen.getByRole("heading", { name: "旅行规划 / 新任务" }),
  ).toBeDefined();
  expect(screen.queryByRole("button", { name: "新任务" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "返回工作区" }));
  expect(screen.getByRole("heading", { name: "工作区首页" })).toBeDefined();
  expect(screen.queryByRole("button", { name: "新任务" })).toBeNull();
});

it("searches across spaces from a selected task and opens a result in its owning space", () => {
  render(<Workspace />);
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  const input = within(dialog).getByRole("textbox", {
    name: "搜索全部空间的任务",
  });
  expect(document.activeElement).toBe(input);
  fireEvent.change(input, { target: { value: " 文案工作室 " } });
  expect(
    within(dialog).queryByRole("button", { name: /杭州周末行程/ }),
  ).toBeNull();
  fireEvent.click(
    within(dialog).getByRole("button", { name: /京都旅行文案.*文案工作室/ }),
  );
  expect(
    screen.getByRole("heading", { name: "文案工作室 / 京都旅行文案" }),
  ).toBeDefined();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("reports unmatched search and restores all tasks when the query is cleared", () => {
  render(<Workspace initialSpace={null} initialTask={null} />);
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  const input = within(dialog).getByRole("textbox");
  fireEvent.change(input, { target: { value: "不存在" } });
  expect(within(dialog).getByRole("status").textContent).toBe(
    "没有找到匹配的任务。",
  );
  expect(
    within(dialog).queryByRole("button", { name: /京都旅行文案/ }),
  ).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "清除搜索" }));
  expect((input as HTMLInputElement).value).toBe("");
  expect(document.activeElement).toBe(input);
  expect(
    within(dialog).getAllByRole("button", {
      name: /杭州周末行程|京都旅行文案/,
    }),
  ).toHaveLength(2);
});

it.each(["Escape", "outside"])(
  "closes search with %s and returns focus to the header trigger",
  (method) => {
    render(<Workspace />);
    const trigger = screen.getByRole("button", { name: "搜索任务" });
    trigger.focus();
    fireEvent.click(trigger);
    if (method === "Escape")
      fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    else fireEvent.click(screen.getByRole("button", { name: "关闭导航背景" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  },
);

it("supports keyboard selection and keeps tab focus inside search", () => {
  render(<Workspace />);
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  const input = within(dialog).getByRole("textbox");
  const first = within(dialog).getByRole("button", { name: /京都旅行文案/ });
  const last = within(dialog).getByRole("button", { name: /杭州周末行程/ });
  fireEvent.keyDown(input, { key: "ArrowDown" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first, { key: "ArrowUp" });
  expect(document.activeElement).toBe(input);
  const close = within(dialog).getByRole("button", { name: "关闭导航" });
  close.focus();
  fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(last, { key: "Tab" });
  expect(document.activeElement).toBe(close);
  input.focus();
  fireEvent.keyDown(input, { key: "Enter" });
  expect(
    screen.getByRole("heading", { name: "文案工作室 / 京都旅行文案" }),
  ).toBeDefined();
});

it("preserves the failed directory state until the user retries", () => {
  render(<Workspace initialError="任务目录暂时不可用" />);
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  expect(within(dialog).getByRole("alert").textContent).toContain(
    "任务目录暂时不可用",
  );
  expect(within(dialog).queryByText("没有找到匹配的任务。")).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "重新加载任务" }));
  expect(within(dialog).queryByRole("alert")).toBeNull();
  expect(
    within(dialog).getByRole("button", { name: /京都旅行文案/ }),
  ).toBeDefined();
});

it.each([true, false])(
  "distinguishes loading and empty history (loading=%s)",
  (loading) => {
    render(<Workspace loading={loading} conversations={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
    expect(screen.getByRole("status").textContent).toBe(
      loading ? "正在加载任务…" : "暂无任务。进入空间即可开始。",
    );
  },
);

it("switches spaces from a list containing only spaces while settings stays outside the menu", () => {
  render(<Workspace />);
  const switcher = screen.getByRole("button", { name: "切换空间" });
  expect(switcher.textContent).toBe("旅行规划");
  expect(screen.getByText("杭州周末行程")).toBeDefined();
  fireEvent.click(switcher);
  const dialog = screen.getByRole("dialog", { name: "切换空间" });
  const choices = within(dialog).getAllByRole("button");
  expect(choices.map((choice) => choice.textContent)).toEqual([
    "旅行规划",
    "文案工作室",
  ]);
  expect(document.activeElement).toBe(choices[0]);
  expect(within(dialog).queryByRole("button", { name: "空间设置" })).toBeNull();
  expect(within(dialog).queryByRole("button", { name: "创建空间" })).toBeNull();
  fireEvent.click(choices[1]);
  expect(
    screen.getByRole("heading", { name: "文案工作室 / 新任务" }),
  ).toBeDefined();
  expect(screen.queryByRole("dialog")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "空间设置" }));
  expect(screen.getByRole("heading", { name: "空间设置" })).toBeDefined();
  expect(screen.queryByRole("button", { name: "切换空间" })).toBeNull();
});

it("moves between spaces with keyboard controls and restores focus when dismissed", () => {
  render(<Workspace />);
  const switcher = screen.getByRole("button", { name: "切换空间" });
  fireEvent.click(switcher);
  const dialog = screen.getByRole("dialog", { name: "切换空间" });
  const first = within(dialog).getByRole("button", { name: "旅行规划" });
  const last = within(dialog).getByRole("button", { name: "文案工作室" });
  fireEvent.keyDown(first, { key: "ArrowDown" });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(last, { key: "ArrowDown" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first, { key: "End" });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(last, { key: "Home" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(last, { key: "Escape" });
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(switcher);
});

it("opens the global resource library without space controls and still searches all tasks", () => {
  render(<Workspace />);
  fireEvent.click(screen.getByRole("button", { name: "资源库" }));
  expect(screen.getByRole("heading", { name: "资源库" })).toBeDefined();
  expect(
    screen.getByRole("button", { name: "资源库" }).getAttribute("aria-current"),
  ).toBe("page");
  expect(screen.queryByRole("button", { name: "切换空间" })).toBeNull();
  expect(screen.queryByRole("button", { name: "空间设置" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "搜索任务" }));
  const dialog = screen.getByRole("dialog", { name: "搜索任务" });
  expect(
    within(dialog).getAllByRole("button", {
      name: /杭州周末行程|京都旅行文案/,
    }),
  ).toHaveLength(2);
  fireEvent.click(within(dialog).getByRole("button", { name: /杭州周末行程/ }));
  expect(
    screen.getByRole("heading", { name: "旅行规划 / 杭州周末行程" }),
  ).toBeDefined();
});
