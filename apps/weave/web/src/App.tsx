import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowClockwise,
  ArrowUp,
  BookOpen,
  ChatCircle,
  Copy,
  GearSix,
  Link,
  Paperclip,
  CirclesThree,
  Stack,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, errorMessage } from "./api";
import { navigate, paneIdentity, readRoute, routeIdentity } from "./projection";
import { useProduct, useRecord } from "./useProduct";
import {
  AttentionCard,
  AttentionsWorkspace,
  ContentWorkspace,
  CopyButton,
  HandbackEvidence,
  Modal,
  Empty,
  ExecutionWorkspace,
  ResourcesWorkspace,
  SettingsWorkspace,
  SpacesWorkspace,
  SpaceWorkspace,
} from "./Workspace";
import type {
  Attachment,
  Attention,
  ContextReference,
  Draft,
  Execution,
  Message,
  Route,
  WorkspaceControls,
} from "./types";

const emptyDraft = (): Draft => ({
  text: "",
  attachment_ids: [],
  target_execution_ids: [],
  context_refs: [],
  reply_to_message_id: null,
  attention_id: null,
});
type PendingSubmission = { digest: string; requestId: string };
type ChatAnchor = { messageId: string; offset: number };
type UploadAttempt = { id: string; file: File; error: string };
type ReturnFocus = {
  element: HTMLElement | null;
  contentId: string | null;
  scope: "main" | ".main-chat";
  routeId?: string;
};
interface SavedDraft {
  draft: Draft;
  pending: PendingSubmission | null;
  error: string;
}
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const strings = (value: unknown): value is string[] =>
  Array.isArray(value) &&
  value.every((item: unknown) => typeof item === "string");
function validDraft(value: unknown): value is Draft {
  return (
    record(value) &&
    typeof value.text === "string" &&
    strings(value.attachment_ids) &&
    strings(value.target_execution_ids) &&
    (value.reply_to_message_id === null ||
      typeof value.reply_to_message_id === "string") &&
    (value.attention_id === null || typeof value.attention_id === "string") &&
    Array.isArray(value.context_refs) &&
    value.context_refs.every(
      (item: unknown) =>
        record(item) &&
        ["space", "execution", "artifact", "graph_node"].includes(
          String(item.kind),
        ) &&
        typeof item.id === "string" &&
        ["execution_id", "graph_run_id", "node_id", "node_attempt_id"].every(
          (key) => item[key] === undefined || typeof item[key] === "string",
        ),
    )
  );
}
function storedDraft(): SavedDraft {
  try {
    const raw = localStorage.getItem("weave-main-draft-v2");
    if (raw === null) return { draft: emptyDraft(), pending: null, error: "" };
    const value: unknown = JSON.parse(raw);
    if (!record(value) || !validDraft(value.draft))
      throw new Error("草稿格式无法识别，请检查浏览器保存的内容。");
    const pending = value.pending;
    if (
      pending !== null &&
      (!record(pending) ||
        typeof pending.digest !== "string" ||
        typeof pending.requestId !== "string")
    )
      throw new Error("待确认提交的身份无法恢复。");
    return {
      draft: value.draft,
      pending:
        pending === null
          ? null
          : {
              digest: String(pending.digest),
              requestId: String(pending.requestId),
            },
      error: "",
    };
  } catch (failure) {
    return { draft: emptyDraft(), pending: null, error: errorMessage(failure) };
  }
}

function storedChatPosition(): {
  following: boolean;
  anchor: ChatAnchor | null;
  error: string;
} {
  try {
    const raw = localStorage.getItem("weave-chat-position-v2");
    if (raw === null) return { following: true, anchor: null, error: "" };
    const value: unknown = JSON.parse(raw);
    if (
      !record(value) ||
      typeof value.following !== "boolean" ||
      (value.anchor !== null &&
        (!record(value.anchor) ||
          typeof value.anchor.messageId !== "string" ||
          typeof value.anchor.offset !== "number" ||
          !Number.isFinite(value.anchor.offset)))
    )
      throw new Error("聊天阅读位置无法恢复。");
    return {
      following: value.following,
      anchor:
        value.anchor === null
          ? null
          : {
              messageId: String(value.anchor.messageId),
              offset: Number(value.anchor.offset),
            },
      error: "",
    };
  } catch (failure) {
    return { following: true, anchor: null, error: errorMessage(failure) };
  }
}

const controlReceiptLabels = new Map([
  ["pause requested", "已请求暂停"],
  ["resume requested", "已请求继续"],
  ["cancel requested", "已请求取消"],
]);

