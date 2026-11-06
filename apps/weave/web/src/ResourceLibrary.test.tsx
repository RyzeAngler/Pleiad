// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ResourceLibrary } from "./ResourceLibrary";
import type { Resources } from "./types";

const resources: Resources = {
  models: [{ role: "text", model: "configured-model" }],
  tools: [
    { code: "web.search", name: "网页检索", description: "检索可引用的来源" },
    { code: "weather", name: "天气查询", description: "查询目的地天气" },
  ],
  skills: [{ code: "trip.plan", name: "旅行规划", description: "整理行程" }],
  workspace_path: "/workspace",
  capability_file: "/workspace/capabilities.yaml",
};
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("filters actual resource metadata and opens details without space preferences", () => {
  render(<ResourceLibrary resources={resources} onClose={() => undefined} />);
  expect(screen.queryByRole("textbox", { name: "默认指令" })).toBeNull();
  expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
  const query = screen.getByRole("textbox", { name: "搜索工具" });
  fireEvent.change(query, { target: { value: " WEB.SEARCH " } });
  expect(screen.queryByText("天气查询")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看网页检索" }));
  expect(screen.getByText("检索可引用的来源")).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "收起网页检索" }));
  expect(screen.queryByText("检索可引用的来源")).toBeNull();
  fireEvent.change(query, { target: { value: "目的地天气" } });
  expect(screen.getByText("天气查询")).toBeDefined();
  fireEvent.change(query, { target: { value: "不存在" } });
  expect(screen.getByText("没有匹配的工具")).toBeDefined();
});

it("switches model tool and skill catalogs with keyboard navigation and resets search", () => {
  render(<ResourceLibrary resources={resources} onClose={() => undefined} />);
  fireEvent.change(screen.getByRole("textbox", { name: "搜索工具" }), {
    target: { value: "不存在" },
  });
  fireEvent.click(screen.getByRole("tab", { name: "Skills" }));
  expect(screen.getByText("旅行规划")).toBeDefined();
  expect(
    (screen.getByRole("textbox", { name: "搜索技能" }) as HTMLInputElement)
      .value,
  ).toBe("");
  fireEvent.keyDown(screen.getByRole("tab", { name: "Skills" }), {
    key: "ArrowRight",
  });
  expect(screen.getByText("configured-model")).toBeDefined();
  expect(screen.getByText("text")).toBeDefined();
  expect(document.activeElement).toBe(
    screen.getByRole("tab", { name: "模型" }),
  );
  fireEvent.keyDown(screen.getByRole("tab", { name: "模型" }), { key: "End" });
  expect(screen.getByText("旅行规划")).toBeDefined();
});

it("shows actual configuration paths and copies the chosen path", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const close = vi.fn();
  render(<ResourceLibrary resources={resources} onClose={close} />);
  fireEvent.click(screen.getByRole("button", { name: "配置位置" }));
  expect(screen.getByText("/workspace/capabilities.yaml")).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "复制能力清单路径" }));
  await screen.findByText("路径已复制");
  expect(writeText).toHaveBeenCalledExactlyOnceWith(
    "/workspace/capabilities.yaml",
  );
  fireEvent.click(screen.getByRole("button", { name: "配置位置" }));
  expect(screen.queryByText("/workspace/capabilities.yaml")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "返回" }));
  expect(close).toHaveBeenCalledOnce();
});

it("reports a clipboard failure without claiming the path was copied", async () => {
  vi.stubGlobal("navigator", {
    clipboard: {
      writeText: vi.fn().mockRejectedValue(new Error("Permission denied")),
    },
  });
  render(<ResourceLibrary resources={resources} onClose={() => undefined} />);
  fireEvent.click(screen.getByRole("button", { name: "配置位置" }));
  fireEvent.click(screen.getByRole("button", { name: "复制工作区路径" }));
  expect((await screen.findByRole("alert")).textContent).toContain("复制失败");
  expect(screen.queryByText("路径已复制")).toBeNull();
});

it("offers a real retry for an unavailable catalog and distinguishes loading and empty catalogs", () => {
  const retry = vi.fn();
  const { rerender } = render(
    <ResourceLibrary
      resources={null}
      onClose={() => undefined}
      onRetry={retry}
    />,
  );
  expect(
    (screen.getByRole("textbox", { name: "搜索工具" }) as HTMLInputElement)
      .disabled,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(retry).toHaveBeenCalledOnce();
  rerender(
    <ResourceLibrary
      resources={null}
      loading
      onClose={() => undefined}
      onRetry={retry}
    />,
  );
  expect(screen.getByText("正在加载资源…")).toBeDefined();
  expect(screen.queryByRole("button", { name: "重新加载" })).toBeNull();
  rerender(
    <ResourceLibrary
      resources={{ ...resources, tools: [] }}
      onClose={() => undefined}
    />,
  );
  expect(screen.getByText("暂无工具")).toBeDefined();
});
