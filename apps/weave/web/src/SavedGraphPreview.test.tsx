// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { SavedGraphPreview } from "./SavedGraphPreview";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows the saved graph without submitting an execution and exposes node instructions", async () => {
  const requests: string[] = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
    requests.push(String(input));
    return Response.json({
      graph_id: "trip",
      revision: 2,
      objective: "两日旅行规划",
      edges: [],
      nodes: [
        {
          node_id: "plan",
          objective: "规划路线",
          prompt: "按公共交通组织路线",
          model_role: "text",
          tool_codes: ["search"],
          skill_codes: [],
          input_schema: {},
          output_schema: {},
        },
      ],
    });
  });
  const onClose = vi.fn();
  render(
    <SavedGraphPreview
      spaceId="space-a"
      reference={{ graph_id: "trip", revision: 2 }}
      resources={null}
      onClose={onClose}
    />,
  );
  const node = await screen.findByTestId("rf__node-plan");
  fireEvent.click(node);
  expect(
    screen.getByRole("complementary", { name: "节点指令" }).textContent,
  ).toContain("按公共交通组织路线");
  expect(requests).toEqual(["/v1/spaces/space-a/saved-graph"]);
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce();
});

it("reports a changed saved graph instead of displaying a different revision", async () => {
  vi.stubGlobal("fetch", async () =>
    Response.json({
      graph_id: "trip",
      revision: 3,
      objective: "另一版本",
      edges: [],
      nodes: [],
    }),
  );
  render(
    <SavedGraphPreview
      spaceId="space-a"
      reference={{ graph_id: "trip", revision: 2 }}
      resources={null}
      onClose={() => undefined}
    />,
  );
  expect((await screen.findByRole("alert")).textContent).toContain(
    "空间编排已更新",
  );
  expect(screen.queryByText("另一版本")).toBeNull();
  expect(screen.queryByRole("button", { name: "重新加载" })).toBeNull();
});

it("retries a failed graph read and restores the preview", async () => {
  let attempts = 0;
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("fetch", async () =>
    ++attempts === 1
      ? Response.json({ detail: "读取失败" }, { status: 503 })
      : Response.json({
          graph_id: "trip",
          revision: 2,
          objective: "重试后的编排",
          edges: [],
          nodes: [],
        }),
  );
  render(
    <SavedGraphPreview
      spaceId="space-a"
      reference={{ graph_id: "trip", revision: 2 }}
      resources={null}
      onClose={() => undefined}
    />,
  );
  expect((await screen.findByRole("alert")).textContent).toContain("读取失败");
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByText("重试后的编排");
  expect(screen.queryByRole("alert")).toBeNull();
});
