import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowRight, ArrowUp, CirclesThree, X } from "@phosphor-icons/react";
import { api, errorMessage, isAbort } from "./api";
import type { Resources, Space, SpaceDraft, SpaceDraftMessage } from "./types";

export function SpaceCreator({
  resources,
  onCreated,
  onClose,
}: {
  resources: Resources | null;
  onCreated: (space: Space) => void;
  onClose: () => void;
}) {
  const [messages, setMessages] = useState<SpaceDraftMessage[]>([]);
  const [draft, setDraft] = useState<SpaceDraft | null>(null);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const panel = useRef<HTMLElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const request = useRef<AbortController | null>(null);
  const closeRef = useRef(onClose);
  const creatingRef = useRef(creating);
  closeRef.current = onClose;
  creatingRef.current = creating;
  useEffect(() => {
    const previous = document.activeElement;
    composer.current?.focus();
    function keys(event: KeyboardEvent) {
      if (event.isComposing) return;
      if (event.key === "Escape" && !creatingRef.current) closeRef.current();
      if (event.key !== "Tab") return;
      const items = Array.from(
        panel.current?.querySelectorAll<HTMLElement>(
          "button, input, textarea, summary",
        ) ?? [],
      ).filter(
        (element) =>
          !element.matches(":disabled") &&
          (!element.closest("details:not([open])") ||
            element.tagName === "SUMMARY"),
      );
      const first = items[0];
      const last = items.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
    function focus(event: FocusEvent) {
      if (
        event.target instanceof Node &&
        !panel.current?.contains(event.target)
      ) {
        panel.current
          ?.querySelector<HTMLElement>("button:not(:disabled)")
          ?.focus();
      }
    }
    document.addEventListener("keydown", keys);
    document.addEventListener("focusin", focus);
    return () => {
      request.current?.abort();
      document.removeEventListener("keydown", keys);
      document.removeEventListener("focusin", focus);
      if (previous instanceof HTMLElement && previous.isConnected)
        previous.focus();
    };
  }, []);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [messages, pending]);
  useEffect(() => {
    if (!pending) composer.current?.focus();
  }, [pending]);

  async function describe(event?: FormEvent) {
    event?.preventDefault();
    if (!input.trim() || pending || creating) return;
    const next: SpaceDraftMessage[] = [
      ...messages,
      { role: "user", content: input.trim() },
    ];
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setError("");
    try {
      const result = await api.draftSpace(next, draft, controller.signal);
      if (controller.signal.aborted) return;
      setMessages([...next, { role: "assistant", content: result.reply }]);
      setDraft(result.draft);
      setInput("");
    } catch (failure) {
      if (!isAbort(failure) && !controller.signal.aborted)
        setError(errorMessage(failure));
    } finally {
      if (!controller.signal.aborted) {
        setPending(false);
      }
    }
  }
  async function create() {
    if (!draft || !draft.name.trim() || pending || creating || input.trim())
      return;
    setCreating(true);
    setError("");
    try {
      onCreated(
        await api.createSpace({
          ...draft,
          name: draft.name.trim(),
          instructions: draft.instructions.trim(),
        }),
      );
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setCreating(false);
    }
  }
  function toggle(
    field: "preferred_tool_codes" | "preferred_skill_codes",
    code: string,
  ) {
    if (!draft) return;
    setDraft({
      ...draft,
      [field]: draft[field].includes(code)
        ? draft[field].filter((item) => item !== code)
        : [...draft[field], code],
    });
  }
  return (
    <div
      className="modal-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget && !creating) onClose();
      }}
    >
      <section
        ref={panel}
        className="space-creator"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-space-title"
      >
        <header className="creator-header">
          <div>
            <h2 id="create-space-title">创建空间</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭创建空间"
            disabled={creating}
            onClick={onClose}
          >
            <X />
          </button>
        </header>
        <div className="creator-body">
          <section className="creator-conversation" aria-label="空间创建对话">
            <div className="creator-messages" aria-live="polite">
              {!messages.length && !pending && (
                <div className="creator-intro">
                  <CirclesThree weight="fill" />
                  <h3>从用途开始</h3>
                  <p>
                    例如：建一个旅游规划空间，默认从杭州出发，优先公共交通。
                  </p>
                </div>
              )}
              {messages.map((message, index) => (
                <article
                  key={index}
                  className={`message ${message.role === "user" ? "user-message" : "assistant-message"}`}
                >
                  <div className="message-author">
                    {message.role === "user" ? "你" : "Weave"}
                  </div>
                  <p>{message.content}</p>
                </article>
              ))}
              {pending && (
                <>
                  <article className="message user-message">
                    <div className="message-author">你</div>
                    <p>{input.trim()}</p>
                  </article>
                  <p className="working-indicator">
                    <span className="status-dot running" />
                    正在整理空间配置…
                  </p>
                </>
              )}
              <div ref={end} />
            </div>
            <form
              className="composer"
              onSubmit={(event) => void describe(event)}
            >
              <textarea
                ref={composer}
                aria-label="描述空间用途"
                placeholder={draft ? "补充用途或默认指令…" : "描述空间用途…"}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                rows={3}
                maxLength={16000}
                disabled={pending || creating}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    !event.shiftKey &&
                    !event.nativeEvent.isComposing
                  ) {
                    event.preventDefault();
                    void describe();
                  }
                }}
              />
              <div className="composer-bottom">
                <span className="muted">
                  {pending ? "正在生成草稿" : "Enter 发送 · Shift + Enter 换行"}
                </span>
                <button
                  className="send-button"
                  aria-label="整理空间配置"
                  disabled={!input.trim() || pending || creating}
                >
                  <ArrowUp />
                </button>
              </div>
            </form>
          </section>
          <section className="space-preview" aria-label="空间预览">
            <h3>空间预览</h3>
            {draft ? (
              <>
                <label>
                  名称
                  <input
                    aria-label="空间名称"
                    maxLength={120}
                    value={draft.name}
                    disabled={pending || creating}
                    onChange={(event) =>
                      setDraft({ ...draft, name: event.target.value })
                    }
                  />
                </label>
                <label>
                  <span>
                    默认指令 <small className="muted">可选</small>
                  </span>
                  <textarea
                    aria-label="默认指令"
                    maxLength={20000}
                    rows={7}
                    value={draft.instructions}
                    disabled={pending || creating}
                    onChange={(event) =>
                      setDraft({ ...draft, instructions: event.target.value })
                    }
                  />
                </label>
                <div className="preview-resources">
                  <h4>优先使用的资源</h4>
                  <div className="resource-chips">
                    {[
                      ...draft.preferred_tool_codes,
                      ...draft.preferred_skill_codes,
                    ].map((code) => (
                      <span key={code}>{code}</span>
                    ))}
                  </div>
                  {resources && (
                    <details>
                      <summary>调整资源偏好</summary>
                      {(["tools", "skills"] as const).map((kind) => (
                        <fieldset key={kind} disabled={pending || creating}>
                          <legend>
                            {kind === "tools" ? "Tools" : "Skills"}
                          </legend>
                          {resources[kind].length ? (
                            resources[kind].map((resource) => (
                              <label
                                className="resource-choice"
                                key={resource.code}
                              >
                                <input
                                  type="checkbox"
                                  checked={draft[
                                    kind === "tools"
                                      ? "preferred_tool_codes"
                                      : "preferred_skill_codes"
                                  ].includes(resource.code)}
                                  onChange={() =>
                                    toggle(
                                      kind === "tools"
                                        ? "preferred_tool_codes"
                                        : "preferred_skill_codes",
                                      resource.code,
                                    )
                                  }
                                />
                                <span>
                                  {resource.name}
                                  <small>{resource.code}</small>
                                </span>
                              </label>
                            ))
                          ) : (
                            <p className="muted">暂无已配置资源</p>
                          )}
                        </fieldset>
                      ))}
                    </details>
                  )}
                </div>
              </>
            ) : (
              <div className="preview-empty">
                <p className="muted">空间草稿将在这里显示</p>
              </div>
            )}
          </section>
        </div>
        {error && (
          <p className="creator-error" role="alert">
            {error}
          </p>
        )}
        <footer className="creator-footer">
          <p className="muted">
            {input.trim() && draft ? "请先发送补充内容，再创建空间。" : ""}
          </p>
          <button
            className="primary-button"
            onClick={() => void create()}
            disabled={
              !draft?.name.trim() ||
              pending ||
              creating ||
              Boolean(input.trim())
            }
          >
            {creating ? "正在创建…" : "创建并进入"}
            <ArrowRight />
          </button>
        </footer>
      </section>
    </div>
  );
}