function ChatMessage({
  message,
  executions,
  onNavigate,
  onReply,
}: {
  message: Message;
  executions: readonly Execution[];
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
  onReply: (message: Message) => void;
}) {
  const body =
    message.role === "system" && message.source_key.startsWith("control/")
      ? (controlReceiptLabels.get(message.body) ?? message.body)
      : message.body;
  return (
    <article
      className={`message message-${message.role}`}
      id={`message-${message.message_id}`}
      data-sequence={message.chat_sequence}
      tabIndex={-1}
    >
      <div className={`avatar avatar-${message.role}`}>
        {message.role === "user" ? (
          "W"
        ) : message.role === "assistant" ? (
          <CirclesThree weight="fill" size={21} />
        ) : (
          <Stack size={17} />
        )}
      </div>
      <div className="message-content">
        <div className="markdown">
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
            }}
          >
            {body}
          </Markdown>
        </div>
        {message.attachment_ids.map((id) => (
          <button
            className="message-attachment"
            data-content-id={`attachment/${id}`}
            key={id}
            onClick={(event) =>
              onNavigate(
                { view: "attachment", attachment_id: id },
                event.currentTarget,
              )
            }
          >
            查看附件 {id.slice(0, 8)}
          </button>
        ))}
        {message.reports.map((report) => (
          <button
            className="message-report"
            key={report.report_id}
            onClick={() =>
              onNavigate({
                view: "execution",
                execution_id: report.execution_id,
              })
            }
          >
            <Stack size={17} />
            <span>
              <strong>{report.summary}</strong>
              <small>
                {report.kind === "ready"
                  ? "结果已提交"
                  : report.kind === "failed"
                    ? "工作失败"
                    : report.kind === "needs_input"
                      ? "需要补充"
                      : "进展记录"}{" "}
                · 查看来源工作
              </small>
            </span>
          </button>
        ))}
        {message.artifacts.map((artifact) => (
          <button
            className="message-artifact"
            data-content-id={`artifact/${artifact.artifact_id}`}
            key={artifact.artifact_id}
            onClick={(event) =>
              onNavigate(
                {
                  view: "artifact",
                  artifact_id: artifact.artifact_id,
                },
                event.currentTarget,
              )
            }
          >
            <Paperclip size={17} />
            <span>
              <strong>{artifact.name}</strong>
              <small>
                {artifact.mime} · {artifact.size.toLocaleString()} 字节 ·{" "}
                {artifact.deletion_state === "active"
                  ? "查看产物"
                  : artifact.deletion_state === "corrupt"
                    ? "内容损坏"
                    : "内容已清理"}
              </small>
            </span>
          </button>
        ))}
        {(message.input_dispositions.length > 0 ||
          message.target_execution_ids.length > 0) && (
          <div className="message-targets">
            {message.input_dispositions.map((disposition) => (
              <button
                className="chip"
                key={disposition.input_id}
                onClick={() =>
                  onNavigate({
                    view: "execution",
                    execution_id: disposition.target_execution_id,
                    tab: "materials",
                  })
                }
              >
                <Stack size={13} />
                <span>
                  {disposition.space_name} · {disposition.execution_objective}
                  <small>
                    {disposition.state === "owned"
                      ? "由此任务处理"
                      : disposition.state === "handled"
                        ? "已处理"
                        : disposition.reassignment_pending
                          ? "等待安排任务"
                          : "已退回"}
                  </small>
                </span>
              </button>
            ))}
            {message.target_execution_ids
              .filter(
                (id) =>
                  !message.input_dispositions.some(
                    (item) => item.target_execution_id === id,
                  ),
              )
              .map((id) => {
                const execution = executions.find(
                  (item) => item.execution_id === id,
                );
                const ordinal =
                  message.target_execution_ids.length > 1
                    ? ` ${message.target_execution_ids.indexOf(id) + 1}`
                    : "";
                return (
                  <button
                    className="chip"
                    key={id}
                    title={execution?.objective}
                    onClick={() =>
                      onNavigate({
                        view: "execution",
                        execution_id: id,
                        tab: "materials",
                      })
                    }
                  >
                    <Stack size={13} />
                    <span className="message-target-label">
                      查看任务{ordinal}
                      {execution ? ` · ${execution.objective}` : ""}
                    </span>
                  </button>
                );
              })}
          </div>
        )}
        {message.handbacks
          .filter((handback) => handback.unknown_effects)
          .map((handback) => (
            <div key={handback.handback_id}>
              <HandbackEvidence
                handback={handback}
                resolution={message.handback_resolutions.find(
                  (value) => value.handback_id === handback.handback_id,
                )}
              />
              <button
                className="text-button"
                onClick={() =>
                  onNavigate({
                    view: "execution",
                    execution_id: handback.target_execution_id,
                    tab: "materials",
                  })
                }
              >
                查看核实记录
              </button>
            </div>
          ))}
        <div className="message-actions">
          <button
            title="引用此消息"
            aria-label={`引用消息 ${message.chat_sequence}`}
            onClick={() => onReply(message)}
          >
            <Link size={14} />
          </button>
          <CopyButton text={body} label={`复制消息 ${message.chat_sequence}`}>
            <Copy size={14} />
          </CopyButton>
          <time dateTime={message.created_at}>
            {new Date(message.created_at).toLocaleTimeString("zh-CN", {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </time>
        </div>
      </div>
    </article>
  );
}

export default function App() {
  const product = useProduct();
  const { snapshot, fresh, generation, refresh, resync } = product;
  const [workspaceQueries, setWorkspaceQueries] = useState<
    Record<string, string>
  >({});
  const [workspacePages, setWorkspacePages] = useState<Record<string, number>>(
    {},
  );
  const setWorkspaceQuery = useCallback((key: string, value: string) => {
    setWorkspaceQueries((current) => ({ ...current, [key]: value }));
  }, []);
  const setWorkspacePage = useCallback((key: string, value: number) => {
    setWorkspacePages((current) => ({ ...current, [key]: value }));
  }, []);
  useEffect(() => setWorkspacePages({}), [generation]);
  const workspaceControls: WorkspaceControls = {
    queries: workspaceQueries,
    pages: workspacePages,
    setQuery: setWorkspaceQuery,
    setPage: setWorkspacePage,
  };
  const [saved] = useState(storedDraft);
  const [savedPosition] = useState(storedChatPosition);
  const [navigation, setNavigation] = useState(() => {
    const route = readRoute();
    const workspaceRoute: Exclude<Route, { view: "chat" }> =
      route.view === "chat" ? { view: "spaces" } : route;
    return { route, workspaceRoute };
  });
  const { route, workspaceRoute } = navigation;
  const setRoute = useCallback((next: Route) => {
    setNavigation((current) => ({
      route: next,
      workspaceRoute: next.view === "chat" ? current.workspaceRoute : next,
    }));
  }, []);
  const [draft, setDraft] = useState<Draft>(saved.draft);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploadAttempts, setUploadAttempts] = useState<UploadAttempt[]>([]);
  const [verifiedTargets, setVerifiedTargets] = useState<Execution[]>([]);
  const [answer, setAnswer] = useState<Attention | null>(null);
  const [answerVerified, setAnswerVerified] = useState(false);
  const [locatedAttentionId, setLocatedAttentionId] = useState<string | null>(
    null,
  );
  const [attentionLocation, setAttentionLocation] = useState(0);
  const attentionScrollIntent = useRef<{ id: string; focus: boolean } | null>(
    null,
  );
  const locatedAttention = useRecord(
    `located-attention/${locatedAttentionId}`,
    (signal) =>
      locatedAttentionId
        ? api.attention(locatedAttentionId, signal)
        : Promise.resolve(null),
    generation,
    product.readVersion,
  );
  const onLocateAttention = useCallback(
    (attention: Attention, focus = true) => {
      attentionScrollIntent.current = { id: attention.attention_id, focus };
      setLocatedAttentionId(attention.attention_id);
      setAttentionLocation((current) => current + 1);
    },
    [],
  );
  const [referencesVerified, setReferencesVerified] = useState(false);
  const [submitting, setSubmitting] = useState(false),
    [uploading, setUploading] = useState(false),
    [error, setError] = useState(saved.error || savedPosition.error),
    [notice, setNotice] = useState("");
  const [decisionBusy, setDecisionBusy] = useState(false),
    [following, setFollowing] = useState(savedPosition.following),
    [newMessages, setNewMessages] = useState(false);
  const chatAnchor = useRef<ChatAnchor | null>(savedPosition.anchor);
  const loadingAnchor = useRef(false);
  const latestMessage = useRef(0);
  const pendingSubmission = useRef<PendingSubmission | null>(saved.pending);
  const pendingDecisions = useRef(
    new Map<string, { digest: string; requestId: string }>(),
  );
  const dirty = useRef(false);
  const [pendingNavigation, setPendingNavigation] = useState<Route | null>(
    null,
  );
  const [saveBeforeLeaving, setSaveBeforeLeaving] = useState(false);
  const onDirtyChange = useCallback((value: boolean) => {
    dirty.current = value;
  }, []);
  const input = useRef<HTMLTextAreaElement>(null),
    chatHeading = useRef<HTMLHeadingElement>(null),
    history = useRef<HTMLDivElement>(null),
    workspace = useRef<HTMLElement>(null),
    fileInput = useRef<HTMLInputElement>(null);
  useLayoutEffect(() => {
    const textarea = input.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${textarea.scrollHeight}px`;
  }, [draft.text, route.view]);
  const workspacePositions = useRef(new Map<string, number>());
  const restoringWorkspace = useRef<string | null>(null);
  const routeRef = useRef(route);
  routeRef.current = route;
  const workspaceRouteRef = useRef(workspaceRoute);
  workspaceRouteRef.current = workspaceRoute;
  const workspaceRouteKey = routeIdentity(workspaceRoute);
  useLayoutEffect(() => {
    const container = workspace.current;
    if (!container || route.view === "chat") return;
    const position = workspacePositions.current.get(workspaceRouteKey) ?? 0;
    restoringWorkspace.current = workspaceRouteKey;
    const restore = () => {
      if (restoringWorkspace.current !== workspaceRouteKey) return true;
      container.scrollTop = position;
      if (container.scrollTop !== position) return false;
      restoringWorkspace.current = null;
      return true;
    };
    if (restore()) return;
    const observer = new MutationObserver(() => {
      if (restore()) observer.disconnect();
    });
    observer.observe(container, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [workspaceRouteKey, route.view]);
  const previewReturn = useRef<Route>({ view: "spaces" });
  const resourcesReturn = useRef<Route>({ view: "spaces" });
  const previewOpener = useRef<ReturnFocus | null>(null);
  const resourceOpener = useRef<ReturnFocus | null>(null);
  const pendingReturnFocus = useRef<ReturnFocus | null>(null);
  const pendingPaneFocus = useRef(false);
  const pendingChatFocus = useRef(false);
  const routeOpeners = useRef(new Map<string, ReturnFocus>());
  const onNavigate = useCallback((value: Route, trigger?: HTMLElement) => {
    pendingReturnFocus.current = null;
    if (
      dirty.current &&
      value.view !== "chat" &&
      value.view !== "resources" &&
      !(
        workspaceRouteRef.current.view === "resources" &&
        JSON.stringify(value) === JSON.stringify(resourcesReturn.current)
      ) &&
      routeIdentity(value) !== routeIdentity(workspaceRouteRef.current)
    ) {
      setPendingNavigation(value);
      setSaveBeforeLeaving(false);
      return;
    }
    const paneChanged = paneIdentity(value) !== paneIdentity(routeRef.current);
    const opener =
      trigger ??
      (document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null);
    if (paneChanged && opener?.dataset.routeId) {
      routeOpeners.current.set(paneIdentity(routeRef.current), {
        element: opener,
        contentId: null,
        routeId: opener.dataset.routeId,
        scope: "main",
      });
    }
    if (paneChanged)
      pendingReturnFocus.current =
        routeOpeners.current.get(paneIdentity(value)) ?? null;
    if (
      value.view === "resources" &&
      workspaceRouteRef.current.view !== "resources"
    ) {
      resourcesReturn.current =
        routeRef.current.view === "chat"
          ? workspaceRouteRef.current
          : routeRef.current;
      resourceOpener.current = {
        element:
          trigger ??
          (document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null),
        contentId: null,
        scope: "main",
      };
    }
    if (
      (value.view === "artifact" || value.view === "attachment") &&
      !["artifact", "attachment"].includes(routeRef.current.view)
    ) {
      previewReturn.current = routeRef.current;
      const element =
        trigger ??
        (document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null);
      previewOpener.current = {
        element,
        contentId: `${value.view}/${value.view === "artifact" ? value.artifact_id : value.attachment_id}`,
        scope: element?.closest("main") ? "main" : ".main-chat",
      };
    }
    if (value.view === "chat") pendingReturnFocus.current = null;
    pendingChatFocus.current = value.view === "chat";
    pendingPaneFocus.current =
      value.view !== "chat" && paneChanged && !pendingReturnFocus.current;
    navigate(value);
    setRoute(value);
  }, []);
  useEffect(() => {
    if (route.view === "chat") {
      if (pendingChatFocus.current && !pendingReturnFocus.current) {
        input.current?.focus({ preventScroll: true });
      }
      pendingChatFocus.current = false;
      if (!pendingReturnFocus.current) return;
    }
    const intent = pendingReturnFocus.current;
    if (!intent && !pendingPaneFocus.current) return;
    const scope = document.querySelector<HTMLElement>(intent?.scope ?? "main");
    if (!scope) return;
    const restore = () => {
      if (!pendingReturnFocus.current && !pendingPaneFocus.current) return true;
      const heading = Array.from(
        scope.querySelectorAll<HTMLElement>("h1"),
      ).find((element) => !element.closest("[hidden]"));
      let target = intent
        ? intent.element?.isConnected
          ? intent.element
          : intent.contentId
            ? scope.querySelector<HTMLElement>(
                `[data-content-id="${CSS.escape(intent.contentId)}"]`,
              )
            : intent.routeId
              ? Array.from(
                  scope.querySelectorAll<HTMLElement>("[data-route-id]"),
                ).find((element) => element.dataset.routeId === intent.routeId)
              : null
        : heading;
      if (
        !target &&
        intent?.routeId &&
        !Array.from(scope.querySelectorAll("[data-loading]")).some(
          (element) => !element.closest("[hidden]"),
        )
      )
        target = heading;
      if (!target) return false;
      if (target === heading && target.getAttribute("tabindex") !== "-1")
        target.tabIndex = -1;
      target.focus({ preventScroll: true });
      if (document.activeElement !== target) return false;
      pendingReturnFocus.current = null;
      pendingPaneFocus.current = false;
      return true;
    };
    if (restore()) return;
    const observer = new MutationObserver(() => {
      if (restore()) observer.disconnect();
    });
    observer.observe(scope, {
      attributes: true,
      childList: true,
      subtree: true,
    });
    return () => observer.disconnect();
  }, [route, generation]);
  useEffect(() => {
    if (!saveBeforeLeaving || pendingNavigation === null || dirty.current)
      return;
    const next = pendingNavigation;
    setPendingNavigation(null);
    setSaveBeforeLeaving(false);
    onNavigate(next);
  });
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (dirty.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, []);
  useEffect(() => {
    const changed = () => {
      const next = readRoute();
      if (JSON.stringify(next) === JSON.stringify(routeRef.current)) return;
      if (
        dirty.current &&
        next.view !== "chat" &&
        next.view !== "resources" &&
        !(
          workspaceRouteRef.current.view === "resources" &&
          JSON.stringify(next) === JSON.stringify(resourcesReturn.current)
        ) &&
        routeIdentity(next) !== routeIdentity(workspaceRouteRef.current)
      ) {
        navigate(routeRef.current);
        setPendingNavigation(next);
        setSaveBeforeLeaving(false);
        return;
      }
      pendingReturnFocus.current =
        next.view === "chat"
          ? null
          : (routeOpeners.current.get(paneIdentity(next)) ?? null);
      pendingPaneFocus.current =
        next.view !== "chat" &&
        paneIdentity(next) !== paneIdentity(routeRef.current) &&
        !pendingReturnFocus.current;
      pendingChatFocus.current = next.view === "chat";
      setRoute(next);
    };
    window.addEventListener("hashchange", changed);
    window.addEventListener("popstate", changed);
    return () => {
      window.removeEventListener("hashchange", changed);
      window.removeEventListener("popstate", changed);
    };
  }, []);
  useEffect(() => {
    if (saved.error && !draft.text) return;
    try {
      localStorage.setItem(
        "weave-main-draft-v2",
        JSON.stringify({ draft, pending: pendingSubmission.current }),
      );
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }, [draft, submitting, saved.error]);
  useLayoutEffect(() => {
    const container = history.current;
    if (!container || following || !chatAnchor.current) return;
    const target = document.getElementById(
      `message-${chatAnchor.current.messageId}`,
    );
    if (target) {
      container.scrollTop +=
        target.getBoundingClientRect().top -
        container.getBoundingClientRect().top -
        chatAnchor.current.offset;
    } else if (
      fresh &&
      snapshot?.messages.next_cursor !== null &&
      snapshot?.messages.next_cursor !== undefined &&
      !loadingAnchor.current
    ) {
      loadingAnchor.current = true;
      void product.loadOlder().finally(() => {
        loadingAnchor.current = false;
      });
    }
  }, [snapshot, following, fresh, product.loadOlder, route.view]);
  useEffect(() => {
    const latest = snapshot?.messages.items.at(-1)?.chat_sequence ?? 0;
    if (following) {
      history.current?.scrollTo({ top: history.current.scrollHeight });
      chatAnchor.current = null;
      try {
        localStorage.setItem(
          "weave-chat-position-v2",
          JSON.stringify({ following: true, anchor: null }),
        );
      } catch (failure) {
        setError(errorMessage(failure));
      }
      setNewMessages(false);
    } else if (latest > latestMessage.current) setNewMessages(true);
    latestMessage.current = latest;
  }, [snapshot?.messages.items, following]);
  useEffect(() => {
    setAnswerVerified(false);
    if (!draft.attention_id) {
      setAnswer(null);
      return;
    }
    const controller = new AbortController();
    let current = true;
    void api
      .attention(draft.attention_id, controller.signal)
      .then((value) => {
        if (current) {
          setAnswer(value);
          setAnswerVerified(true);
          if (value.status !== "pending") {
            setDraft((previous) => ({
              ...previous,
              attention_id: null,
              target_execution_ids: [],
            }));
            setNotice(
              `该事项${value.status === "resolved" ? "已处理" : "已失效"}，正文已保留在普通消息草稿中。`,
            );
          }
        }
      })
      .catch((failure) => {
        if (current) setError(errorMessage(failure));
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [draft.attention_id, generation, snapshot?.snapshot_sequence]);
  useEffect(() => {
    setReferencesVerified(false);
    if (!fresh) return;
    const controller = new AbortController();
    let current = true;
    const verify = async () => {
      const targets: Execution[] = [];
      for (const target of draft.target_execution_ids) {
        const execution = await api.execution(target, controller.signal);
        targets.push(execution);
        if (
          ["succeeded", "failed", "cancelled", "cancelling"].includes(
            execution.status,
          )
        )
          throw new Error("关联工作已结束或正在取消，请调整发送目标。");
      }
      for (const reference of draft.context_refs) {
        if (reference.kind === "space") {
          const space = await api.space(reference.id, controller.signal);
          if (space.deleted || space.deletion_pending)
            throw new Error("引用空间已失效。");
        } else if (reference.kind === "artifact") {
          const artifact = await api.artifact(reference.id, controller.signal);
          if (artifact.deletion_state !== "active")
            throw new Error("引用产物已失效。");
        } else if (reference.kind === "execution") {
          await api.execution(reference.id, controller.signal);
        } else {
          if (!reference.execution_id || !reference.graph_run_id)
            throw new Error("节点引用缺少来源身份。");
          const attempt = await api.nodeAttempt(
            reference.execution_id,
            reference.id,
            controller.signal,
          );
          if (
            attempt.graph_run_id !== reference.graph_run_id ||
            attempt.node_id !== reference.node_id ||
            (reference.node_attempt_id !== undefined &&
              attempt.node_attempt_id !== reference.node_attempt_id)
          )
            throw new Error("引用节点的实际来源已无法核验。");
        }
      }
      for (const attachmentId of draft.attachment_ids) {
        const attachment = await api.attachment(
          attachmentId,
          controller.signal,
        );
        if (
          attachment.status === "deleted" ||
          (attachment.expires_at &&
            Date.parse(attachment.expires_at) <= Date.now())
        )
          throw new Error("附件已失效，请重新选择。");
      }
      if (current) {
        setVerifiedTargets(targets);
        setReferencesVerified(true);
      }
    };
    void verify().catch((failure) => {
      if (
        current &&
        !(failure instanceof DOMException && failure.name === "AbortError")
      )
        setError(errorMessage(failure));
    });
    return () => {
      current = false;
      controller.abort();
    };
  }, [
    fresh,
    generation,
    draft.target_execution_ids,
    draft.context_refs,
    draft.attachment_ids,
    snapshot?.snapshot_sequence,
  ]);
  useEffect(() => {
    if (!fresh) return;
    const controller = new AbortController();
    let current = true;
    void Promise.all(
      draft.attachment_ids.map((id) => api.attachment(id, controller.signal)),
    )
      .then((values) => {
        if (current) setAttachments(values);
      })
      .catch((failure) => {
        if (current) setError(errorMessage(failure));
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [draft.attachment_ids, generation, fresh]);
  const focus = () => input.current?.focus();
  const returnToChat = () => {
    onNavigate({ view: "chat" });
    input.current?.focus({ preventScroll: true });
  };
  const onSupplement = useCallback(
    (execution: Execution) => {
      const limit = snapshot?.capabilities.max_target_executions;
      if (limit === undefined) {
        setError("工作关联上限尚未取得，请重新同步后选择。");
        return;
      }
      const targets = draftRef.current.attention_id
        ? []
        : draftRef.current.target_execution_ids;
      if (
        !targets.includes(execution.execution_id) &&
        targets.length >= limit
      ) {
        setError(`最多关联 ${limit} 项工作，请先移除已有目标。`);
        focus();
        return;
      }
      setDraft((current) => ({
        ...current,
        attention_id: null,
        reply_to_message_id: null,
        target_execution_ids: current.attention_id
          ? [execution.execution_id]
          : [
              ...new Set([
                ...current.target_execution_ids,
                execution.execution_id,
              ]),
            ],
      }));
      setError("");
      setNotice("");
      focus();
    },
    [snapshot?.capabilities.max_target_executions],
  );
  const onReference = useCallback((reference: ContextReference) => {
    setDraft((current) => ({
      ...current,
      attention_id: null,
      target_execution_ids: [],
      context_refs: [
        ...current.context_refs.filter(
          (value) =>
            !(value.kind === reference.kind && value.id === reference.id),
        ),
        reference,
      ],
    }));
    setNotice("");
    focus();
  }, []);
  const onAnswer = useCallback(
    (attention: Attention) => {
      setDraft((current) => ({
        ...current,
        attention_id: attention.attention_id,
        target_execution_ids: [],
        reply_to_message_id: null,
      }));
      setAnswer(attention);
      onLocateAttention(attention, false);
      setNotice("");
      focus();
    },
    [onLocateAttention],
  );
  const onReply = (message: Message) => {
    setDraft((current) => ({
      ...current,
      attention_id: null,
      reply_to_message_id: message.message_id,
      target_execution_ids: current.attention_id
        ? []
        : current.target_execution_ids,
    }));
    setNotice("");
    focus();
  };
  const identity = (digest: string) => {
    if (pendingSubmission.current?.digest !== digest)
      pendingSubmission.current = { digest, requestId: crypto.randomUUID() };
    return pendingSubmission.current.requestId;
  };
  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (
      !snapshot ||
      !fresh ||
      !referencesVerified ||
      submitting ||
      uploading ||
      uploadAttempts.length > 0 ||
      !draft.text.trim()
    )
      return;
    const captured = draftRef.current;
    if (
      captured.context_refs.length > snapshot.capabilities.max_context_refs ||
      captured.target_execution_ids.length >
        snapshot.capabilities.max_target_executions
    ) {
      setError("关联目标或只读引用超出当前允许的数量，请调整后发送。");
      return;
    }
    if (
      new TextEncoder().encode(captured.text).byteLength >
      snapshot.capabilities.max_message_bytes
    ) {
      setError("正文超出当前允许的大小。");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      if (captured.attention_id) {
        if (
          !answerVerified ||
          !answer ||
          answer.status !== "pending" ||
          answer.attention_id !== captured.attention_id
        )
          throw new Error("回答事项尚未取得有效状态，请重新读取。");
        const payload = {
          expected_revision: answer.revision,
          answer: captured.text,
          attachment_ids: captured.attachment_ids,
          context_refs: captured.context_refs,
        };
        await api.resolve(captured.attention_id, {
          ...payload,
          request_id: identity(
            JSON.stringify({ attention_id: captured.attention_id, ...payload }),
          ),
        });
      } else {
        const payload = {
          text: captured.text,
          attachment_ids: captured.attachment_ids,
          target_execution_ids: captured.target_execution_ids,
          context_refs: captured.context_refs,
          reply_to_message_id: captured.reply_to_message_id,
        };
        await api.submit({
          ...payload,
          client_message_id: identity(JSON.stringify(payload)),
        });
      }
      if (JSON.stringify(draftRef.current) === JSON.stringify(captured)) {
        setDraft(emptyDraft());
        setAttachments([]);
        setAnswer(null);
      }
      pendingSubmission.current = null;
      setNotice("");
      setFollowing(true);
      refresh();
    } catch (failure) {
      setError(errorMessage(failure));
      refresh();
    } finally {
      setSubmitting(false);
    }
  };
  const onDecision = useCallback(
    async (attention: Attention, decision: "approve" | "reject") => {
      const payload = {
        expected_revision: attention.revision,
        decision,
        action_digest: attention.action_digest,
      };
      const digest = JSON.stringify(payload);
      const previous = pendingDecisions.current.get(attention.attention_id);
      const requestId =
        previous?.digest === digest ? previous.requestId : crypto.randomUUID();
      pendingDecisions.current.set(attention.attention_id, {
        digest,
        requestId,
      });
      setDecisionBusy(true);
      setError("");
      try {
        await api.resolve(attention.attention_id, {
          ...payload,
          request_id: requestId,
        });
        pendingDecisions.current.delete(attention.attention_id);
        refresh();
        setNotice("决定已保存。");
      } catch (failure) {
        setError(errorMessage(failure));
        refresh();
      } finally {
        setDecisionBusy(false);
      }
    },
    [refresh],
  );
  const sendUploads = async (attempts: UploadAttempt[]) => {
    if (!fresh || uploading) return;
    setUploading(true);
    setError("");
    for (const attempt of attempts) {
      try {
        const value = await api.upload(attempt.file, attempt.id);
        setAttachments((current) =>
          current.some((item) => item.attachment_id === value.attachment_id)
            ? current
            : [...current, value],
        );
        setDraft((current) => ({
          ...current,
          attachment_ids: current.attachment_ids.includes(value.attachment_id)
            ? current.attachment_ids
            : [...current.attachment_ids, value.attachment_id],
        }));
        setUploadAttempts((current) =>
          current.filter((item) => item.id !== attempt.id),
        );
      } catch (failure) {
        setUploadAttempts((current) =>
          current.map((item) =>
            item.id === attempt.id
              ? { ...item, error: errorMessage(failure) }
              : item,
          ),
        );
      }
    }
    setUploading(false);
    if (fileInput.current) fileInput.current.value = "";
  };
  const upload = async (files: FileList | File[] | null) => {
    if (!files || !snapshot || !fresh || uploading) return;
    const selected = [...files];
    if (
      selected.length + draft.attachment_ids.length + uploadAttempts.length >
      snapshot.capabilities.max_attachments
    ) {
      setError("附件数量超出当前限制。");
      return;
    }
    if (
      selected.some(
        (file) => file.size > snapshot.capabilities.max_attachment_bytes,
      ) ||
      selected.reduce(
        (sum, file) => sum + file.size,
        attachments.reduce((sum, file) => sum + file.size, 0) +
          uploadAttempts.reduce((sum, item) => sum + item.file.size, 0),
      ) > snapshot.capabilities.max_message_attachment_bytes
    ) {
      setError("附件大小超出当前限制。");
      return;
    }
    const attempts = selected.map((file) => ({
      id: crypto.randomUUID(),
      file,
      error: "",
    }));
    setUploadAttempts((current) => [...current, ...attempts]);
    await sendUploads(attempts);
  };
  const removeAttachment = async (id: string) => {
    try {
      await api.deleteAttachment(id, crypto.randomUUID());
      setAttachments((current) =>
        current.filter((value) => value.attachment_id !== id),
      );
      setDraft((current) => ({
        ...current,
        attachment_ids: current.attachment_ids.filter((value) => value !== id),
      }));
      const index = attachments.findIndex(
        (value) => value.attachment_id === id,
      );
      const next = attachments[index + 1] ?? attachments[index - 1];
      const adjacent = next
        ? document.querySelector<HTMLElement>(
            `.attachment-chips [data-content-id="attachment/${CSS.escape(next.attachment_id)}"]`,
          )
        : null;
      if (adjacent) adjacent.focus();
      else focus();
    } catch (failure) {
      setError(errorMessage(failure));
    }
  };
  const resourceSource = resourcesReturn.current;
  const displayedSpace =
    workspaceRoute.view === "space"
      ? workspaceRoute
      : workspaceRoute.view === "resources" && resourceSource.view === "space"
        ? resourceSource
        : null;
  const common = {
    controls: workspaceControls,
    generation,
    version: product.readVersion,
    fresh,
    onNavigate,
    onReference,
    onSupplement,
    refresh,
    onDirtyChange,
  };
  const pending = [...(snapshot?.attentions.items ?? [])];
  if (
    locatedAttention.value?.status === "pending" &&
    !pending.some(
      (item) => item.attention_id === locatedAttention.value?.attention_id,
    )
  )
    pending.push(locatedAttention.value);
  useEffect(() => {
    const intent = attentionScrollIntent.current;
    if (!intent) return;
    const target = document.getElementById(`chat-attention-${intent.id}`);
    if (!target) return;
    setFollowing(false);
    target.scrollIntoView({ block: "nearest" });
    if (intent.focus) target.focus({ preventScroll: true });
    attentionScrollIntent.current = null;
  }, [attentionLocation, locatedAttention.value, snapshot?.attentions.items]);
  const textBytes = new TextEncoder().encode(draft.text).byteLength;
  const draftTargetNames = draft.target_execution_ids.map((id, index) => ({
    id,
    name:
      verifiedTargets.find((value) => value.execution_id === id)?.objective ??
      snapshot?.executions.items.find((value) => value.execution_id === id)
        ?.objective ??
      `任务 ${index + 1}`,
  }));
  const activeNavigation = [
    "space",
    "execution",
    "artifact",
    "attachment",
  ].includes(route.view)
    ? "spaces"
    : route.view;
  return (
    <>
      <div className={`app-shell ${route.view === "chat" ? "chat-view" : ""}`}>
        <nav className="navigation" aria-label="主导航">
          <button
            className="brand"
            aria-label="Weave 主聊天"
            onClick={returnToChat}
          >
            <CirclesThree size={28} weight="fill" />
          </button>
          <button
            className={`nav-item ${activeNavigation === "chat" ? "selected" : ""}`}
            aria-label="聊天"
            aria-current={activeNavigation === "chat" ? "true" : undefined}
            title="聊天"
            onClick={returnToChat}
          >
            <ChatCircle size={23} />
          </button>
          <button
            className={`nav-item ${activeNavigation === "spaces" ? "selected" : ""}`}
            aria-label="空间"
            aria-current={activeNavigation === "spaces" ? "true" : undefined}
            onClick={() =>
              onNavigate(
                route.view === "chat" &&
                  [
                    "spaces",
                    "space",
                    "execution",
                    "artifact",
                    "attachment",
                  ].includes(workspaceRoute.view)
                  ? workspaceRoute
                  : { view: "spaces" },
              )
            }
          >
            <Stack size={23} />
          </button>
          <button
            className={`nav-item ${activeNavigation === "resources" ? "selected" : ""}`}
            aria-label="资源"
            aria-current={activeNavigation === "resources" ? "true" : undefined}
            onClick={(event) =>
              onNavigate({ view: "resources" }, event.currentTarget)
            }
          >
            <BookOpen size={23} />
          </button>
          <button
            className={`nav-item ${activeNavigation === "attentions" ? "selected" : ""}`}
            aria-label={`需要处理 ${snapshot?.attentions.total ?? 0} 项`}
            aria-current={
              activeNavigation === "attentions" ? "true" : undefined
            }
            onClick={() => onNavigate({ view: "attentions" })}
          >
            <WarningCircle size={23} />
            {snapshot && snapshot.attentions.total > 0 && (
              <span className="nav-badge">{snapshot.attentions.total}</span>
            )}
          </button>
          <button
            className={`nav-item settings-navigation ${activeNavigation === "settings" ? "selected" : ""}`}
            aria-label="设置"
            aria-current={activeNavigation === "settings" ? "true" : undefined}
            onClick={() => onNavigate({ view: "settings" })}
          >
            <GearSix size={23} />
          </button>
        </nav>
        <section className="main-chat" aria-label="永久主聊天">
          <header className="chat-heading">
            <h1 ref={chatHeading} tabIndex={-1}>
              Weave
            </h1>
            <span
              className={`connection ${fresh ? "connected" : ""}`}
              title={fresh ? "状态已核验" : "正在核验状态"}
            >
              {fresh ? "持续协作" : "连接核验"}
            </span>
          </header>
          {product.error && (
            <div className="connection-error" role="alert">
              <span>{product.error}</span>
              <button aria-label="重新同步工作区" onClick={resync}>
                <ArrowClockwise size={17} />
              </button>
            </div>
          )}
          <div
            className="chat-history"
            ref={history}
            onScroll={() => {
              const element = history.current;
              if (element) {
                const top = element.getBoundingClientRect().top;
                const visible = Array.from(
                  element.querySelectorAll<HTMLElement>(".message"),
                ).find(
                  (message) => message.getBoundingClientRect().bottom > top,
                );
                if (visible)
                  chatAnchor.current = {
                    messageId: visible.id.slice("message-".length),
                    offset: visible.getBoundingClientRect().top - top,
                  };
                const nextFollowing =
                  element.scrollHeight -
                    element.scrollTop -
                    element.clientHeight <
                  48;
                setFollowing(nextFollowing);
                try {
                  localStorage.setItem(
                    "weave-chat-position-v2",
                    JSON.stringify({
                      following: nextFollowing,
                      anchor: nextFollowing ? null : chatAnchor.current,
                    }),
                  );
                } catch (failure) {
                  setError(errorMessage(failure));
                }
              }
            }}
          >
            {snapshot?.messages.next_cursor !== null &&
              snapshot?.messages.next_cursor !== undefined && (
                <button
                  className="text-button older-messages"
                  onClick={() => {
                    const container = history.current;
                    const height = container?.scrollHeight ?? 0;
                    const top = container?.scrollTop ?? 0;
                    setFollowing(false);
                    void product.loadOlder().then(() =>
                      requestAnimationFrame(() => {
                        if (container)
                          container.scrollTop =
                            top + container.scrollHeight - height;
                      }),
                    );
                  }}
                >
                  读取更早的消息
                </button>
              )}
            {!snapshot ? (
              <p className="muted" role="status">
                正在恢复主聊天…
              </p>
            ) : snapshot.messages.items.length === 0 ? (
              <div className="chat-welcome">
                <CirclesThree size={38} weight="fill" />
                <h2>从一个目标开始</h2>
              </div>
            ) : (
              snapshot.messages.items.map((message) => (
                <ChatMessage
                  key={message.message_id}
                  message={message}
                  executions={snapshot.executions.items}
                  onNavigate={onNavigate}
                  onReply={onReply}
                />
              ))
            )}
            {snapshot?.routing.state === "processing" && (
              <p className="router-processing" role="status">
                Weave 正在处理 {snapshot.routing.pending_input_count} 条输入…
              </p>
            )}
            {pending.map((attention) => (
              <AttentionCard
                key={attention.attention_id}
                anchorId={`chat-attention-${attention.attention_id}`}
                attention={attention}
                onNavigate={onNavigate}
                onAnswer={onAnswer}
                onDecision={(value, decision) =>
                  void onDecision(value, decision)
                }
                busy={decisionBusy}
                enabled={
                  fresh &&
                  (snapshot?.attentions.items.some(
                    (item) => item.attention_id === attention.attention_id,
                  ) ||
                    (!locatedAttention.loading && !locatedAttention.error))
                }
              />
            ))}
          </div>
          {newMessages && !following && (
            <button
              className="new-messages"
              onClick={() => {
                setFollowing(true);
                const latest = snapshot?.messages.items.at(-1);
                if (latest)
                  document
                    .getElementById(`message-${latest.message_id}`)
                    ?.focus({ preventScroll: true });
              }}
            >
              有新消息，回到底部 ↓
            </button>
          )}
          <form
            className="composer"
            aria-label="主聊天输入"
            onDragOver={(event) => {
              if (event.dataTransfer.types.includes("Files"))
                event.preventDefault();
            }}
            onDrop={(event) => {
              if (event.dataTransfer.files.length) {
                event.preventDefault();
                void upload(event.dataTransfer.files);
              }
            }}
            onSubmit={(event) => void send(event)}
          >
            <div className="composer-tags">
              {draft.attention_id && (
                <span className="chip answer-tag">
                  回答 {answer?.question ?? "正在核验事项"}
                  <button
                    aria-label="退出回答模式"
                    onClick={() => {
                      setDraft((current) => ({
                        ...current,
                        attention_id: null,
                        target_execution_ids: [],
                      }));
                      focus();
                    }}
                    type="button"
                  >
                    <X size={13} />
                  </button>
                </span>
              )}
              {draftTargetNames.map((target) => (
                <span className="chip supplement-tag" key={target.id}>
                  <span className="chip-label" title={target.name}>
                    补充：{target.name}
                  </span>
                  <button
                    type="button"
                    aria-label={`取消补充：${target.name}`}
                    title="取消补充"
                    onClick={() => {
                      setDraft((current) => ({
                        ...current,
                        target_execution_ids:
                          current.target_execution_ids.filter(
                            (id) => id !== target.id,
                          ),
                      }));
                      setError("");
                      setNotice("");
                      focus();
                    }}
                  >
                    <X size={13} />
                  </button>
                </span>
              ))}
              {draft.context_refs.map((reference) => (
                <span
                  className="chip"
                  key={`${reference.kind}/${reference.id}`}
                >
                  <Link size={12} />
                  {reference.kind === "space"
                    ? "空间"
                    : reference.kind === "execution"
                      ? "工作"
                      : reference.kind === "artifact"
                        ? "产物"
                        : "节点"}{" "}
                  {reference.id.slice(0, 6)}
                  <button
                    type="button"
                    aria-label={`移除引用 ${reference.id}`}
                    onClick={() => {
                      setDraft((current) => ({
                        ...current,
                        context_refs: current.context_refs.filter(
                          (value) => value !== reference,
                        ),
                      }));
                      focus();
                    }}
                  >
                    <X size={13} />
                  </button>
                </span>
              ))}
              {draft.reply_to_message_id && (
                <span className="chip">
                  引用正式回复
                  <button
                    type="button"
                    aria-label="取消消息引用"
                    onClick={() => {
                      setDraft((current) => ({
                        ...current,
                        reply_to_message_id: null,
                      }));
                      focus();
                    }}
                  >
                    <X size={13} />
                  </button>
                </span>
              )}
            </div>
            {answer && draft.attention_id && answer.options.length > 0 && (
              <div className="answer-options">
                {answer.options.map((option) => (
                  <button
                    type="button"
                    className="chip"
                    key={option}
                    onClick={() => {
                      setDraft((current) => ({ ...current, text: option }));
                      focus();
                    }}
                  >
                    {option}
                  </button>
                ))}
              </div>
            )}
            <div className="composer-surface">
              <div className="composer-row">
                <input
                  hidden
                  ref={fileInput}
                  type="file"
                  multiple
                  onChange={(event) => void upload(event.target.files)}
                />
                <button
                  className="icon-button"
                  type="button"
                  aria-label="添加附件"
                  disabled={!fresh || uploading || submitting}
                  onClick={() => fileInput.current?.click()}
                >
                  <Paperclip size={22} />
                </button>
                <textarea
                  ref={input}
                  aria-label={
                    draft.attention_id ? "回答事项" : "给 Weave 的消息"
                  }
                  placeholder={
                    draft.attention_id ? "填写回答…" : "继续与 Weave 沟通…"
                  }
                  value={draft.text}
                  rows={1}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      text: event.target.value,
                    }))
                  }
                  onKeyDown={(event) => {
                    if (
                      event.key === "Enter" &&
                      !event.shiftKey &&
                      !event.nativeEvent.isComposing
                    ) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                />
                <button
                  className="send-button"
                  type="submit"
                  aria-busy={submitting}
                  aria-label={draft.attention_id ? "提交回答" : "发送消息"}
                  disabled={
                    !fresh ||
                    !referencesVerified ||
                    submitting ||
                    uploading ||
                    uploadAttempts.length > 0 ||
                    (snapshot !== null &&
                      textBytes > snapshot.capabilities.max_message_bytes) ||
                    !draft.text.trim() ||
                    (draft.attention_id !== null &&
                      (!answerVerified ||
                        !answer ||
                        answer.status !== "pending"))
                  }
                >
                  <ArrowUp size={21} />
                </button>
              </div>
              {attachments.length > 0 && (
                <div className="attachment-chips">
                  {attachments.map((attachment) => (
                    <span className="chip" key={attachment.attachment_id}>
                      <Paperclip size={13} />
                      <button
                        type="button"
                        data-content-id={`attachment/${attachment.attachment_id}`}
                        onClick={(event) =>
                          onNavigate(
                            {
                              view: "attachment",
                              attachment_id: attachment.attachment_id,
                            },
                            event.currentTarget,
                          )
                        }
                      >
                        {attachment.name}
                      </button>
                      <button
                        type="button"
                        disabled={!fresh || submitting}
                        aria-label={`移除附件 ${attachment.name}`}
                        onClick={() =>
                          void removeAttachment(attachment.attachment_id)
                        }
                      >
                        <X size={13} />
                      </button>
                    </span>
                  ))}
                </div>
              )}
              {uploadAttempts.length > 0 && (
                <div className="upload-attempts">
                  {uploadAttempts.map((attempt) => (
                    <div className="upload-attempt" key={attempt.id}>
                      <span>
                        {attempt.file.name} ·{" "}
                        {Math.ceil(attempt.file.size / 1024)} KiB
                      </span>
                      {attempt.error ? (
                        <>
                          <span role="alert" className="inline-error">
                            {attempt.error}
                          </span>
                          <button
                            type="button"
                            className="text-button"
                            disabled={!fresh || uploading}
                            onClick={() => void sendUploads([attempt])}
                          >
                            重试上传 {attempt.file.name}
                          </button>
                          <button
                            type="button"
                            aria-label={`移除失败附件 ${attempt.file.name}`}
                            onClick={() =>
                              setUploadAttempts((current) =>
                                current.filter(
                                  (item) => item.id !== attempt.id,
                                ),
                              )
                            }
                          >
                            <X size={13} />
                          </button>
                        </>
                      ) : (
                        <span role="status">正在上传…</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {snapshot &&
                textBytes >=
                  Math.ceil(snapshot.capabilities.max_message_bytes * 0.9) && (
                  <p
                    className={`draft-capacity ${textBytes > snapshot.capabilities.max_message_bytes ? "inline-error" : "muted"}`}
                    role="status"
                    aria-label="草稿容量提示"
                  >
                    {textBytes > snapshot.capabilities.max_message_bytes
                      ? "内容已超出输入上限，请删减后发送。"
                      : "内容接近输入上限。"}
                  </p>
                )}
            </div>
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            {(submitting || notice) && (
              <p className="composer-status" role="status">
                {submitting ? "正在发送…" : notice}
              </p>
            )}
          </form>
        </section>
        <main
          ref={workspace}
          className={`workspace ${fresh ? "" : "stale"}`}
          hidden={route.view === "chat"}
          aria-label="工作区"
          aria-busy={!snapshot}
          onScroll={(event) => {
            if (route.view !== "chat" && restoringWorkspace.current === null)
              workspacePositions.current.set(
                workspaceRouteKey,
                event.currentTarget.scrollTop,
              );
          }}
          onWheel={() => {
            restoringWorkspace.current = null;
          }}
          onPointerDown={() => {
            restoringWorkspace.current = null;
          }}
          onKeyDown={(event) => {
            if (
              [
                "PageUp",
                "PageDown",
                "Home",
                "End",
                "ArrowUp",
                "ArrowDown",
              ].includes(event.key)
            )
              restoringWorkspace.current = null;
          }}
        >
          {!fresh && snapshot && (
            <div className="stale-banner">
              正在核验最新事实，相关变更操作暂不可用。
            </div>
          )}
          {snapshot && displayedSpace && (
            <div hidden={workspaceRoute.view !== "space"}>
              <SpaceWorkspace
                {...common}
                key={displayedSpace.space_id}
                spaceId={displayedSpace.space_id}
                tab={displayedSpace.tab}
              />
            </div>
          )}
          {!snapshot ? (
            <Empty>正在读取工作区…</Empty>
          ) : workspaceRoute.view === "spaces" ? (
            <SpacesWorkspace
              {...common}
              spaces={snapshot.spaces.items}
              executions={snapshot.executions.items}
            />
          ) : workspaceRoute.view === "space" ? null : workspaceRoute.view ===
            "execution" ? (
            <ExecutionWorkspace
              {...common}
              key={workspaceRoute.execution_id}
              executionId={workspaceRoute.execution_id}
              tab={workspaceRoute.tab}
              graphRunId={workspaceRoute.graph_run_id}
              runId={workspaceRoute.run_id}
              nodeId={workspaceRoute.node_id}
              nodeAttemptId={workspaceRoute.node_attempt_id}
              onAnswer={onAnswer}
              onDecision={(value, decision) => void onDecision(value, decision)}
            />
          ) : workspaceRoute.view === "artifact" ||
            workspaceRoute.view === "attachment" ? (
            <ContentWorkspace
              controls={workspaceControls}
              kind={workspaceRoute.view}
              identity={
                workspaceRoute.view === "artifact"
                  ? workspaceRoute.artifact_id
                  : workspaceRoute.attachment_id
              }
              generation={generation}
              version={product.readVersion}
              onBack={() => {
                onNavigate(previewReturn.current);
                pendingReturnFocus.current = previewOpener.current;
                pendingPaneFocus.current = false;
              }}
              onNavigate={onNavigate}
              onReference={onReference}
            />
          ) : workspaceRoute.view === "resources" ? (
            <ResourcesWorkspace
              active={route.view !== "chat"}
              controls={workspaceControls}
              generation={generation}
              onBack={() => {
                onNavigate(resourcesReturn.current);
                pendingReturnFocus.current = resourceOpener.current;
                pendingPaneFocus.current = false;
              }}
            />
          ) : workspaceRoute.view === "settings" ? (
            <SettingsWorkspace generation={generation} />
          ) : (
            <AttentionsWorkspace
              controls={workspaceControls}
              onNavigate={onNavigate}
              generation={generation}
              version={product.readVersion}
              fresh={fresh}
              onAnswer={onAnswer}
              onLocate={onLocateAttention}
              onDecision={(value, decision) => void onDecision(value, decision)}
              busy={decisionBusy}
            />
          )}
        </main>
      </div>
      {pendingNavigation && !saveBeforeLeaving && (
        <Modal
          title="空间配置尚未保存"
          onClose={() => setPendingNavigation(null)}
        >
          <p>可以继续编辑、放弃这次修改，或保存后再离开。</p>
          <div className="row">
            <button
              className="secondary"
              autoFocus
              data-initial-focus
              onClick={() => setPendingNavigation(null)}
            >
              继续编辑
            </button>
            <button
              className="secondary"
              onClick={() => {
                const next = pendingNavigation;
                dirty.current = false;
                setPendingNavigation(null);
                onNavigate(next);
              }}
            >
              放弃修改并离开
            </button>
            <button
              className="primary"
              onClick={() => {
                setSaveBeforeLeaving(true);
                const form = document.getElementById("space-settings-form");
                if (form instanceof HTMLFormElement) form.requestSubmit();
              }}
            >
              保存后离开
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
