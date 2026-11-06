// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { SpaceSettings } from "./SpaceSettings";
import type { Resources, Space } from "./types";

const space: Space = {
  space_id: "travel",
  name: "旅行规划",
  instructions: "核实开放时间",
  preferred_tool_codes: [],
  preferred_skill_codes: [],
  saved_graph: { graph_id: "trip", revision: 3 },
  revision: 4,
  created_at: "2026-11-04T00:00:00Z",
  updated_at: "2026-11-04T00:00:00Z",
};
const resources: Resources = {
  models: [{ role: "default", model: "configured-model" }],
  tools: [
    { code: "web.search", name: "网页检索", description: "检索来源" },
    { code: "weather", name: "天气查询", description: "查询目的地天气" },
  ],
  skills: [{ code: "trip.plan", name: "旅行规划", description: "整理行程" }],
  workspace_path: "/workspace",
  capability_file: "/workspace/capabilities.yaml",
};
const props = {
  space,
  resources,
  onSaved: () => undefined,
  onClose: () => undefined,
  onViewGraph: () => undefined,
};
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("saves empty default instructions with the observed revision", async () => {
  let submitted: unknown;
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) !== "/v1/spaces/travel" || init?.method !== "PATCH")
        throw new Error(`Unexpected request: ${input}`);
      submitted = JSON.parse(String(init.body));
      return Response.json({ ...space, instructions: "", revision: 5 });
    },
  );
  const dirty = vi.fn();
  render(<SpaceSettings {...props} onDirtyChange={dirty} />);
  expect(screen.getByText("可选")).toBeDefined();
  expect(screen.queryByRole("heading", { name: "模型" })).toBeNull();
  fireEvent.change(screen.getByRole("textbox", { name: "默认指令" }), {
    target: { value: "" },
  });
  expect(dirty).toHaveBeenLastCalledWith(true);
  const unload = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("已保存，对后续提交生效");
  expect(submitted).toEqual({
    name: space.name,
    instructions: "",
    preferred_tool_codes: [],
    preferred_skill_codes: [],
    saved_graph: space.saved_graph,
    expected_revision: 4,
  });
  expect(dirty).toHaveBeenLastCalledWith(false);
  expect(
    (
      screen.getByRole("button", {
        name: "保存",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  const afterSave = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(afterSave);
  expect(afterSave.defaultPrevented).toBe(false);
});

it("keeps resource selections across searches and persists removals from selected chips", async () => {
  let submitted: unknown;
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) !== "/v1/spaces/travel" || init?.method !== "PATCH")
        throw new Error(`Unexpected request: ${input}`);
      submitted = JSON.parse(String(init.body));
      return Response.json({
        ...space,
        preferred_skill_codes: ["trip.plan"],
        revision: 5,
      });
    },
  );
  render(<SpaceSettings {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "选择资源" }));
  const search = screen.getByRole("textbox", { name: "搜索资源" });
  fireEvent.change(search, { target: { value: " WEB.SEARCH " } });
  expect(screen.queryByRole("checkbox", { name: /天气查询/ })).toBeNull();
  fireEvent.click(screen.getByRole("checkbox", { name: /网页检索/ }));
  fireEvent.change(search, { target: { value: "整理行程" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /旅行规划/ }));
  fireEvent.change(search, { target: { value: "不存在" } });
  expect(screen.getAllByText("没有匹配的资源")).toHaveLength(2);
  fireEvent.change(search, { target: { value: "   " } });
  expect(screen.getAllByRole("checkbox")).toHaveLength(3);
  expect(
    (screen.getByRole("checkbox", { name: /网页检索/ }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  expect(
    (screen.getByRole("checkbox", { name: /旅行规划/ }) as HTMLInputElement)
      .checked,
  ).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "完成" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "选择资源" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "移除网页检索" }));
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("已保存，对后续提交生效");
  expect(submitted).toEqual({
    name: space.name,
    instructions: space.instructions,
    preferred_tool_codes: [],
    preferred_skill_codes: ["trip.plan"],
    saved_graph: space.saved_graph,
    expected_revision: 4,
  });
});

