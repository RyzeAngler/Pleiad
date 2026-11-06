import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import {
  ArrowClockwise,
  ArrowLeft,
  ArrowUp,
  CaretDown,
  CaretRight,
  ChatCircle,
  Check,
  Cube,
  Compass,
  Stack,
  ArrowsOutSimple,
  ArrowsInSimple,
  Clock,
  FlowArrow,
  Plus,
  Stop,
  Trash,
} from "@phosphor-icons/react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, errorMessage, isAbort } from "./api";
import { actionableExecution, isActive, statusLabel } from "./projection";
import type {
  Approval,
  Conversation,
  ConversationMessage,
  Execution,
  GraphNode,
  GraphReference,
  Resources,
  Space,
} from "./types";
import { Workbench, ResultValue } from "./Workbench";
import { ResourceLibrary } from "./ResourceLibrary";
import { SpaceSettings } from "./SpaceSettings";
import { SavedGraphPreview } from "./SavedGraphPreview";
import { FailureDetails } from "./FailureDetails";
import { useExecution } from "./useExecution";
import { Navigation } from "./Navigation";
import { SpaceCreator } from "./SpaceCreator";
import { DeleteConfirmation } from "./DeleteConfirmation";

const initialLocation = new URLSearchParams(window.location.hash.slice(1));
const settingsSavingMessage = "空间配置正在保存，请稍候。";
type Screen = "workspace" | "space-settings" | "resources";
type DeleteTarget =
  | { kind: "space"; space: Space }
  | { kind: "conversation"; conversation: Conversation };

function Message({ message }: { message: ConversationMessage }) {
  if (message.role === "tool")
    return (
      <details className="tool-message">
        <summary>
          <Check size={12} /> 工具结果 <span>{message.tool_call_id}</span>
        </summary>
        <pre>{message.content ?? "无文本内容"}</pre>
      </details>
    );
  if (!message.content && !message.refusal && !message.tool_calls.length)
    return null;
  return (
    <article
      className={`message ${message.role === "user" ? "user-message" : "assistant-message"}`}
    >
      <div className="message-author">
        {message.role === "user" ? "你" : "Weave"}
      </div>
      {message.content && (
        <div className="markdown">
          <Markdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
            }}
          >
            {message.content}
          </Markdown>
        </div>
      )}
      {message.refusal && <p className="inline-error">{message.refusal}</p>}
      {message.tool_calls.map((call) => (
        <details className="tool-call" key={call.id}>
          <summary>
            <CaretRight size={12} />
            {call.name}
          </summary>
          <pre>
            {typeof call.arguments === "string"
              ? call.arguments
              : JSON.stringify(call.arguments, null, 2)}
          </pre>
        </details>
      ))}
    </article>
  );
}

export function ExecutionNotice({ execution }: { execution: Execution }) {
  if (!execution.error && execution.status !== "cancelled") return null;
  return (
    <div className="execution-error" role="status">
      <strong>
        {execution.status === "cancelled" ? "执行已取消" : "执行失败"}
      </strong>
      <p>本次请求：{execution.objective}</p>
      {execution.error && <FailureDetails error={execution.error} />}
    </div>
  );
}

function ApprovalCard({
  approval,
  onDecision,
  busy,
}: {
  approval: Approval;
  onDecision: (approval: Approval, decision: "approve" | "reject") => void;
  busy: boolean;
}) {
  const [expanded, setExpanded] = useState(true);
  return (
    <section className="approval-card">
      <div className="row">
        <Clock size={17} />
        <h3>待审批</h3>
      </div>
      <p>{approval.interruption.reason}</p>
      <strong>
        {approval.interruption.tool_name}{" "}
        <code>{approval.interruption.tool_code}</code>
      </strong>
      <p className="muted">
        有效期至 {new Date(approval.deadline).toLocaleString("zh-CN")}
      </p>
      <button
        className="text-button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
      >
        操作与参数 <CaretDown size={12} />
      </button>
      {expanded && (
        <pre>
          {JSON.stringify(
            {
              arguments: approval.interruption.arguments,
              details: approval.interruption.details,
            },
            null,
            2,
          )}
        </pre>
      )}
      <div className="row approval-actions">
        <button
          className="secondary-button"
          disabled={busy}
          onClick={() => onDecision(approval, "reject")}
        >
          拒绝
        </button>
        <button
          className="primary-button"
          disabled={busy}
          onClick={() => onDecision(approval, "approve")}
        >
          批准执行
        </button>
      </div>
    </section>
  );
}

