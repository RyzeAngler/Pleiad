import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import {
  ArrowClockwise,
  CaretDown,
  CaretRight,
  ChatCircle,
  Check,
  GearSix,
  MagnifyingGlass,
  Planet,
  Plus,
  Stack,
  Trash,
  X,
} from "@phosphor-icons/react";
import type { Conversation, Space } from "./types";
import "./Navigation.css";

export function Navigation({
  spaces,
  space,
  conversations,
  conversationId,
  loading,
  error,
  onRetry,
  onSelectSpace,
  onSelectConversation,
  onNewTask,
  resourcesActive = false,
  onSettings,
  onDeleteConversation,
  deletionDisabled = false,
}: {
  spaces: Space[];
  space: Space | undefined;
  conversations: Conversation[];
  conversationId: string | null;
  loading: boolean;
  error: string;
  onRetry: () => void;
  onSelectSpace: (id: string | null) => void;
  onSelectConversation: (task: Conversation) => void;
  onNewTask: (spaceId: string) => void;
  resourcesActive?: boolean;
  onSettings: (section: "space" | "resources") => void;
  onDeleteConversation?: (task: Conversation) => void;
  deletionDisabled?: boolean;
}) {
  const [panel, setPanel] = useState<"spaces" | "search" | null>(null);
  const [query, setQuery] = useState("");
  const [spaceAnchor, setSpaceAnchor] = useState({ left: 0, top: 0 });
  const dialog = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const restoreFocus = useRef(true);
  const currentTask = conversations.find(
    (task) => task.conversation_id === conversationId,
  );
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const matchingTasks = conversations
    .filter((task) => {
      const owner = spaces.find((item) => item.space_id === task.space_id);
      return `${task.title} ${owner?.name ?? ""}`
        .toLocaleLowerCase()
        .includes(normalizedQuery);
    })
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));

  useEffect(() => {
    if (!panel) return;
    restoreFocus.current = true;
    const previous = opener.current;
    const focusable = () =>
      Array.from(
        dialog.current?.querySelectorAll<HTMLElement>(
          'button, input, [tabindex="0"]',
        ) ?? [],
      ).filter((element) => !element.matches(":disabled"));
    if (panel === "search") search.current?.focus();
    else
      (
        dialog.current?.querySelector<HTMLElement>('[aria-current="true"]') ??
        focusable()[0]
      )?.focus();

    function keydown(event: globalThis.KeyboardEvent) {
      if (event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setPanel(null);
      } else if (
        panel === "spaces" &&
        ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)
      ) {
        event.preventDefault();
        const elements = focusable();
        const current = elements.findIndex(
          (element) => element === document.activeElement,
        );
        const next =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? elements.length - 1
              : (current +
                  (event.key === "ArrowDown" ? 1 : -1) +
                  elements.length) %
                elements.length;
        elements[next]?.focus();
      } else if (event.key === "Tab") {
        const elements = focusable();
        const first = elements[0];
        const last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    }
    function focusin(event: FocusEvent) {
      if (
        event.target instanceof Node &&
        !dialog.current?.contains(event.target)
      )
        focusable()[0]?.focus();
    }
    function closeOnResize() {
      setPanel(null);
    }
    window.addEventListener("resize", closeOnResize);
    document.addEventListener("keydown", keydown);
    document.addEventListener("focusin", focusin);
    return () => {
      window.removeEventListener("resize", closeOnResize);
      document.removeEventListener("keydown", keydown);
      document.removeEventListener("focusin", focusin);
      if (restoreFocus.current && previous?.isConnected) previous.focus();
      else
        queueMicrotask(() => {
          if (document.activeElement === document.body && previous?.isConnected)
            previous.focus();
        });
    };
  }, [panel]);

  function reveal(mode: "spaces" | "search", trigger: HTMLElement) {
    opener.current = trigger;
    setQuery("");
    if (mode === "spaces") {
      const bounds = trigger.getBoundingClientRect();
      setSpaceAnchor({
        left: Math.max(16, Math.min(bounds.left, window.innerWidth - 256)),
        top: bounds.bottom + 6,
      });
    }
    setPanel(mode);
  }

  function navigate(action: () => void) {
    restoreFocus.current = false;
    setPanel(null);
    action();
  }

  function focusTask(event: KeyboardEvent<HTMLElement>) {
    if (event.nativeEvent.isComposing) return;
    const buttons = Array.from(
      dialog.current?.querySelectorAll<HTMLButtonElement>(".navigation-task") ??
        [],
    );
    const current = buttons.findIndex((button) => button === event.target);
    if (event.key === "ArrowDown" && buttons.length) {
      event.preventDefault();
      buttons[(current + 1) % buttons.length].focus();
    } else if (event.key === "ArrowUp" && buttons.length) {
      event.preventDefault();
      if (current === 0) search.current?.focus();
      else buttons[current < 0 ? buttons.length - 1 : current - 1].focus();
    } else if (event.key === "Enter" && event.target === search.current) {
      event.preventDefault();
      if (matchingTasks.length)
        navigate(() => onSelectConversation(matchingTasks[0]));
    }
  }

  return (
    <header className="navigation">
      <nav className="navigation-bar" aria-label="主导航">
        <button
          className="navigation-brand"
          aria-label="返回工作区"
          onClick={() => navigate(() => onSelectSpace(null))}
        >
          <Planet size={34} weight="fill" aria-hidden="true" />
          <span>Weave</span>
        </button>
        <div className="navigation-actions">
          <button
            className="navigation-action"
            aria-label="搜索任务"
            title="搜索任务"
            aria-haspopup="dialog"
            aria-expanded={panel === "search"}
            aria-controls={panel === "search" ? "navigation-panel" : undefined}
            onClick={(event) => reveal("search", event.currentTarget)}
          >
            <MagnifyingGlass size={22} aria-hidden="true" />
            <span>搜索</span>
          </button>
          <button
            className="navigation-action"
            aria-label="资源库"
            title="资源库"
            aria-current={resourcesActive ? "page" : undefined}
            onClick={() => navigate(() => onSettings("resources"))}
          >
            <Stack size={22} aria-hidden="true" />
            <span>资源库</span>
          </button>
        </div>
      </nav>
      {space && (
        <div
          className={`navigation-space-bar${conversationId ? " navigation-space-bar-task" : ""}`}
        >
          <button
            className="navigation-context"
            aria-label="切换空间"
            aria-haspopup="dialog"
            aria-expanded={panel === "spaces"}
            aria-controls={panel === "spaces" ? "navigation-panel" : undefined}
            onClick={(event) => reveal("spaces", event.currentTarget)}
          >
            <span className="navigation-context-space">{space.name}</span>
            <CaretDown size={15} aria-hidden="true" />
          </button>
          {currentTask && (
            <span className="navigation-task-title" title={currentTask.title}>
              {currentTask.title}
            </span>
          )}
          <div className="navigation-space-actions">
            {currentTask && onDeleteConversation && (
              <button
                className="icon-button delete-action"
                aria-label="删除任务"
                disabled={deletionDisabled}
                title="删除任务"
                onClick={() =>
                  navigate(() => onDeleteConversation(currentTask))
                }
              >
                <Trash size={18} aria-hidden="true" />
              </button>
            )}
            <button
              className="navigation-space-settings"
              onClick={() => navigate(() => onSettings("space"))}
            >
              <GearSix size={18} aria-hidden="true" /> 空间设置
            </button>
            {conversationId && (
              <button
                className="navigation-new-task"
                onClick={() => navigate(() => onNewTask(space.space_id))}
              >
                <Plus size={18} aria-hidden="true" /> 新任务
              </button>
            )}
          </div>
        </div>
      )}
      {panel && (
        <div className={`navigation-overlay navigation-overlay-${panel}`}>
          <button
            className="navigation-backdrop"
            tabIndex={-1}
            aria-label="关闭导航背景"
            onClick={() => setPanel(null)}
          />
          <div
            ref={dialog}
            id="navigation-panel"
            className={`navigation-panel navigation-panel-${panel}`}
            style={
              panel === "spaces"
                ? {
                    left: spaceAnchor.left,
                    top: spaceAnchor.top,
                    maxHeight: `calc(100dvh - ${spaceAnchor.top + 16}px)`,
                  }
                : undefined
            }
            role="dialog"
            aria-modal="true"
            aria-label={panel === "search" ? "搜索任务" : "切换空间"}
          >
            {panel === "spaces" ? (
              <>
                <nav className="navigation-spaces" aria-label="空间列表">
                  {spaces.map((item) => (
                    <button
                      key={item.space_id}
                      aria-current={
                        item.space_id === space?.space_id ? "true" : undefined
                      }
                      onClick={() =>
                        navigate(() => onSelectSpace(item.space_id))
                      }
                    >
                      <span>{item.name}</span>
                      {item.space_id === space?.space_id && (
                        <Check size={16} aria-hidden="true" />
                      )}
                    </button>
                  ))}
                  {!spaces.length && (
                    <p className="navigation-empty">暂无空间。</p>
                  )}
                </nav>
              </>
            ) : (
              <>
                <div className="navigation-panel-heading">
                  <h2>搜索任务</h2>
                  <button
                    className="navigation-close"
                    aria-label="关闭导航"
                    onClick={() => setPanel(null)}
                  >
                    <X size={20} aria-hidden="true" />
                  </button>
                </div>
                <div className="navigation-search-field">
                  <MagnifyingGlass size={22} aria-hidden="true" />
                  <input
                    ref={search}
                    aria-label="搜索全部空间的任务"
                    placeholder="搜索任务或空间名称…"
                    autoComplete="off"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    onKeyDown={focusTask}
                  />
                  {query && (
                    <button
                      className="navigation-clear"
                      aria-label="清除搜索"
                      onClick={() => {
                        setQuery("");
                        search.current?.focus();
                      }}
                    >
                      <X size={16} aria-hidden="true" />
                    </button>
                  )}
                </div>
                <p className="navigation-search-caption">
                  {normalizedQuery ? "匹配任务" : "最近任务"}
                  <span>全部空间</span>
                </p>
                <nav
                  className="navigation-tasks"
                  aria-label="任务列表"
                  onKeyDown={focusTask}
                >
                  {matchingTasks.map((task) => (
                    <button
                      className="navigation-task"
                      key={task.conversation_id}
                      aria-current={
                        task.conversation_id === conversationId
                          ? "page"
                          : undefined
                      }
                      onClick={() => navigate(() => onSelectConversation(task))}
                    >
                      <ChatCircle size={21} aria-hidden="true" />
                      <span className="navigation-task-content">
                        <strong>{task.title}</strong>
                        <small>
                          {
                            spaces.find(
                              (item) => item.space_id === task.space_id,
                            )?.name
                          }
                        </small>
                      </span>
                      <CaretRight size={15} aria-hidden="true" />
                    </button>
                  ))}
                  {error ? (
                    <div className="navigation-empty" role="alert">
                      <p>{error}</p>
                      <button className="navigation-retry" onClick={onRetry}>
                        <ArrowClockwise size={16} aria-hidden="true" />{" "}
                        重新加载任务
                      </button>
                    </div>
                  ) : loading ? (
                    <p className="navigation-empty" role="status">
                      正在加载任务…
                    </p>
                  ) : !matchingTasks.length ? (
                    <p className="navigation-empty" role="status">
                      {normalizedQuery
                        ? "没有找到匹配的任务。"
                        : "暂无任务。进入空间即可开始。"}
                    </p>
                  ) : null}
                </nav>
                <div className="navigation-search-footer">
                  ↑ ↓ 选择任务 <span>Esc 关闭</span>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </header>
  );
}