it("prevents empty and unchanged names and saves the trimmed changed name", async () => {
  let submitted: unknown;
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) !== "/v1/spaces/travel" || init?.method !== "PATCH")
        throw new Error(`Unexpected request: ${input}`);
      submitted = JSON.parse(String(init.body));
      return Response.json({ ...space, name: "周末旅行", revision: 5 });
    },
  );
  render(<SpaceSettings {...props} />);
  const save = screen.getByRole("button", {
    name: "保存",
  }) as HTMLButtonElement;
  const name = screen.getByRole("textbox", {
    name: "空间名称",
  }) as HTMLInputElement;
  expect(save.disabled).toBe(true);
  fireEvent.change(name, { target: { value: "   " } });
  expect(save.disabled).toBe(true);
  expect(screen.getByText("请输入空间名称")).toBeDefined();
  fireEvent.change(name, { target: { value: " 旅行规划 " } });
  expect(save.disabled).toBe(true);
  fireEvent.change(name, { target: { value: " 周末旅行 " } });
  fireEvent.click(save);
  await screen.findByText("已保存，对后续提交生效");
  expect(name.value).toBe("周末旅行");
  expect(submitted).toMatchObject({ name: "周末旅行", expected_revision: 4 });
});

it("keeps a failed draft editable and locks form actions while saving", async () => {
  let finishSave: (response: Response) => void = () => {
    throw new Error("Save not started");
  };
  const pending = new Promise<Response>((resolve) => {
    finishSave = resolve;
  });
  let attempts = 0;
  vi.stubGlobal("fetch", async () => {
    attempts++;
    return attempts === 1
      ? pending
      : Response.json({ ...space, instructions: "保留住宿预算", revision: 5 });
  });
  render(<SpaceSettings {...props} />);
  const instructions = screen.getByRole("textbox", {
    name: "默认指令",
  }) as HTMLTextAreaElement;
  fireEvent.change(instructions, { target: { value: "保留住宿预算" } });
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(instructions.disabled).toBe(true);
  for (const label of ["取消默认编排", "选择资源", "返回", "查看", "保存中…"])
    expect(
      (
        screen.getByRole("button", {
          name: label,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  await act(async () => {
    finishSave(Response.json({ detail: "配置暂时无法写入" }, { status: 503 }));
  });
  const error = await screen.findByRole("alert");
  expect(error.textContent).toBe("配置暂时无法写入");
  await waitFor(() => expect(document.activeElement).toBe(error));
  expect(instructions.disabled).toBe(false);
  expect(instructions.value).toBe("保留住宿预算");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("已保存，对后续提交生效");
  expect(screen.queryByRole("alert")).toBeNull();
});

it("opens the exact saved graph revision and saves an explicitly removed reference", async () => {
  let submitted: unknown;
  vi.stubGlobal(
    "fetch",
    async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) !== "/v1/spaces/travel" || init?.method !== "PATCH")
        throw new Error(`Unexpected request: ${input}`);
      submitted = JSON.parse(String(init.body));
      return Response.json({ ...space, saved_graph: null, revision: 5 });
    },
  );
  const open = vi.fn();
  render(<SpaceSettings {...props} onViewGraph={open} />);
  fireEvent.click(screen.getByRole("button", { name: "查看" }));
  expect(open).toHaveBeenCalledExactlyOnceWith({
    graph_id: "trip",
    revision: 3,
  });
  fireEvent.click(screen.getByRole("button", { name: "取消默认编排" }));
  expect(screen.getByText("尚未保存编排")).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await screen.findByText("已保存，对后续提交生效");
  expect(submitted).toMatchObject({ saved_graph: null, expected_revision: 4 });
});

it("allows instruction editing with an unavailable catalog and preserves unknown resource selections", () => {
  render(
    <SpaceSettings
      {...props}
      space={{ ...space, preferred_tool_codes: ["removed.tool"] }}
      resources={null}
    />,
  );
  expect(screen.getByText("资源目录暂不可用")).toBeDefined();
  expect(
    (screen.getByRole("button", { name: "选择资源" }) as HTMLButtonElement)
      .disabled,
  ).toBe(true);
  expect(
    (screen.getByRole("textbox", { name: "默认指令" }) as HTMLTextAreaElement)
      .disabled,
  ).toBe(false);
  expect(screen.getByText("removed.tool")).toBeDefined();
  fireEvent.click(screen.getByRole("button", { name: "移除removed.tool" }));
  expect(screen.queryByText("removed.tool")).toBeNull();
  expect(
    (
      screen.getByRole("button", {
        name: "保存",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(false);
});