function App() {
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spaceId, setSpaceId] = useState<string | null>(
    initialLocation.get("space"),
  );
  const [conversationId, setConversationId] = useState<string | null>(
    initialLocation.get("conversation"),
  );
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [allConversations, setAllConversations] = useState<Conversation[]>([]);
  const [allConversationsLoading, setAllConversationsLoading] = useState(false);
  const [globalHistoryError, setGlobalHistoryError] = useState("");
  const [currentHistoryError, setCurrentHistoryError] = useState("");
  const [executions, setExecutions] = useState<Execution[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<Approval[]>([]);
  const [selectedExecutionId, setSelectedExecutionId] = useState<string | null>(
    null,
  );
  const [resources, setResources] = useState<Resources | null>(null);
  const [resourcesLoading, setResourcesLoading] = useState(true);
  const [resourcesError, setResourcesError] = useState("");
  const [screen, setScreen] = useState<Screen>(() => {
    const view = initialLocation.get("view");
    return view === "resources" ||
      (view === "space-settings" && initialLocation.has("space"))
      ? view
      : "workspace";
  });
  const resourceReturn = useRef<Screen>("workspace");
  const settingsDirty = useRef(false);
  const settingsSaving = useRef(false);
  const reportSettingsDirty = useCallback((dirty: boolean) => {
    settingsDirty.current = dirty;
  }, []);
  const [previewGraph, setPreviewGraph] = useState<GraphReference | null>(null);
  const [loading, setLoading] = useState(true);
  const [taskLoading, setTaskLoading] = useState(false);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [composerFocus, setComposerFocus] = useState(0);
  const [error, setError] = useState("");
  const reportSettingsSaving = useCallback((saving: boolean) => {
    settingsSaving.current = saving;
    if (!saving)
      setError((current) => (current === settingsSavingMessage ? "" : current));
  }, []);
  const [taskError, setTaskError] = useState("");
  const [composer, setComposer] = useState("");
  const [busy, setBusy] = useState(false);
  const [savingGraph, setSavingGraph] = useState(false);
  const [canvasOpen, setCanvasOpen] = useState(true);
  const [dockExpanded, setDockExpanded] = useState(false);
  const [newSpace, setNewSpace] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const deletionPending = useRef(false);
  const dataVersion = useRef(0);
  const [refreshIndex, setRefreshIndex] = useState(0);
  const [historyRevision, setHistoryRevision] = useState(0);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const navigation = useRef(0);
  const conversationRef = useRef(conversationId);
  conversationRef.current = conversationId;
  const messageEnd = useRef<HTMLDivElement>(null);
  const autoScroll = useRef(true);
  const currentSpace = spaces.find((item) => item.space_id === spaceId);
  const currentConversation = conversations.find(
    (item) => item.conversation_id === conversationId,
  );
  const refreshConversation = useCallback(
    () => setRefreshIndex((value) => value + 1),
    [],
  );
  const execution = useExecution(selectedExecutionId, refreshConversation);
  const lastExecution = executions.at(-1);
  const selectedExecution = executions.find(
    (item) => item.execution_id === selectedExecutionId,
  );
  const activeExecution = actionableExecution(executions);

  useEffect(() => {
    const abort = new AbortController();
    const version = dataVersion.current;
    setLoading(true);
    api
      .spaces(abort.signal)
      .then((loaded) => {
        if (abort.signal.aborted || version !== dataVersion.current) return;
        setSpaces(loaded);
        setError("");
      })
      .catch((failure) => {
        if (
          !isAbort(failure) &&
          !abort.signal.aborted &&
          version === dataVersion.current
        )
          setError(errorMessage(failure));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [refreshIndex]);

  useEffect(() => {
    const abort = new AbortController();
    const version = dataVersion.current;
    setResourcesLoading(true);
    api
      .resources(abort.signal)
      .then((catalog) => {
        if (abort.signal.aborted || version !== dataVersion.current) return;
        setResources(catalog);
        setResourcesError("");
      })
      .catch((failure) => {
        if (
          !isAbort(failure) &&
          !abort.signal.aborted &&
          version === dataVersion.current
        ) {
          setResources(null);
          setResourcesError(`资源目录加载失败：${errorMessage(failure)}`);
        }
      })
      .finally(() => {
        if (!abort.signal.aborted && version === dataVersion.current)
          setResourcesLoading(false);
      });
    return () => abort.abort();
  }, [refreshIndex]);

  useEffect(() => {
    const parameters = new URLSearchParams();
    if (spaceId) parameters.set("space", spaceId);
    if (conversationId) parameters.set("conversation", conversationId);
    if (screen !== "workspace") parameters.set("view", screen);
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${parameters.size ? `#${parameters}` : ""}`,
    );
  }, [spaceId, conversationId, screen]);

  useEffect(() => {
    if (!spaceId) {
      setConversations([]);
      setConversationsLoading(false);
      return;
    }
    const abort = new AbortController();
    const version = dataVersion.current;
    setConversationsLoading(true);
    setCurrentHistoryError("");
    api
      .conversations(spaceId, abort.signal)
      .then((records) => {
        if (!abort.signal.aborted && version === dataVersion.current)
          setConversations(records);
      })
      .catch((failure) => {
        if (
          !isAbort(failure) &&
          !abort.signal.aborted &&
          version === dataVersion.current
        )
          setCurrentHistoryError(errorMessage(failure));
      })
      .finally(() => {
        if (!abort.signal.aborted) setConversationsLoading(false);
      });
    return () => abort.abort();
  }, [spaceId, conversationId, refreshIndex, historyRevision]);

  useEffect(() => {
    if (loading) return;
    const abort = new AbortController();
    const version = dataVersion.current;
    setAllConversationsLoading(true);
    setGlobalHistoryError("");
    Promise.all(
      spaces.map((space) => api.conversations(space.space_id, abort.signal)),
    )
      .then((records) => {
        if (!abort.signal.aborted && version === dataVersion.current)
          setAllConversations(records.flat());
      })
      .catch((failure) => {
        if (
          !isAbort(failure) &&
          !abort.signal.aborted &&
          version === dataVersion.current
        ) {
          setAllConversations([]);
          setGlobalHistoryError(errorMessage(failure));
        }
      })
      .finally(() => {
        if (!abort.signal.aborted) setAllConversationsLoading(false);
      });
    return () => abort.abort();
  }, [spaces, loading, refreshIndex, historyRevision]);

  useEffect(() => {
    setExecutions([]);
    setMessages([]);
    setPendingApprovals([]);
    setSelectedExecutionId(null);
    setTaskError("");
    setCanvasOpen(true);
    setDockExpanded(false);
    autoScroll.current = true;
  }, [conversationId]);

  useEffect(() => {
    if (!conversationId) {
      setTaskLoading(false);
      return;
    }
    const abort = new AbortController();
    const version = dataVersion.current;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setTaskLoading(true);
    async function poll() {
      let active = true;
      try {
        const [records, history] = await Promise.all([
          api.executions(conversationId!, abort.signal),
          api.messages(conversationId!, abort.signal),
        ]);
        if (abort.signal.aborted || version !== dataVersion.current) return;
        const sorted = [...records].sort((a, b) =>
          a.created_at.localeCompare(b.created_at),
        );
        const pending = await Promise.all(
          sorted
            .filter((record) => record.status === "waiting_approval")
            .map((record) => api.approvals(record.execution_id, abort.signal)),
        );
        if (abort.signal.aborted || version !== dataVersion.current) return;
        setPendingApprovals(pending.flat());
        setExecutions(sorted);
        setMessages(history);
        setTaskError("");
        setSelectedExecutionId(
          (previous) => previous ?? sorted.at(-1)?.execution_id ?? null,
        );
        active = records.some(isActive);
      } catch (failure) {
        if (
          !isAbort(failure) &&
          !abort.signal.aborted &&
          version === dataVersion.current
        )
          setTaskError(errorMessage(failure));
      } finally {
        if (!abort.signal.aborted) {
          setTaskLoading(false);
          timer = setTimeout(() => void poll(), active ? 1500 : 6000);
        }
      }
    }
    void poll();
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [conversationId, refreshIndex]);

  useEffect(() => {
    if (autoScroll.current)
      messageEnd.current?.scrollIntoView({ block: "end", behavior: "instant" });
  }, [messages, execution.live, pendingApprovals]);

  useEffect(() => {
    if (composerFocus > 0) composerRef.current?.focus();
  }, [composerFocus]);

  function startTask(targetSpaceId: string) {
    if (!canLeaveSettings()) return;
    navigation.current++;
    if (targetSpaceId !== spaceId) setConversations([]);
    setSpaceId(targetSpaceId);
    setComposer("");
    setConversationId(null);
    setScreen("workspace");
    setError("");
    setCurrentHistoryError("");
    setComposerFocus((value) => value + 1);
  }

  function selectSpace(id: string | null) {
    if (!canLeaveSettings()) return;
    navigation.current++;
    setComposer("");
    if (id !== spaceId) setConversations([]);
    setSpaceId(id);
    setConversationId(null);
    setScreen("workspace");
    setError("");
    setCurrentHistoryError("");
    if (id) setComposerFocus((value) => value + 1);
  }
  function selectConversation(id: string, targetSpaceId = spaceId) {
    if (!canLeaveSettings()) return;
    navigation.current++;
    if (targetSpaceId !== spaceId) {
      setConversations([]);
      setSpaceId(targetSpaceId);
    }
    setError("");
    setCurrentHistoryError("");
    setComposer("");
    setConversationId(id);
    setScreen("workspace");
  }

  function canLeaveSettings() {
    if (deletionPending.current) return false;
    if (settingsSaving.current) {
      setError(settingsSavingMessage);
      return false;
    }
    if (!settingsDirty.current) return true;
    if (!window.confirm("有未保存的修改，确定离开？")) return false;
    settingsDirty.current = false;
    return true;
  }

  function requestDeletion(target: DeleteTarget) {
    if (
      busy ||
      savingGraph ||
      settingsSaving.current ||
      deletionPending.current
    )
      return;
    setDeleteError("");
    setDeleteTarget(target);
  }

  async function deleteRecord() {
    if (!deleteTarget || deletionPending.current) return;
    deletionPending.current = true;
    setDeleting(true);
    setDeleteError("");
    try {
      if (deleteTarget.kind === "space")
        await api.deleteSpace(deleteTarget.space.space_id);
      else
        await api.deleteConversation(deleteTarget.conversation.conversation_id);

      dataVersion.current++;
      navigation.current++;
      const removedSpace =
        deleteTarget.kind === "space" ? deleteTarget.space.space_id : null;
      const removedTask =
        deleteTarget.kind === "conversation"
          ? deleteTarget.conversation.conversation_id
          : null;
      const keepTask = (task: Conversation) =>
        task.space_id !== removedSpace && task.conversation_id !== removedTask;
      setSpaces((previous) =>
        previous.filter((space) => space.space_id !== removedSpace),
      );
      setConversations((previous) => previous.filter(keepTask));
      setAllConversations((previous) => previous.filter(keepTask));
      if (
        (removedSpace !== null && removedSpace === spaceId) ||
        (removedTask !== null && removedTask === conversationId)
      ) {
        setConversationId(null);
        setSelectedExecutionId(null);
        setExecutions([]);
        setMessages([]);
        setPendingApprovals([]);
        setTaskError("");
        setComposer("");
        setScreen("workspace");
        setPreviewGraph(null);
        if (removedSpace) {
          setSpaceId(null);
          settingsDirty.current = false;
        } else setComposerFocus((value) => value + 1);
      }
      setError("");
      setCurrentHistoryError("");
      setGlobalHistoryError("");
      setDeleteTarget(null);
      refreshConversation();
    } catch (failure) {
      setDeleteError(errorMessage(failure));
    } finally {
      deletionPending.current = false;
      setDeleting(false);
    }
  }

  function openSettings(section: "space" | "resources") {
    const next: Screen = section === "space" ? "space-settings" : "resources";
    if (next === screen || !canLeaveSettings()) return;
    if (next === "resources") resourceReturn.current = screen;
    setScreen(next);
  }

  async function send(event?: FormEvent) {
    event?.preventDefault();
    if (!composer.trim() || !currentSpace || busy || deletionPending.current)
      return;
    const objective = composer.trim();
    const targetSpace = currentSpace.space_id;
    const startedNavigation = navigation.current;
    setBusy(true);
    setError("");
    try {
      let target = conversationId;
      if (!target) {
        const created = await api.createConversation(
          targetSpace,
          objective.slice(0, 160),
        );
        target = created.conversation_id;
        setHistoryRevision((value) => value + 1);
        if (navigation.current === startedNavigation) {
          setConversations((previous) => [created, ...previous]);
          setConversationId(target);
          conversationRef.current = target;
        }
      }
      const response = await api.submit(target, objective);
      if (
        navigation.current === startedNavigation &&
        conversationRef.current === target
      ) {
        setComposer("");
        setExecutions((previous) => [
          ...previous.filter(
            (item) => item.execution_id !== response.execution.execution_id,
          ),
          response.execution,
        ]);
        setSelectedExecutionId(response.execution.execution_id);
        setCanvasOpen(true);
        autoScroll.current = true;
      }
      refreshConversation();
    } catch (failure) {
      setError(
        navigation.current === startedNavigation
          ? errorMessage(failure)
          : `其他空间的消息提交失败：${errorMessage(failure)}`,
      );
      if (navigation.current === startedNavigation) setComposer(objective);
    } finally {
      setBusy(false);
    }
  }

  function composerKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      void send();
    }
  }
  async function decision(approval: Approval, value: "approve" | "reject") {
    setBusy(true);
    setError("");
    try {
      await api.decide(approval, value);
      execution.refresh();
      refreshConversation();
    } catch (failure) {
      setError(errorMessage(failure));
      execution.refresh();
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    if (!activeExecution) return;
    setBusy(true);
    setError("");
    try {
      await api.cancel(activeExecution.execution_id);
      execution.refresh();
      refreshConversation();
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  }
  function askNode(node: GraphNode) {
    setComposer(`请解释“${node.objective}”节点的资源选择、输入与执行结果。`);
    setDockExpanded(true);
    composerRef.current?.focus();
  }
  async function saveGraph(graph: GraphReference) {
    if (!currentSpace || savingGraph) return;
    setSavingGraph(true);
    setError("");
    try {
      const updated = await api.updateSpace({
        ...currentSpace,
        saved_graph: graph,
      });
      setSpaces((previous) =>
        previous.map((space) =>
          space.space_id === updated.space_id ? updated : space,
        ),
      );
    } catch (failure) {
      setError(`保存编排失败：${errorMessage(failure)}`);
    } finally {
      setSavingGraph(false);
    }
  }
  const knownMessageIds = new Set(
    messages.map((message) => message.message_id),
  );
  const liveEntries = Object.entries(execution.live).filter(
    ([id]) => !knownMessageIds.has(id),
  );
  const hasGraph = Boolean(execution.details?.graphs.length);
  const visibleCanvas = Boolean(
    conversationId && selectedExecutionId && hasGraph && canvasOpen,
  );
  useEffect(() => {
    if (pendingApprovals.length) setDockExpanded(true);
  }, [pendingApprovals.length]);
  useEffect(() => {
    if (autoScroll.current)
      messageEnd.current?.scrollIntoView({ block: "end" });
  }, [visibleCanvas, dockExpanded]);
  const errorText =
    error ||
    taskError ||
    execution.error ||
    currentHistoryError ||
    globalHistoryError ||
    resourcesError;

  const composerView = (
    <form className="composer-area" onSubmit={(event) => void send(event)}>
      {currentSpace?.saved_graph && (
        <div className="composer-graph-context">
          <FlowArrow size={14} />
          <span>
            空间编排：{" "}
            <strong>
              {currentSpace.saved_graph.graph_id} · v
              {currentSpace.saved_graph.revision}
            </strong>
          </span>
        </div>
      )}
      <div className="composer">
        <textarea
          ref={composerRef}
          aria-label="发送消息"
          placeholder={
            conversationId
              ? "继续补充需求，或调整当前编排…"
              : "描述这次任务的需求…"
          }
          value={composer}
          onChange={(event) => setComposer(event.target.value)}
          onKeyDown={composerKey}
          rows={3}
          disabled={busy}
        />
        <div className="composer-bottom">
          <span className="composer-context">
            <Cube size={17} />
            {resources?.models.find((model) => model.role === "text")?.model ??
              "Weave"}
          </span>
          <div className="row">
            {activeExecution && (
              <button
                type="button"
                className="icon-button stop-button"
                disabled={busy}
                onClick={() => void cancel()}
                aria-label="停止当前执行"
              >
                <Stop weight="fill" />
              </button>
            )}
            <button
              className="send-button"
              type="submit"
              disabled={busy || !composer.trim()}
              aria-label="发送消息"
            >
              <ArrowUp weight="bold" />
            </button>
          </div>
        </div>
      </div>
      <div className="composer-note">
        <span>
          {activeExecution
            ? "新消息将在当前执行结束后处理。"
            : "Enter 发送 · Shift + Enter 换行"}
        </span>
      </div>
    </form>
  );

  return (
    <div className="application">
      <Navigation
        spaces={spaces}
        space={screen === "workspace" ? currentSpace : undefined}
        conversations={allConversations}
        conversationId={conversationId}
        loading={loading || allConversationsLoading}
        error={globalHistoryError}
        onRetry={refreshConversation}
        onSelectSpace={selectSpace}
        onSelectConversation={(task) =>
          selectConversation(task.conversation_id, task.space_id)
        }
        onNewTask={startTask}
        resourcesActive={screen === "resources"}
        onSettings={openSettings}
        deletionDisabled={busy || savingGraph}
        onDeleteConversation={(conversation) =>
          requestDeletion({ kind: "conversation", conversation })
        }
      />
      <main className="main">
        {errorText && (
          <div className="error-banner" role="alert">
            <span>{errorText}</span>
            <button
              onClick={() => {
                setError("");
                execution.refresh();
                refreshConversation();
              }}
            >
              <ArrowClockwise />
              重新加载
            </button>
          </div>
        )}
        {screen === "space-settings" && currentSpace ? (
          <SpaceSettings
            key={currentSpace.space_id}
            space={currentSpace}
            resources={resources}
            onDirtyChange={reportSettingsDirty}
            onSavingChange={reportSettingsSaving}
            deletionDisabled={busy || savingGraph}
            onDelete={() =>
              requestDeletion({ kind: "space", space: currentSpace })
            }
            onViewGraph={setPreviewGraph}
            onSaved={(updated) =>
              setSpaces((previous) =>
                previous.map((item) =>
                  item.space_id === updated.space_id ? updated : item,
                ),
              )
            }
            onClose={() => {
              if (canLeaveSettings()) setScreen("workspace");
            }}
          />
        ) : screen === "resources" ? (
          <ResourceLibrary
            resources={resources}
            loading={resourcesLoading}
            onRetry={refreshConversation}
            onClose={() => setScreen(resourceReturn.current)}
          />
        ) : !spaceId ? (
          <section className="spaces-page">
            <div className="page-heading">
              <div>
                <span className="eyebrow">工作区</span>
                <h1>空间</h1>
              </div>
              <button
                className="primary-button"
                onClick={() => setNewSpace(true)}
              >
                <Plus />
                创建空间
              </button>
            </div>
            {loading && !spaces.length ? (
              <p className="muted">正在连接工作区…</p>
            ) : spaces.length ? (
              <div className="spaces-grid">
                {spaces.map((space, index) => (
                  <button
                    className="space-tile"
                    key={space.space_id}
                    aria-label={`${space.name}，进入空间`}
                    onClick={() => selectSpace(space.space_id)}
                  >
                    <span className={`space-symbol tone-${index % 3}`}>
                      {index % 2 === 0 ? (
                        <Compass size={30} />
                      ) : (
                        <Stack size={30} />
                      )}
                    </span>
                    <div>
                      <h2>{space.name}</h2>
                      <p>
                        {allConversationsLoading
                          ? "正在读取任务…"
                          : `${allConversations.filter((task) => task.space_id === space.space_id).length} 个任务`}
                      </p>
                    </div>
                    <span className="space-enter">
                      进入空间 <CaretRight />
                    </span>
                  </button>
                ))}
              </div>
            ) : (
              !error && (
                <div className="first-space">
                  <Stack size={40} />
                  <h2>暂无空间</h2>
                  <p>创建空间，为同类任务保存常用配置。</p>
                </div>
              )
            )}
            {allConversations.length > 0 && (
              <div className="recent-tasks workspace-recent">
                <h3>最近任务</h3>
                {[...allConversations]
                  .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
                  .slice(0, 5)
                  .map((task) => (
                    <div className="recent-task-row" key={task.conversation_id}>
                      <button
                        className="recent-task-open"
                        onClick={() =>
                          selectConversation(
                            task.conversation_id,
                            task.space_id,
                          )
                        }
                      >
                        <ChatCircle />
                        <span>{task.title}</span>
                        <small>
                          {
                            spaces.find(
                              (space) => space.space_id === task.space_id,
                            )?.name
                          }
                        </small>
                        <CaretRight />
                      </button>
                      <button
                        className="icon-button delete-action"
                        disabled={busy || savingGraph}
                        aria-label={`删除任务：${task.title}`}
                        title="删除任务"
                        onClick={() =>
                          requestDeletion({
                            kind: "conversation",
                            conversation: task,
                          })
                        }
                      >
                        <Trash size={18} aria-hidden="true" />
                      </button>
                    </div>
                  ))}
              </div>
            )}
          </section>
        ) : !currentSpace && !loading ? (
          <div className="page-empty">
            <h2>空间不存在</h2>
            <button className="text-button" onClick={() => selectSpace(null)}>
              <ArrowLeft />
              返回所有空间
            </button>
          </div>
        ) : !conversationId ? (
          <section className="space-home">
            <div className="space-intro">
              <h1>开始新的任务</h1>
            </div>
            {composerView}
            {conversationsLoading && <p className="muted">正在读取最近任务…</p>}
            {conversations.length > 0 && (
              <div className="recent-tasks">
                <div className="section-heading">
                  <h3>最近任务</h3>
                  <span className="muted">{conversations.length}</span>
                </div>
                {conversations.slice(0, 5).map((task) => (
                  <div className="recent-task-row" key={task.conversation_id}>
                    <button
                      className="recent-task-open"
                      onClick={() => selectConversation(task.conversation_id)}
                    >
                      <ChatCircle />
                      <span>{task.title}</span>
                      <CaretRight />
                    </button>
                    <button
                      className="icon-button delete-action"
                      disabled={busy || savingGraph}
                      aria-label={`删除任务：${task.title}`}
                      title="删除任务"
                      onClick={() =>
                        requestDeletion({
                          kind: "conversation",
                          conversation: task,
                        })
                      }
                    >
                      <Trash size={18} aria-hidden="true" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>
        ) : (
          <div
            className={`task-workspace ${visibleCanvas ? "with-canvas" : ""} ${dockExpanded ? "dock-expanded" : ""}`}
          >
            <section className="conversation">
              <div className="conversation-header">
                <div>
                  <h2>{currentConversation?.title ?? "任务对话"}</h2>
                  {visibleCanvas && <span className="muted">对话</span>}
                </div>
                <div className="conversation-actions">
                  {hasGraph && !canvasOpen && (
                    <button
                      className="text-button"
                      aria-label="打开工作画布"
                      onClick={() => setCanvasOpen(true)}
                    >
                      <FlowArrow />
                      展开编排
                    </button>
                  )}
                  {visibleCanvas && (
                    <button
                      className="text-button"
                      aria-expanded={dockExpanded}
                      onClick={() => setDockExpanded(!dockExpanded)}
                    >
                      {dockExpanded ? <ArrowsInSimple /> : <ArrowsOutSimple />}
                      {dockExpanded ? "收起对话" : "展开对话"}
                    </button>
                  )}
                  {executions.length > 0 && (
                    <select
                      aria-label="查看执行记录"
                      value={selectedExecutionId ?? ""}
                      onChange={(event) => {
                        setSelectedExecutionId(event.target.value);
                        setCanvasOpen(true);
                      }}
                    >
                      {executions.map((record, index) => (
                        <option
                          key={record.execution_id}
                          value={record.execution_id}
                        >
                          第 {index + 1} 次 · {statusLabel(record.status)}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              </div>
              <div
                className="conversation-scroll"
                onScroll={(event) => {
                  const element = event.currentTarget;
                  autoScroll.current =
                    element.scrollHeight -
                      element.scrollTop -
                      element.clientHeight <
                    100;
                }}
              >
                {taskLoading && !messages.length && (
                  <p className="muted loading-line">正在读取对话…</p>
                )}
                {messages.map((message) => (
                  <Message key={message.message_id} message={message} />
                ))}
                {!messages.length &&
                  lastExecution &&
                  lastExecution.status !== "queued" && (
                    <article className="message user-message">
                      <div className="message-author">你</div>
                      <p>{lastExecution.objective}</p>
                    </article>
                  )}
                {executions
                  .filter((record) => record.status === "queued")
                  .map((record) => (
                    <article
                      className="message user-message"
                      key={record.execution_id}
                    >
                      <div className="message-author">
                        你 <span className="muted">已排队</span>
                      </div>
                      <p>{record.objective}</p>
                    </article>
                  ))}
                {liveEntries.map(([id, content]) => (
                  <article
                    className="message assistant-message streaming"
                    key={id}
                  >
                    <div className="message-author">
                      Weave <span className="muted">正在输出</span>
                    </div>
                    <div className="markdown">
                      <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
                    </div>
                  </article>
                ))}
                {selectedExecution?.output !== null &&
                  selectedExecution?.output !== undefined &&
                  !messages.some(
                    (message) =>
                      message.role === "assistant" && message.content,
                  ) && (
                    <article className="message assistant-message">
                      <div className="message-author">Weave</div>
                      <ResultValue value={selectedExecution.output} />
                    </article>
                  )}
                {pendingApprovals.map((approval) => (
                  <ApprovalCard
                    key={approval.interruption.interruption_id}
                    approval={approval}
                    busy={busy}
                    onDecision={(item, value) => void decision(item, value)}
                  />
                ))}
                {selectedExecution && (
                  <ExecutionNotice execution={selectedExecution} />
                )}
                {activeExecution && !liveEntries.length && (
                  <div className="working-indicator">
                    <span className={`status-dot ${activeExecution.status}`} />
                    {statusLabel(activeExecution.status)}
                    <span>
                      {hasGraph
                        ? "编排状态已同步到画布"
                        : "对话与工具记录将在这里更新"}
                    </span>
                  </div>
                )}
                <div ref={messageEnd} />
              </div>
              {composerView}
            </section>
            {visibleCanvas && (
              <Workbench
                key={selectedExecutionId}
                details={execution.details!}
                events={execution.events}
                resources={resources}
                onClose={() => setCanvasOpen(false)}
                onAsk={askNode}
                savedGraph={currentSpace?.saved_graph ?? null}
                savingGraph={savingGraph}
                onSaveGraph={(graph) => void saveGraph(graph)}
              />
            )}
          </div>
        )}
      </main>
      {previewGraph && currentSpace && (
        <SavedGraphPreview
          spaceId={currentSpace.space_id}
          reference={previewGraph}
          resources={resources}
          onClose={() => setPreviewGraph(null)}
        />
      )}
      {deleteTarget && (
        <DeleteConfirmation
          kind={deleteTarget.kind}
          name={
            deleteTarget.kind === "space"
              ? deleteTarget.space.name
              : deleteTarget.conversation.title
          }
          pending={deleting}
          error={deleteError}
          onCancel={() => {
            if (!deletionPending.current) setDeleteTarget(null);
          }}
          onConfirm={() => void deleteRecord()}
        />
      )}
      {newSpace && (
        <SpaceCreator
          resources={resources}
          onClose={() => setNewSpace(false)}
          onCreated={(created) => {
            setSpaces((previous) => [...previous, created]);
            selectSpace(created.space_id);
            setNewSpace(false);
            setComposerFocus((value) => value + 1);
          }}
        />
      )}
    </div>
  );
}

export default App;
