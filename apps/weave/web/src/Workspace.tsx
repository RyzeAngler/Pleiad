import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type KeyboardEvent,
} from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type NodeProps,
  type Node,
  type Edge,
  type Viewport,
} from "@xyflow/react";
import {
  ArrowLeft,
  ArrowSquareOut,
  ArrowsClockwise,
  BookOpen,
  CheckCircle,
  Check,
  Circle,
  FileText,
  Globe,
  HardDrives,
  PencilSimple,
  Robot,
  Terminal,
  Wrench,
  Clock,
  Flask,
  GearSix,
  Pause,
  Play,
  Stop,
  Trash,
  X,
} from "@phosphor-icons/react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  api,
  ApiError,
  completeOperation,
  errorMessage,
  operationId,
} from "./api";
import { graphPositions, statusLabel } from "./projection";
import { useRecord } from "./useProduct";
import type {
  Artifact,
  Attachment,
  Attention,
  ContextReference,
  DeletionPreview,
  Delivery,
  Execution,
  ExecutionDetail,
  ExecutionInput,
  Graph,
  GraphDetail,
  GraphRun,
  Handback,
  HandbackResolution,
  NodeAttempt,
  ModelStep,
  Page,
  Resource,
  ResourceDetail,
  Route,
  RunDetail,
  RunOperation,
  SDKToolCall,
  SDKMessage,
  Space,
  SpaceConfiguration,
  TextPage,
  ToolResultReference,
  WorkspaceControls,
} from "./types";

export function CopyButton({
  text,
  label,
  children,
}: {
  text: string;
  label: string;
  children?: ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);
  const copy = async () => {
    setError("");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch (failure) {
      setCopied(false);
      setError(`复制失败，请选择原文手动复制。${errorMessage(failure)}`);
    }
  };
  return (
    <>
      <button
        type="button"
        className="text-button"
        aria-label={label}
        onClick={() => void copy()}
      >
        {children ?? label}
        {copied && <span role="status">已复制</span>}
      </button>
      {error && (
        <span className="inline-error" role="alert">
          {error}
        </span>
      )}
    </>
  );
}

export function HandbackEvidence({
  handback,
  resolution,
}: {
  handback: Handback;
  resolution: HandbackResolution | undefined;
}) {
  if (!handback.unknown_effects) return null;
  return (
    <section className={`notice ${resolution ? "" : "warning"}`}>
      <strong>
        {resolution ? "操作影响已核实" : "有些操作的结果尚未确认"}
      </strong>
      {resolution && (
        <p>{new Date(resolution.resolved_at).toLocaleString("zh-CN")}</p>
      )}
    </section>
  );
}

export function Status({ value }: { value: string }) {
  return (
    <span className={`status status-${value}`}>
      <span aria-hidden="true">
        {value === "succeeded" ? "✓" : value === "failed" ? "!" : "●"}
      </span>
      {statusLabel(value)}
    </span>
  );
}
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <Flask size={28} />
      <p>{children}</p>
    </div>
  );
}
export function LoadState({
  loading,
  error,
}: {
  loading: boolean;
  error: string;
}) {
  return loading ? (
    <p className="muted" role="status" data-loading>
      正在读取最新状态…
    </p>
  ) : error ? (
    <p className="inline-error" role="alert">
      {error}
    </p>
  ) : null;
}
export function Modal({
  title,
  children,
  onClose,
  size = "standard",
}: {
  title: string;
  size?: "standard" | "compact" | "resource";
  children: ReactNode;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const opener = document.activeElement;
    dialog.current?.showModal();
    dialog.current?.querySelector<HTMLElement>("[data-initial-focus]")?.focus();
    return () => {
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className={`modal modal-${size}`}
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        close.current();
      }}
    >
      <header>
        <h2>{title}</h2>
        <button aria-label="关闭弹窗" onClick={onClose}>
          <X size={20} />
        </button>
      </header>
      {children}
    </dialog>
  );
}
export function AttentionCard({
  anchorId,
  attention,
  onAnswer,
  onDecision,
  onNavigate,
  busy,
  enabled,
}: {
  anchorId?: string;
  attention: Attention;
  onAnswer: (attention: Attention) => void;
  onDecision: (attention: Attention, decision: "approve" | "reject") => void;
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
  busy: boolean;
  enabled: boolean;
}) {
  return (
    <section
      className="attention-card"
      id={anchorId}
      tabIndex={anchorId ? -1 : undefined}
    >
      <div className="row">
        <Clock size={18} />
        <strong>
          {attention.kind === "approval"
            ? "操作审批"
            : attention.kind === "recovery"
              ? "需要核实"
              : "需要你的回答"}
        </strong>
        <Status value={attention.status} />
      </div>
      {attention.execution_id !== null ? (
        <button
          className="attention-source text-button"
          onClick={() =>
            onNavigate({
              view: "execution",
              execution_id: attention.execution_id!,
            })
          }
        >
          {attention.space_name} · {attention.execution_objective}
        </button>
      ) : (
        <p className="muted">主聊天事项</p>
      )}
      <p>{attention.question}</p>
      {attention.expires_at && (
        <p className="muted">
          有效期至 {new Date(attention.expires_at).toLocaleString("zh-CN")}
        </p>
      )}
      {attention.kind === "approval" &&
        (attention.action ? (
          <div className="approval-action">
            <strong>{attention.action.tool_name}</strong>
            <code>{attention.action.tool_code}</code>
            <pre>{JSON.stringify(attention.action.arguments, null, 2)}</pre>
          </div>
        ) : (
          <p role="alert" className="inline-error">
            具体动作尚未取得，请刷新当前事项。
          </p>
        ))}
      {attention.action_digest && (
        <details>
          <summary>查看动作依据</summary>
          <code>{attention.action_digest}</code>
        </details>
      )}
      {attention.status === "pending" ? (
        attention.kind === "approval" ? (
          <div className="row">
            <button
              className="secondary"
              disabled={
                !enabled ||
                busy ||
                !attention.available_actions.includes("resolve") ||
                !attention.action
              }
              onClick={() => onDecision(attention, "reject")}
            >
              拒绝
            </button>
            <button
              className="primary"
              disabled={
                !enabled ||
                busy ||
                !attention.available_actions.includes("resolve") ||
                !attention.action
              }
              onClick={() => onDecision(attention, "approve")}
            >
              批准此操作
            </button>
          </div>
        ) : (
          <button
            className="secondary"
            disabled={
              !enabled ||
              busy ||
              !attention.available_actions.includes("resolve")
            }
            onClick={() => onAnswer(attention)}
          >
            在主聊天回答
          </button>
        )
      ) : (
        <p className="muted">
          {attention.resolution ?? statusLabel(attention.status)}
        </p>
      )}
    </section>
  );
}
export function ArtifactList({
  artifacts,
  onReference,
  onNavigate,
}: {
  artifacts: Artifact[];
  onReference: (ref: ContextReference) => void;
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
}) {
  return (
    <div className="artifact-list">
      {artifacts.length === 0 ? (
        <Empty>这项工作尚未保存产物。</Empty>
      ) : (
        artifacts.map((artifact) => (
          <article className="artifact-row" key={artifact.artifact_id}>
            <div>
              <strong>{artifact.name}</strong>
              <p className="muted">
                {artifact.mime} · {Math.ceil(artifact.size / 1024)} KiB ·{" "}
                {artifact.deletion_state === "active"
                  ? "摘要已保存"
                  : statusLabel(artifact.deletion_state)}
              </p>
            </div>
            <div className="row">
              {artifact.previewable && artifact.deletion_state === "active" && (
                <button
                  className="text-button"
                  data-content-id={`artifact/${artifact.artifact_id}`}
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
                  预览 <ArrowSquareOut size={14} />
                </button>
              )}
              {artifact.downloadable &&
                artifact.deletion_state === "active" && (
                  <a
                    className="text-button"
                    href={api.artifactUrl(artifact.artifact_id, true)}
                  >
                    下载
                  </a>
                )}
              <button
                className="text-button"
                disabled={artifact.deletion_state !== "active"}
                onClick={() =>
                  onReference({ kind: "artifact", id: artifact.artifact_id })
                }
              >
                引用
              </button>
            </div>
          </article>
        ))
      )}
    </div>
  );
}
export function ExecutionCard({
  execution,
  onOpen,
  onSupplement,
}: {
  execution: Execution;
  onOpen: (id: string, trigger: HTMLElement) => void;
  onSupplement: (execution: Execution) => void;
}) {
  return (
    <article className="execution-card">
      <button
        className="card-title"
        data-route-id={`execution/${execution.execution_id}`}
        onClick={(event) => onOpen(execution.execution_id, event.currentTarget)}
      >
        {execution.objective}
      </button>
      <Status value={execution.status} />
      {execution.result && <p className="muted clip">{execution.result}</p>}
      <div className="row">
        <span className="muted">
          {new Date(execution.created_at).toLocaleDateString("zh-CN")}
        </span>
        <button
          className="text-button"
          data-route-id={`execution/${execution.execution_id}/open`}
          onClick={(event) =>
            onOpen(execution.execution_id, event.currentTarget)
          }
        >
          查看工作
        </button>
        {!["succeeded", "failed", "cancelled"].includes(execution.status) && (
          <button
            className="text-button"
            onClick={() => onSupplement(execution)}
          >
            补充
          </button>
        )}
      </div>
    </article>
  );
}
interface Common {
  controls: WorkspaceControls;
  generation: number;
  version: number;
  fresh: boolean;
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
  onReference: (ref: ContextReference) => void;
  onSupplement: (execution: Execution) => void;
  refresh: () => void;
  onDirtyChange: (dirty: boolean) => void;
}

export function SpacesWorkspace({
  controls,
  spaces,
  executions,
  generation,
  version,
  onNavigate,
  onReference,
  onSupplement,
}: Common & { spaces: Space[]; executions: Execution[] }) {
  const query = controls.queries["spaces/query"] ?? "";
  const status = controls.queries["spaces/status"] ?? "active";
  const cursor = controls.pages.spaces ?? 0;
  const setCursor = (value: number) => controls.setPage("spaces", value);
  const setQuery = (value: string) => {
    controls.setQuery("spaces/query", value);
    setCursor(0);
  };
  const setStatus = (value: string) => {
    controls.setQuery("spaces/status", value);
    setCursor(0);
  };
  const list = useRecord(
    `spaces/${query}/${status}/${cursor}`,
    async (signal) => {
      const items: Space[] = [];
      let offset = 0;
      let result: Page<Space>;
      do {
        result = await api.spaces(query, status, offset, signal);
        items.push(...result.items);
        if (result.next_cursor === null || result.next_cursor > cursor) break;
        offset = result.next_cursor;
      } while (offset <= cursor);
      return { ...result, items };
    },
    generation,
    version,
  );
  const items = useMemo(() => {
    const values =
      list.value?.items ?? spaces.filter((space) => space.status === status);
    return [
      ...new Map(values.map((space) => [space.space_id, space])).values(),
    ];
  }, [list.value, spaces, status]);
  return (
    <>
      <div className="eyebrow">工作区 / 空间</div>
      <header className="workspace-heading">
        <div>
          <h1>我的空间</h1>
        </div>
        <div className="row">
          <input
            aria-label="搜索空间"
            placeholder="查找空间"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                setQuery("");
              } else if (
                event.key === "Enter" &&
                query.trim() &&
                !list.loading &&
                !list.error &&
                list.value?.total === 1 &&
                list.value.items.length === 1
              ) {
                event.preventDefault();
                onNavigate({
                  view: "space",
                  space_id: list.value.items[0].space_id,
                });
              }
            }}
          />
          <select
            aria-label="空间状态"
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="active">可用空间</option>
            <option value="archived">已归档</option>
          </select>
        </div>
      </header>
      <LoadState loading={list.loading} error={list.error} />
      {items.length === 0 && !list.loading ? (
        <Empty>暂无空间</Empty>
      ) : (
        <div className="space-grid">
          {items.map((space) => (
            <article
              className={`space-card ${space.active_execution_count ? "working" : ""}`}
              key={space.space_id}
            >
              <div className="row space-title">
                <span className="space-icon">
                  <Flask size={25} />
                </span>
                <div>
                  <button
                    className="card-title"
                    data-route-id={`space/${space.space_id}`}
                    onClick={(event) =>
                      onNavigate(
                        { view: "space", space_id: space.space_id },
                        event.currentTarget,
                      )
                    }
                  >
                    {space.name}
                  </button>
                  <p className="muted">
                    {space.purpose || "复用方法与能力，独立推进每项工作"}
                  </p>
                </div>
              </div>
              <div className="space-facts">
                {executions
                  .filter(
                    (execution) =>
                      execution.space_id === space.space_id &&
                      !["succeeded", "failed", "cancelled"].includes(
                        execution.status,
                      ),
                  )
                  .slice(0, 1)
                  .map((execution) => (
                    <p key={execution.execution_id}>
                      <span>当前工作</span>
                      <button
                        className="text-button current-work"
                        onClick={() =>
                          onNavigate({
                            view: "execution",
                            execution_id: execution.execution_id,
                          })
                        }
                      >
                        {execution.objective}
                      </button>
                    </p>
                  ))}
                <p>
                  <Status
                    value={
                      space.deletion_pending
                        ? "cancelling"
                        : space.status === "archived"
                          ? "archived"
                          : space.active_execution_count
                            ? "running"
                            : "active"
                    }
                  />
                  <span>{space.active_execution_count} 项进行中</span>
                </p>
                <p>
                  <span>工具与技能</span>
                  <span>
                    {space.tool_codes.length + space.skill_codes.length} 项绑定
                  </span>
                </p>
                {space.pending_attention_count > 0 && (
                  <p>
                    <span>需要处理</span>
                    <strong>{space.pending_attention_count} 项</strong>
                  </p>
                )}
              </div>
              <footer>
                <button
                  className="text-button"
                  data-route-id={`space/${space.space_id}/open`}
                  onClick={(event) =>
                    onNavigate(
                      { view: "space", space_id: space.space_id },
                      event.currentTarget,
                    )
                  }
                >
                  查看空间
                </button>
                <button
                  className="text-button"
                  disabled={space.deletion_pending || space.deleted}
                  onClick={(event) =>
                    space.status === "archived"
                      ? onNavigate(
                          {
                            view: "space",
                            space_id: space.space_id,
                            tab: "settings",
                          },
                          event.currentTarget,
                        )
                      : onReference({ kind: "space", id: space.space_id })
                  }
                >
                  {space.status === "archived" ? "恢复后使用" : "使用此空间"}
                </button>
              </footer>
            </article>
          ))}
        </div>
      )}
      {executions.some(
        (execution) =>
          !["succeeded", "failed", "cancelled"].includes(execution.status),
      ) && (
        <section className="overview-work">
          <h2>当前工作</h2>
          <div className="execution-list">
            {executions
              .filter(
                (execution) =>
                  !["succeeded", "failed", "cancelled"].includes(
                    execution.status,
                  ),
              )
              .map((execution) => (
                <ExecutionCard
                  key={execution.execution_id}
                  execution={execution}
                  onOpen={(id, trigger) =>
                    onNavigate({ view: "execution", execution_id: id }, trigger)
                  }
                  onSupplement={onSupplement}
                />
              ))}
          </div>
        </section>
      )}
      {list.value?.next_cursor !== null &&
        list.value?.next_cursor !== undefined && (
          <button
            className="secondary"
            onClick={() => {
              setCursor(list.value!.next_cursor!);
            }}
          >
            加载更多空间
          </button>
        )}
    </>
  );
}

export function SpaceWorkspace({
  controls,
  spaceId,
  tab = "overview",
  generation,
  version,
  fresh,
  onNavigate,
  onReference,
  onSupplement,
  refresh,
  onDirtyChange,
}: Common & { spaceId: string; tab?: string }) {
  const detail = useRecord(
    `space/${spaceId}`,
    (signal) => api.space(spaceId, signal),
    generation,
    version,
  );
  const jobsCursor = controls.pages[`space/${spaceId}`] ?? 0;
  const setJobsCursor = (value: number) =>
    controls.setPage(`space/${spaceId}`, value);
  const jobs = useRecord(
    `space-jobs/${spaceId}/${jobsCursor}`,
    (signal) => api.executions(spaceId, jobsCursor, signal),
    generation,
    version,
  );
  const settings = tab === "settings";
  const setSettings = (value: boolean) =>
    onNavigate({
      view: "space",
      space_id: spaceId,
      tab: value ? "settings" : "overview",
    });
  const [configuration, setConfiguration] = useState<SpaceConfiguration | null>(
      null,
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [preview, setPreview] = useState<DeletionPreview | null>(null),
    [conflict, setConflict] = useState<Space | null>(null),
    [archive, setArchive] = useState(false);
  const [modelPage, setModelPage] = useState<Page<Resource> | null>(null);
  const baseConfiguration = useRef("");
  const editingRevision = useRef(0);
  useEffect(() => {
    if (!settings || !detail.value || configuration !== null) return;
    const {
      name,
      purpose,
      instructions,
      model_role,
      tool_codes,
      skill_codes,
      resource_bindings,
    } = detail.value;
    const initial = {
      name,
      purpose,
      instructions,
      model_role,
      tool_codes,
      skill_codes,
      resource_bindings,
    };
    baseConfiguration.current = JSON.stringify(initial);
    editingRevision.current = detail.value.revision;
    setConfiguration(initial);
    void api
      .resources("models")
      .then(setModelPage)
      .catch((failure) => setError(errorMessage(failure)));
  }, [settings, detail.value, configuration]);
  useEffect(() => {
    onDirtyChange(
      settings &&
        configuration !== null &&
        JSON.stringify(configuration) !== baseConfiguration.current,
    );
  }, [settings, configuration, onDirtyChange]);
  useEffect(() => {
    if (!settings) setConfiguration(null);
  }, [settings]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);
  const space = detail.value;
  const perform = async (
    operation: () => Promise<unknown>,
    configurationWrite = false,
  ) => {
    setBusy(true);
    setError("");
    try {
      await operation();
      completeOperation(`space/${spaceId}`);
      onDirtyChange(false);
      refresh();
      setPreview(null);
      setArchive(false);
      if (preview === null) setSettings(false);
    } catch (failure) {
      setError(errorMessage(failure));
      refresh();
      if (
        configurationWrite &&
        failure instanceof ApiError &&
        failure.status === 409
      ) {
        try {
          setConflict(await api.space(spaceId));
        } catch (readFailure) {
          setError(`${errorMessage(failure)} ${errorMessage(readFailure)}`);
        }
      }
    } finally {
      setBusy(false);
    }
  };
  if (!space)
    return (
      <>
        <button
          className="text-button"
          onClick={() => onNavigate({ view: "spaces" })}
        >
          <ArrowLeft />
          返回空间
        </button>
        <LoadState loading={detail.loading} error={detail.error} />
      </>
    );
  return (
    <>
      <div className="eyebrow">空间 / {space.name}</div>
      <header className="workspace-heading">
        <div>
          <h1>{space.name}</h1>
          <p className="muted">{space.purpose}</p>
        </div>
        <div className="row">
          <Status value={space.status} />
          <button
            className="icon-button"
            aria-label="空间设置"
            disabled={!fresh || detail.loading || space.deletion_pending}
            onClick={() => {
              setSettings(!settings);
              setConfiguration(null);
            }}
          >
            <GearSix size={21} />
          </button>
        </div>
      </header>
      <div
        className="tabs"
        role="tablist"
        aria-label="空间内容"
        onKeyDown={navigateTabs}
      >
        {[
          ["overview", "概览"],
          ["work", "工作"],
          ["materials", "资料"],
          ["artifacts", "产物"],
          ["settings", "设置"],
        ].map(([value, label]) => (
          <button
            role="tab"
            aria-selected={tab === value}
            tabIndex={tab === value ? 0 : -1}
            key={value}
            onClick={() =>
              onNavigate({ view: "space", space_id: spaceId, tab: value })
            }
          >
            {label}
          </button>
        ))}
      </div>
      <p className="inline-error" role={error ? "alert" : undefined}>
        {error}
      </p>
      {tab === "materials" ? (
        <section>
          <h2>绑定资料与宿主资源</h2>
          {space.resource_bindings.length === 0 ? (
            <Empty>未绑定资源</Empty>
          ) : (
            space.resource_bindings.map((resource) => (
              <article className="artifact-row" key={resource}>
                <code>{resource}</code>
                <button
                  className="text-button"
                  onClick={(event) =>
                    onNavigate({ view: "resources" }, event.currentTarget)
                  }
                >
                  查看宿主资源目录
                </button>
              </article>
            ))
          )}
        </section>
      ) : tab === "artifacts" ? (
        <SpaceArtifacts
          spaceId={spaceId}
          generation={generation}
          version={version}
          onNavigate={onNavigate}
          onReference={onReference}
        />
      ) : settings && configuration ? (
        <form
          id="space-settings-form"
          className="settings-form"
          onSubmit={(event) => {
            event.preventDefault();
            void perform(
              () =>
                api.updateSpace(
                  { ...space, revision: editingRevision.current },
                  configuration,
                  operationId(`space/${spaceId}`, {
                    action: "update",
                    revision: editingRevision.current,
                    configuration,
                  }),
                ),
              true,
            );
          }}
        >
          <h2>空间配置</h2>
          <button
            type="button"
            className="text-button"
            onClick={(event) =>
              onNavigate({ view: "resources" }, event.currentTarget)
            }
          >
            查看资源目录
          </button>
          {conflict && (
            <section className="notice warning" aria-label="空间配置版本冲突">
              <h3>空间配置已被修改</h3>
              {(
                [
                  "name",
                  "purpose",
                  "instructions",
                  "model_role",
                  "tool_codes",
                  "skill_codes",
                  "resource_bindings",
                ] as const
              )
                .filter(
                  (field) =>
                    JSON.stringify(configuration[field]) !==
                    JSON.stringify(conflict[field]),
                )
                .map((field) => (
                  <div key={field}>
                    <strong>
                      {
                        {
                          name: "名称",
                          purpose: "职责",
                          instructions: "默认方法",
                          model_role: "模型角色",
                          tool_codes: "工具",
                          skill_codes: "技能",
                          resource_bindings: "资源绑定",
                        }[field]
                      }
                    </strong>
                    <p>
                      服务端 <code>{JSON.stringify(conflict[field])}</code>
                    </p>
                    <p>
                      当前草稿{" "}
                      <code>{JSON.stringify(configuration[field])}</code>
                    </p>
                  </div>
                ))}
              <div className="row">
                <button
                  type="button"
                  className="secondary"
                  onClick={() => {
                    const next = {
                      name: conflict.name,
                      purpose: conflict.purpose,
                      instructions: conflict.instructions,
                      model_role: conflict.model_role,
                      tool_codes: conflict.tool_codes,
                      skill_codes: conflict.skill_codes,
                      resource_bindings: conflict.resource_bindings,
                    };
                    editingRevision.current = conflict.revision;
                    baseConfiguration.current = JSON.stringify(next);
                    setConfiguration(next);
                    setConflict(null);
                    setError("");
                  }}
                >
                  载入服务端配置，放弃当前草稿
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => {
                    const next = {
                      name: conflict.name,
                      purpose: conflict.purpose,
                      instructions: conflict.instructions,
                      model_role: conflict.model_role,
                      tool_codes: conflict.tool_codes,
                      skill_codes: conflict.skill_codes,
                      resource_bindings: conflict.resource_bindings,
                    };
                    editingRevision.current = conflict.revision;
                    baseConfiguration.current = JSON.stringify(next);
                    setConfiguration({ ...configuration });
                    setConflict(null);
                    setError("");
                  }}
                >
                  保留草稿，基于新版本继续编辑
                </button>
              </div>
            </section>
          )}
          <label>
            名称
            <input
              required
              value={configuration.name}
              onChange={(event) =>
                setConfiguration({ ...configuration, name: event.target.value })
              }
            />
          </label>
          <label>
            职责
            <input
              value={configuration.purpose}
              onChange={(event) =>
                setConfiguration({
                  ...configuration,
                  purpose: event.target.value,
                })
              }
            />
          </label>
          <label>
            默认方法
            <textarea
              rows={6}
              value={configuration.instructions}
              onChange={(event) =>
                setConfiguration({
                  ...configuration,
                  instructions: event.target.value,
                })
              }
            />
          </label>
          <label>
            模型角色
            <select
              value={configuration.model_role}
              onChange={(event) =>
                setConfiguration({
                  ...configuration,
                  model_role: event.target.value,
                })
              }
            >
              <option value={configuration.model_role}>
                {modelPage?.items.find(
                  (model) => model.code === configuration.model_role,
                )?.name ?? modelCategory(configuration.model_role)}
              </option>
              {modelPage?.items
                .filter((model) => model.code !== configuration.model_role)
                .map((model) => (
                  <option
                    key={model.code}
                    value={model.code}
                    disabled={!model.available}
                  >
                    {model.name}
                  </option>
                ))}
            </select>
          </label>
          <ResourceBindings
            generation={generation}
            kind="tools"
            selected={configuration.tool_codes}
            onChange={(values) =>
              setConfiguration({ ...configuration, tool_codes: values })
            }
          />
          <ResourceBindings
            generation={generation}
            kind="skills"
            selected={configuration.skill_codes}
            onChange={(values) =>
              setConfiguration({ ...configuration, skill_codes: values })
            }
          />
          <ResourceBindings
            generation={generation}
            kind="environments"
            selected={configuration.resource_bindings}
            onChange={(values) =>
              setConfiguration({ ...configuration, resource_bindings: values })
            }
          />
          <div className="row">
            <button
              className="primary"
              disabled={
                !fresh ||
                detail.loading ||
                busy ||
                conflict !== null ||
                !space.available_actions.includes("update")
              }
            >
              保存配置
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => setSettings(false)}
            >
              放弃修改
            </button>
          </div>
          <hr />
          <div className="row">
            <button
              type="button"
              className="secondary"
              disabled={
                !fresh ||
                busy ||
                !space.available_actions.includes(
                  space.status === "archived" ? "restore" : "archive",
                )
              }
              onClick={() =>
                space.status === "archived"
                  ? void perform(() =>
                      api.archive(
                        space,
                        true,
                        operationId(`space/${spaceId}`, {
                          action: "restore",
                          revision: space.revision,
                        }),
                      ),
                    )
                  : setArchive(true)
              }
            >
              {space.status === "archived" ? "恢复空间" : "归档空间"}
            </button>
            <button
              type="button"
              className="danger"
              disabled={
                !fresh || busy || !space.available_actions.includes("delete")
              }
              onClick={() => {
                setError("");
                void api
                  .deletionPreview(space.space_id)
                  .then(setPreview)
                  .catch((failure) => setError(errorMessage(failure)));
              }}
            >
              <Trash />
              删除预览
            </button>
          </div>
        </form>
      ) : (
        <>
          <div className="space-summary">
            <Flask size={24} />
            <div>
              <h2>空间方法</h2>
              {space.instructions && (
                <div className="markdown">
                  <Markdown remarkPlugins={[remarkGfm]}>
                    {space.instructions}
                  </Markdown>
                </div>
              )}
            </div>
            <button
              className="text-button"
              disabled={!fresh || space.deletion_pending}
              onClick={() => onReference({ kind: "space", id: space.space_id })}
            >
              在主聊天引用
            </button>
          </div>
          <h2>关联工作</h2>
          <LoadState loading={jobs.loading} error={jobs.error} />
          <div className="execution-list">
            {jobs.value?.items.map((execution) => (
              <ExecutionCard
                key={execution.execution_id}
                execution={execution}
                onOpen={(id, trigger) =>
                  onNavigate({ view: "execution", execution_id: id }, trigger)
                }
                onSupplement={onSupplement}
              />
            ))}
          </div>
          <PageControls
            cursor={jobsCursor}
            page={jobs.value}
            loading={jobs.loading}
            onCursor={setJobsCursor}
          />
          {jobs.value?.total === 0 && <Empty>暂无关联工作</Empty>}
        </>
      )}
      {archive && (
        <Modal title="确认归档空间" onClose={() => setArchive(false)}>
          <p>
            “{space.name}”仍有 {space.active_execution_count} 项进行中的工作。
            归档后这些工作继续推进，空间停止接收新工作。
          </p>
          <div className="row">
            <button
              data-initial-focus
              className="secondary"
              onClick={() => setArchive(false)}
            >
              继续使用空间
            </button>
            <button
              className="primary"
              disabled={
                !fresh ||
                detail.loading ||
                busy ||
                !space.available_actions.includes("archive")
              }
              onClick={() =>
                void perform(() =>
                  api.archive(
                    space,
                    false,
                    operationId(`space/${spaceId}`, {
                      action: "archive",
                      revision: space.revision,
                    }),
                  ),
                )
              }
            >
              确认归档
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
        </Modal>
      )}
      {preview && (
        <Modal title="核对空间删除影响" onClose={() => setPreview(null)}>
          <p>
            将清理 {preview.execution_ids.length} 项历史工作与{" "}
            {preview.artifact_ids.length} 份自有产物。
            {preview.shared_artifact_ids.length} 份共享产物继续保留。
          </p>
          <p>
            关联 {preview.scopes.sdk_session_count} 份执行记录、
            {preview.scopes.task_file_count} 个任务文件和{" "}
            {preview.scopes.space_fact_count} 条空间事实。
          </p>
          <p className="muted">
            预览有效期至{" "}
            {new Date(preview.expires_at).toLocaleTimeString("zh-CN")}
          </p>
          {preview.blockers.map((blocker) => (
            <p className="inline-error" key={blocker}>
              {blocker}
            </p>
          ))}
          <div className="row">
            <button
              data-initial-focus
              className="secondary"
              onClick={() => setPreview(null)}
            >
              返回
            </button>
            <button
              className="danger"
              disabled={!fresh || busy || preview.blockers.length > 0}
              onClick={() =>
                void perform(async () => {
                  await api.deleteSpace(
                    space,
                    preview,
                    operationId(`space/${spaceId}`, {
                      action: "delete",
                      revision: space.revision,
                      preview_token: preview.preview_token,
                    }),
                  );
                  onNavigate({ view: "spaces" });
                })
              }
            >
              确认删除空间
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
function ResourceBindings({
  generation,
  kind,
  selected,
  onChange,
}: {
  generation: number;
  kind: string;
  selected: string[];
  onChange: (values: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const label =
    kind === "tools" ? "工具" : kind === "skills" ? "技能" : "宿主资源";
  useEffect(() => setCursor(0), [query, generation]);
  const list = useRecord(
    `binding/${kind}/${query}/${cursor}`,
    (signal) => api.resources(kind, query, cursor, signal),
    generation,
    0,
  );
  return (
    <fieldset>
      <legend>{label}</legend>
      <input
        aria-label={`搜索可绑定${label}`}
        value={query}
        placeholder={`搜索${label}`}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            setQuery("");
          }
        }}
      />
      {selected.length > 0 && (
        <div className="chips" aria-label={`已选择${label}`}>
          {selected.map((code) => (
            <span className="chip" key={code}>
              {capabilityName(code)}
              <button
                type="button"
                aria-label={`移除已绑定${label} ${capabilityName(code)}`}
                onClick={() =>
                  onChange(selected.filter((value) => value !== code))
                }
              >
                <X size={13} />
              </button>
            </span>
          ))}
        </div>
      )}
      <LoadState loading={list.loading} error={list.error} />
      {list.value?.items.map((resource) => (
        <label className="checkbox" key={resource.code}>
          <input
            type="checkbox"
            checked={selected.includes(resource.code)}
            disabled={list.loading || !resource.available}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...selected, resource.code]
                  : selected.filter((code) => code !== resource.code),
              )
            }
          />
          {capabilityName(resource.name)}
          {!resource.available && (
            <span className="muted">{resource.disabled_reason}</span>
          )}
        </label>
      ))}
      {list.value?.total === 0 && <p className="muted">没有匹配的{label}。</p>}
      <PageControls
        cursor={cursor}
        page={list.value}
        loading={list.loading}
        onCursor={setCursor}
      />
    </fieldset>
  );
}

export function ExecutionWorkspace({
  controls,
  executionId,
  tab = "overview",
  graphRunId,
  runId,
  nodeId,
  nodeAttemptId,
  generation,
  version,
  fresh,
  onNavigate,
  onReference,
  onSupplement,
  onAnswer,
  onDecision,
  refresh,
}: Common & {
  executionId: string;
  tab?: string;
  graphRunId?: string;
  runId?: string;
  nodeId?: string;
  nodeAttemptId?: string;
  onAnswer: (attention: Attention) => void;
  onDecision: (attention: Attention, decision: "approve" | "reject") => void;
}) {
  const detail = useRecord(
    `execution/${executionId}`,
    (signal) => api.execution(executionId, signal),
    generation,
    version,
  );
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [cancel, setCancel] = useState(false),
    [retry, setRetry] = useState(false),
    [supplement, setSupplement] = useState("");
  const execution = detail.value;
  const control = async (action: "pause" | "resume" | "cancel" | "retry") => {
    if (!execution) return;
    setBusy(true);
    setError("");
    try {
      const requestId = operationId(`execution/${executionId}`, {
        action,
        revision: execution.revision,
        supplement: action === "retry" ? supplement : "",
      });
      if (action === "retry") {
        const result = await api.retryExecution(
          execution,
          requestId,
          supplement,
        );
        onNavigate({
          view: "execution",
          execution_id: result.new_execution_id,
        });
      } else {
        await api.control(execution, action, requestId);
      }
      completeOperation(`execution/${executionId}`);
      refresh();
      setCancel(false);
      setRetry(false);
    } catch (failure) {
      setError(errorMessage(failure));
      refresh();
    } finally {
      setBusy(false);
    }
  };
  if (!execution)
    return (
      <>
        <LoadState loading={detail.loading} error={detail.error} />
        <button
          className="secondary"
          onClick={() => onNavigate({ view: "spaces" })}
        >
          返回空间
        </button>
      </>
    );
  return (
    <>
      <div className="eyebrow">
        <button
          className="text-button"
          onClick={() =>
            onNavigate({
              view: "space",
              space_id: execution.space_id,
              tab: "work",
            })
          }
        >
          空间
        </button>{" "}
        / {execution.config_snapshot.name}
        {execution.parent_execution_id && (
          <button
            className="text-button"
            onClick={() =>
              onNavigate({
                view: "execution",
                execution_id: execution.parent_execution_id!,
              })
            }
          >
            返回父工作
          </button>
        )}
      </div>
      <header className="workspace-heading">
        <div>
          <h1>{objectiveTitle(execution.objective)}</h1>
          <div className="row">
            <Status value={execution.status} />
            {execution.pause_requested && (
              <span className="muted">正在暂停</span>
            )}
          </div>
        </div>
        <div className="row control-actions">
          {(["pause", "resume", "cancel", "retry"] as const)
            .filter((action) => execution.available_actions.includes(action))
            .map((action) => (
              <button
                key={action}
                className={action === "cancel" ? "danger" : "secondary"}
                disabled={!fresh || detail.loading || busy}
                onClick={() =>
                  action === "cancel"
                    ? setCancel(true)
                    : action === "retry"
                      ? setRetry(true)
                      : void control(action)
                }
              >
                {action === "pause" ? (
                  <Pause />
                ) : action === "resume" ? (
                  <Play />
                ) : action === "cancel" ? (
                  <Stop />
                ) : (
                  <Clock />
                )}
                {
                  {
                    pause: "暂停",
                    resume: "继续",
                    cancel: "取消",
                    retry: "重新执行",
                  }[action]
                }
              </button>
            ))}
        </div>
      </header>
      {objectiveTitle(execution.objective) !== execution.objective && (
        <details className="work-instructions">
          <summary>完整任务要求</summary>
          <div className="markdown">
            <Markdown remarkPlugins={[remarkGfm]}>
              {execution.objective}
            </Markdown>
          </div>
        </details>
      )}
      <div
        className="tabs"
        role="tablist"
        aria-label="工作详情"
        onKeyDown={navigateTabs}
      >
        {[
          ["overview", "概览"],
          ["process", "过程"],
          ["artifacts", "产物"],
          ["materials", "资料"],
        ].map(([value, label], index) => (
          <button
            role="tab"
            aria-selected={tab === value}
            tabIndex={tab === value ? 0 : -1}
            key={value}
            onKeyDown={(event) => {
              if (
                ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
              ) {
                event.preventDefault();
                const values = [
                  "overview",
                  "process",
                  "artifacts",
                  "materials",
                ];
                const next =
                  event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? 3
                      : (index + (event.key === "ArrowRight" ? 1 : 3)) % 4;
                onNavigate({
                  view: "execution",
                  execution_id: executionId,
                  tab: values[next],
                  graph_run_id: graphRunId,
                  run_id: runId,
                  node_id: nodeId,
                  node_attempt_id: nodeAttemptId,
                });
              }
            }}
            onClick={() =>
              onNavigate({
                view: "execution",
                execution_id: executionId,
                tab: value,
                graph_run_id: graphRunId,
                run_id: runId,
                node_id: nodeId,
                node_attempt_id: nodeAttemptId,
              })
            }
          >
            {label}
            {value === "artifacts" && execution.artifacts.length > 0 && (
              <span>{execution.artifacts.length}</span>
            )}
          </button>
        ))}
      </div>
      <p className="inline-error" role={error ? "alert" : undefined}>
        {error}
      </p>
      {tab === "overview" ? (
        <>
          <div className="row section-actions">
            <button
              className="text-button"
              disabled={
                !fresh || !execution.available_actions.includes("supplement")
              }
              onClick={() => onSupplement(execution)}
            >
              补充这项工作
            </button>
            <button
              className="text-button"
              onClick={() =>
                onReference({ kind: "execution", id: execution.execution_id })
              }
            >
              引用工作
            </button>
          </div>
          {execution.reason && (
            <p className="inline-error">{execution.reason}</p>
          )}
          {execution.unknown_effects.length > 0 && (
            <div className="notice warning">
              有 {execution.unknown_effects.length}{" "}
              项外部动作需要核实，处理前请查看恢复事项。
            </div>
          )}
          {execution.attentions.map((attention) => (
            <AttentionCard
              key={attention.attention_id}
              attention={attention}
              onNavigate={onNavigate}
              onAnswer={onAnswer}
              onDecision={onDecision}
              busy={busy}
              enabled={fresh && !detail.loading}
            />
          ))}
          {execution.result && (
            <section className="result markdown">
              <h2>交付结果</h2>
              <Markdown remarkPlugins={[remarkGfm]}>
                {execution.result}
              </Markdown>
            </section>
          )}
          {execution.pending_prerequisites.length > 0 && (
            <p className="notice">
              当前等待{" "}
              {execution.pending_prerequisites
                .map(
                  (value) =>
                    ({
                      recovery: "恢复核实",
                      approval: "权限审批",
                      input: "用户补充",
                      children: "必需子工作",
                    })[value] ?? value,
                )
                .join("、")}
            </p>
          )}
          <details className="report">
            <summary>交付要求与验证证据</summary>
            <p>
              {execution.delivery_contract.require_text
                ? "需要文字交付"
                : "未要求文字交付"}
            </p>
            <p>
              要求的产物{" "}
              {execution.delivery_contract.required_artifact_ids.length
                ? execution.delivery_contract.required_artifact_ids.join("、")
                : "未指定"}
            </p>
            <p>
              要求的验证{" "}
              {execution.delivery_contract.required_verifications.length
                ? execution.delivery_contract.required_verifications
                    .map((item) => item.name)
                    .join("、")
                : "未指定"}
            </p>
            {execution.reports.flatMap((report) =>
              report.verification.map((verification, index) => (
                <article key={`${report.report_id}/${index}`}>
                  <strong>{verification.name}</strong>
                  <p>
                    {
                      { passed: "已通过", failed: "未通过", unknown: "未核实" }[
                        verification.status
                      ]
                    }{" "}
                    ·{" "}
                    {verification.executed_at
                      ? new Date(verification.executed_at).toLocaleString(
                          "zh-CN",
                        )
                      : "未提供执行时间"}
                  </p>
                  <p>
                    证据{" "}
                    {verification.evidence_refs.length
                      ? verification.evidence_refs.join("、")
                      : "未提供"}
                  </p>
                </article>
              )),
            )}
          </details>
          <section>
            <h2>最新进展</h2>
            {execution.reports.length === 0 ? (
              <Empty>暂无进展</Empty>
            ) : (
              execution.reports
                .slice(-5)
                .reverse()
                .map((report) => (
                  <article className="report" key={report.report_id}>
                    <strong>
                      {
                        {
                          progress: "进展",
                          ready: "交付候选",
                          needs_input: "需要补充",
                          failed: "执行失败",
                        }[report.kind]
                      }
                    </strong>
                    <p>{report.summary}</p>
                    {report.rejection_reasons.map((reason) => (
                      <p className="inline-error" key={reason}>
                        {reason}
                      </p>
                    ))}
                  </article>
                ))
            )}
          </section>
          {execution.children.length > 0 && (
            <section>
              <h2>独立子工作</h2>
              {execution.children.map((child) => (
                <ExecutionCard
                  key={child.execution_id}
                  execution={child}
                  onOpen={(id, trigger) =>
                    onNavigate({ view: "execution", execution_id: id }, trigger)
                  }
                  onSupplement={onSupplement}
                />
              ))}
            </section>
          )}
          <div className="budget">
            额度 {execution.budget_limit.toLocaleString()} · 已确认使用{" "}
            {execution.budget_consumed.toLocaleString()} · 已预留{" "}
            {execution.budget_reserved.toLocaleString()}
          </div>
        </>
      ) : tab === "process" ? (
        <ProcessWorkspace
          controls={controls}
          execution={execution}
          graphRunId={graphRunId}
          runId={runId}
          nodeId={nodeId}
          nodeAttemptId={nodeAttemptId}
          generation={generation}
          version={version}
          onNavigate={onNavigate}
          onReference={onReference}
        />
      ) : tab === "artifacts" ? (
        <ArtifactList
          artifacts={execution.artifacts}
          onReference={onReference}
          onNavigate={onNavigate}
        />
      ) : (
        <section className="materials-section">
          <h2>工作说明</h2>
          <div className="markdown work-instructions">
            <Markdown remarkPlugins={[remarkGfm]}>
              {execution.config_snapshot.instructions}
            </Markdown>
          </div>
          <h2>工具与技能</h2>
          <ul className="capability-list">
            {[
              ...execution.config_snapshot.tool_codes,
              ...execution.config_snapshot.skill_codes,
            ].map((code) => (
              <li key={code}>
                <CapabilityIcon code={code} />
                <span>{capabilityName(code)}</span>
              </li>
            ))}
          </ul>
          <ExecutionInputs
            execution={execution}
            fresh={fresh && !detail.loading}
            refresh={refresh}
          />
          {execution.source_execution_id && (
            <button
              className="text-button"
              onClick={() =>
                onNavigate({
                  view: "execution",
                  execution_id: execution.source_execution_id!,
                })
              }
            >
              查看再执行来源
            </button>
          )}
        </section>
      )}
      {retry && (
        <Modal title="重新执行这项目标" onClose={() => setRetry(false)}>
          <p>
            将以当前空间配置创建新的独立工作，沿用目标和交付要求。原工作的结果与动作记录保留在来源中。
          </p>
          <p>
            <strong>{execution.objective}</strong>
          </p>
          <label>
            补充要求
            <textarea
              value={supplement}
              onChange={(event) => setSupplement(event.target.value)}
            />
          </label>
          <div className="row">
            <button className="secondary" onClick={() => setRetry(false)}>
              返回
            </button>
            <button
              disabled={
                busy || !fresh || !execution.available_actions.includes("retry")
              }
              onClick={() => void control("retry")}
            >
              确认重新执行
            </button>
          </div>
        </Modal>
      )}
      {cancel && (
        <Modal title="取消这项工作" onClose={() => setCancel(false)}>
          <p>
            Weave
            将停止后续动作，并结算这项工作拥有的运行资源与子工作。已完成的外部动作会保留实际记录。
          </p>
          <LoadState loading={detail.loading} error={detail.error} />
          {execution.children.length > 0 && (
            <section aria-label="关联子工作">
              <h3>当前关联的子工作</h3>
              {execution.children.map((child) => (
                <p key={child.execution_id}>
                  <strong>{child.objective}</strong> ·{" "}
                  {statusLabel(child.status)} ·{" "}
                  {["succeeded", "failed", "cancelled"].includes(child.status)
                    ? "已结束"
                    : "将请求取消"}
                </p>
              ))}
            </section>
          )}
          <div className="row">
            <button
              data-initial-focus
              className="secondary"
              onClick={() => setCancel(false)}
            >
              返回
            </button>
            <button
              className="danger"
              disabled={
                busy ||
                !fresh ||
                detail.loading ||
                !execution.available_actions.includes("cancel")
              }
              onClick={() => void control("cancel")}
            >
              确认取消
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}

function ExecutionInputs({
  execution,
  fresh,
  refresh,
}: {
  execution: ExecutionDetail;
  fresh: boolean;
  refresh: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const retry = async (delivery: Delivery) => {
    setBusy(delivery.delivery_id);
    setError("");
    const scope = `delivery/${delivery.delivery_id}`;
    try {
      await api.retryDelivery(
        delivery,
        operationId(scope, { generation: delivery.generation }),
      );
      completeOperation(scope);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      refresh();
      setBusy(null);
    }
  };
  const standaloneDeliveries = execution.deliveries.filter(
    (delivery) =>
      delivery.target_kind !== "execution" ||
      !execution.inputs.some((input) => input.input_id === delivery.input_id),
  );
  return (
    <section className="input-history">
      <h3>收到的要求</h3>
      {error && (
        <p className="inline-error" role="alert">
          {error}
        </p>
      )}
      {execution.inputs.length === 0 && <Empty>暂无要求</Empty>}
      {execution.inputs.map((input) => {
        const consumption = input.sdk_consumption;
        const disposition = execution.dispositions.find(
          (item) => item.input_id === input.input_id,
        );
        const handback = execution.handbacks.find(
          (item) => item.handback_id === disposition?.handback_id,
        );
        const delivery = execution.deliveries.find(
          (item) =>
            item.input_id === input.input_id &&
            item.target_kind === "execution",
        );
        return (
          <article className="report input-message" key={input.input_id}>
            <div className="markdown">
              <Markdown remarkPlugins={[remarkGfm]}>{input.content}</Markdown>
            </div>
            <div className="input-facts">
              <span>
                {consumption !== null || delivery?.state === "delivered"
                  ? "已送达"
                  : delivery?.state === "failed"
                    ? "发送失败"
                    : "待送达"}
              </span>
              {consumption && (
                <span>
                  {
                    {
                      accepted: "待助手读取",
                      claimed: "等待读取",
                      consumed: "助手已读取",
                    }[consumption.state]
                  }
                </span>
              )}
              {disposition && (
                <span>
                  {
                    {
                      owned: "由本任务处理",
                      handled: "已处理",
                      returned: "已退回",
                    }[disposition.state]
                  }
                </span>
              )}
            </div>
            {delivery && (
              <DeliveryFeedback
                delivery={delivery}
                enabled={fresh && busy === null}
                onRetry={retry}
              />
            )}
            {disposition && disposition.outcome_refs.length > 0 && (
              <div>
                <strong>相关结果</strong>
                {disposition.outcome_refs.map((id) => (
                  <p key={id}>
                    {execution.reports.find((report) => report.report_id === id)
                      ?.summary ??
                      execution.artifacts.find(
                        (artifact) => artifact.artifact_id === id,
                      )?.name ??
                      "结果详情暂不可用"}
                  </p>
                ))}
              </div>
            )}
            {handback && (
              <div className="notice">
                <strong>退回原因</strong>
                <p>{handback.reason}</p>
                {handback.already_handled_refs.length > 0 && (
                  <p>已处理 {handback.already_handled_refs.length} 项</p>
                )}
                {handback.effect_refs.length > 0 && (
                  <p>已执行 {handback.effect_refs.length} 项操作</p>
                )}
                <HandbackEvidence
                  handback={handback}
                  resolution={execution.handback_resolutions.find(
                    (value) => value.handback_id === handback.handback_id,
                  )}
                />
              </div>
            )}
          </article>
        );
      })}
      {standaloneDeliveries.length > 0 && <h3>发送记录</h3>}
      {standaloneDeliveries.map((delivery) => (
        <article className="report" key={delivery.delivery_id}>
          <strong>
            {delivery.target_kind === "router"
              ? "向主聊天反馈报告"
              : "向任务发送消息"}
          </strong>
          <p>
            {
              {
                pending: "等待发送",
                delivered: "已送达",
                failed: "发送失败",
              }[delivery.state]
            }
          </p>
          <DeliveryFeedback
            delivery={delivery}
            enabled={fresh && busy === null}
            onRetry={retry}
          />
        </article>
      ))}
    </section>
  );
}

function DeliveryFeedback({
  delivery,
  enabled,
  onRetry,
}: {
  delivery: Delivery;
  enabled: boolean;
  onRetry: (delivery: Delivery) => Promise<void>;
}) {
  return (
    <>
      {delivery.attempts > 1 && (
        <p className="muted">已尝试 {delivery.attempts} 次</p>
      )}
      {delivery.last_error && (
        <p className="inline-error">{delivery.last_error}</p>
      )}
      {delivery.state === "failed" && (
        <button
          className="secondary"
          disabled={!enabled || !delivery.available_actions.includes("retry")}
          title={delivery.disabled_reasons.retry}
          onClick={() => void onRetry(delivery)}
        >
          重新发送
        </button>
      )}
    </>
  );
}

type WorkNode = Node<{ label: string; status: string }, "work">;
function GraphNodeCard({ data }: NodeProps<WorkNode>) {
  return (
    <div className={`graph-node status-border-${data.status}`}>
      <Handle type="target" position={Position.Left} />
      <strong>{data.label}</strong>
      <Status value={data.status} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}
const nodeTypes = { work: GraphNodeCard };
function storedGraphViewport(graphRunId: string): Viewport | null {
  const raw = sessionStorage.getItem(`weave-graph-viewport/${graphRunId}`);
  if (raw === null) return null;
  const value: unknown = JSON.parse(raw);
  if (
    value !== null &&
    typeof value === "object" &&
    "x" in value &&
    "y" in value &&
    "zoom" in value &&
    typeof value.x === "number" &&
    typeof value.y === "number" &&
    typeof value.zoom === "number" &&
    [value.x, value.y, value.zoom].every(Number.isFinite) &&
    value.zoom > 0
  )
    return { x: value.x, y: value.y, zoom: value.zoom };
  throw new Error("图视口记录无效，请重置该运行的浏览位置。");
}
function GraphCanvas({
  graph,
  run,
  attempts,
  selectedNodeId,
  onNode,
}: {
  graph: Graph;
  run: GraphRun;
  attempts: NodeAttempt[];
  selectedNodeId: string;
  onNode: (id: string) => void;
}) {
  const [viewport] = useState(() => storedGraphViewport(run.graph_run_id));
  const canvas = useRef<HTMLDivElement>(null);
  const previousSelection = useRef(selectedNodeId);
  useEffect(() => {
    const previous = previousSelection.current;
    previousSelection.current = selectedNodeId;
    const container = canvas.current;
    if (!previous || selectedNodeId || !container) return;
    const restore = () => {
      const target = container.querySelector<HTMLElement>(
        `.react-flow__node[data-id="${CSS.escape(previous)}"]`,
      );
      target?.focus({ preventScroll: true });
      return target !== null && document.activeElement === target;
    };
    if (restore()) return;
    const observer = new MutationObserver(() => {
      if (restore()) observer.disconnect();
    });
    observer.observe(container, {
      attributes: true,
      childList: true,
      subtree: true,
    });
    return () => observer.disconnect();
  }, [selectedNodeId]);
  const elements = useMemo(() => {
    try {
      const positions = graphPositions(graph);
      const latest = new Map<string, NodeAttempt>();
      for (const attempt of attempts)
        if (
          !latest.has(attempt.node_id) ||
          latest.get(attempt.node_id)!.attempt < attempt.attempt
        )
          latest.set(attempt.node_id, attempt);
      const nodes: WorkNode[] = graph.nodes.map((node) => ({
        id: node.node_id,
        type: "work",
        position: positions.get(node.node_id)!,
        selected: node.node_id === selectedNodeId,
        data: {
          label: node.objective,
          status:
            run.state?.node_states.find(
              (value) => value.node_id === node.node_id,
            )?.status ??
            latest.get(node.node_id)?.status ??
            "unknown",
        },
      }));
      const edges: Edge[] = graph.edges.map((edge, index) => ({
        id: String(index),
        source: edge.source_node_id,
        target: edge.target_node_id,
      }));
      return { nodes, edges, error: "" };
    } catch (failure) {
      return { nodes: [], edges: [], error: errorMessage(failure) };
    }
  }, [graph, run, attempts, selectedNodeId]);
  if (elements.error)
    return (
      <p role="alert" className="inline-error">
        {elements.error}
      </p>
    );
  return (
    <div
      ref={canvas}
      className="graph-canvas"
      aria-label="任务图"
      onKeyDown={(event) => {
        if (event.key !== "Enter" || !(event.target instanceof HTMLElement))
          return;
        const identity =
          event.target.closest<HTMLElement>(".react-flow__node")?.dataset.id;
        if (identity) {
          event.preventDefault();
          onNode(identity);
        }
      }}
    >
      <ReactFlow
        nodes={elements.nodes}
        edges={elements.edges}
        nodeTypes={nodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        defaultViewport={viewport ?? undefined}
        fitView={viewport === null}
        onMoveEnd={(_event, value) =>
          sessionStorage.setItem(
            `weave-graph-viewport/${run.graph_run_id}`,
            JSON.stringify(value),
          )
        }
        onNodeClick={(_event, node) => onNode(node.id)}
      >
        <Background color="#dce0ed" gap={22} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
function objectiveTitle(objective: string): string {
  if (objective.length <= 100) return objective;
  const firstSentence = objective.split(/[。\n]/, 1)[0];
  return firstSentence.length > 100
    ? `${firstSentence.slice(0, 100)}…`
    : firstSentence;
}

function capabilityName(code: string): string {
  const labels: Record<string, string> = {
    command: "执行命令",
    read_file: "读取文件",
    write_file: "写入文件",
    query_work: "查看任务资料",
    query_product: "查看工作区资料",
    read_tool_result: "读取运行结果",
    return_to_router: "退回任务消息",
    delegate_execution: "分配子任务",
    consult_specialist: "咨询专项助手",
    submit_subagent: "分配助手任务",
    wait_subagents: "等待助手结果",
    submit_graph: "安排任务流程",
    inspect_graph: "查看任务流程",
    execute_graph: "执行任务流程",
    workspace_resource: "读取工作区资料",
    "weave.query_work": "查看任务资料",
    query: "查询工作信息",
    ask_user: "向你提问",
    report_progress: "更新任务进展",
    finish_work: "提交任务结果",
    return_inputs: "退回任务消息",
    delegate_work: "分配子任务",
    search_capabilities: "查找可用工具",
    execution_content: "处理任务文件",
    "web-research": "网络研究",
    "document-writing": "文档写作",
    "verification-before-completion": "完成前检查",
    "task-analysis": "任务拆解",
    "data-audit": "数据检查",
    "tool-development": "工具开发",
    "systematic-debugging": "问题排查",
    "travel-planning": "行程规划",
    "example.tool.text_statistics": "文本统计",
    text_statistics: "文本统计",
    "data.tool.csv_profile": "表格分析",
    csv_profile: "表格分析",
  };
  return (
    labels[code] ??
    (code.startsWith("weave.") ? labels[code.slice(6)] : undefined) ??
    code
  );
}

function modelCategory(role: string): string {
  const categories: Record<string, string> = {
    text: "文本对话",
    vision: "图像理解",
    embedding: "文本向量",
    image: "图像生成",
    audio: "音频处理",
  };
  return categories[role] ?? "模型";
}

function CapabilityIcon({ code }: { code: string }) {
  const Icon =
    code === "command"
      ? Terminal
      : code === "read_file" || code === "document-writing"
        ? FileText
        : code === "write_file"
          ? PencilSimple
          : code === "web-research"
            ? Globe
            : code === "verification-before-completion"
              ? CheckCircle
              : Wrench;
  return <Icon size={21} aria-hidden="true" />;
}

function callPresentation(call: SDKToolCall): {
  title: string;
  context: string | null;
  command: string | null;
} {
  let argumentsValue: unknown = call.arguments;
  if (typeof argumentsValue === "string") {
    try {
      argumentsValue = JSON.parse(argumentsValue);
    } catch {
      return {
        title: capabilityName(call.name),
        context: "操作说明暂时无法读取。",
        command: null,
      };
    }
  }
  const values =
    argumentsValue !== null &&
    typeof argumentsValue === "object" &&
    !Array.isArray(argumentsValue)
      ? (argumentsValue as Record<string, unknown>)
      : {};
  const field = (key: string) =>
    typeof values[key] === "string" ? values[key] : null;
  const kind = field("kind");
  const queryTitles: Record<string, string> = {
    objective: "阅读任务要求",
    input: "阅读收到的消息",
    message: "阅读聊天消息",
    execution: "查看任务进展",
    space: "阅读工作说明",
    disposition: "检查消息处理情况",
    fact: "查阅已有资料",
    overview: "查看任务概况",
  };
  return {
    title:
      (call.name === "query_work" || call.name === "weave.query_work") && kind
        ? (queryTitles[kind] ?? "查看任务资料")
        : capabilityName(call.name),
    context:
      field("path") ??
      field("query") ??
      field("objective") ??
      field("question"),
    command: field("command"),
  };
}

function messageText(message: SDKMessage, inputs: ExecutionInput[]): string {
  const content = message.content;
  if (typeof content === "string") {
    if (message.role !== "user") return content;
    let value: unknown;
    try {
      value = JSON.parse(content);
    } catch {
      return content;
    }
    if (
      !isObject(value) ||
      value.input_id !== message.message_id ||
      typeof value.input_id !== "string" ||
      !value.input_id ||
      typeof value.kind !== "string" ||
      !["user", "observation", "delegate_result", "operation_result"].includes(
        value.kind,
      ) ||
      typeof value.source !== "string" ||
      (value.causation_id !== undefined &&
        value.causation_id !== null &&
        typeof value.causation_id !== "string") ||
      !Object.keys(value).every((key) =>
        ["input_id", "kind", "source", "content", "causation_id"].includes(key),
      )
    )
      return content;
    const weaveSources = [
      "weave.execution",
      "weave.message",
      "weave.control",
      "weave.retry",
      "weave.child",
      "weave.reassessment",
      "weave.handback",
      "weave.attention",
      "weave.report",
      "weave.validation",
    ];
    if (weaveSources.includes(value.source)) {
      if (typeof value.content === "string") return value.content;
      if (
        isInputReference(value.content) &&
        value.content.reference === `weave:input:${value.input_id}`
      ) {
        return (
          inputs.find((input) => input.input_id === value.input_id)?.content ??
          "原始要求未加载。"
        );
      }
    }
    if (
      value.source === "pleiad.operation" &&
      value.kind === "operation_result" &&
      /^operation:.+:result$/.test(value.input_id) &&
      isInputReference(value.content) &&
      value.content.reference === value.input_id
    )
      return "已收到运行结果";
    return content;
  }
  if (Array.isArray(content))
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part !== null && typeof part === "object" && !Array.isArray(part)) {
          if (typeof part.text === "string") return part.text;
          if (part.type === "image") return "[图片资料]";
          if (part.type === "audio") return "[音频资料]";
        }
        return JSON.stringify(part);
      })
      .filter(Boolean)
      .join("\n\n");
  return content === null ? "" : JSON.stringify(content, null, 2);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isInputReference(value: unknown): value is {
  reference: string;
  digest: string | null;
  summary: string | null;
} {
  return (
    isObject(value) &&
    typeof value.reference === "string" &&
    (value.digest === null ||
      (typeof value.digest === "string" &&
        /^[a-f0-9]{64}$/.test(value.digest))) &&
    (value.summary === null || typeof value.summary === "string") &&
    Object.keys(value).every((key) =>
      ["reference", "digest", "summary"].includes(key),
    )
  );
}

function ToolResultPanel({
  executionId,
  reference,
  generation,
  version,
}: {
  executionId: string;
  reference: ToolResultReference;
  generation: number;
  version: number;
}) {
  const [offsets, setOffsets] = useState([0]);
  const offset = offsets.at(-1)!;
  const result = useRecord<Awaited<
    ReturnType<typeof api.toolResultDisplay>
  > | null>(
    `tool-result/${executionId}/${reference.tool_result_id}/${offset}`,
    async (signal) => {
      const page = await api.toolResultDisplay(
        executionId,
        reference.tool_result_id,
        offset,
        signal,
      );
      if (page.id !== reference.tool_result_id || page.offset !== offset)
        throw new Error("结果与当前调用或文本分页不一致，请重新读取。");
      return page;
    },
    generation,
    version,
  );
  const page = result.value;
  const matched =
    page?.id === reference.tool_result_id && page.offset === offset;
  return (
    <div className="tool-result">
      <section
        className="raw-result"
        aria-label={reference.kind === "operation" ? "运行结果" : "操作结果"}
      >
        <LoadState
          loading={result.loading || (!result.error && !matched)}
          error={result.error}
        />
        {matched && page && (
          <>
            {page.format === "markdown" ? (
              <div className="markdown activity-result">
                <Markdown remarkPlugins={[remarkGfm]}>{page.text}</Markdown>
              </div>
            ) : (
              <pre className="activity-result">{page.text}</pre>
            )}
            {(offsets.length > 1 || page.next_offset !== null) && (
              <div className="row">
                <button
                  className="secondary"
                  disabled={result.loading || offsets.length === 1}
                  onClick={() => setOffsets((current) => current.slice(0, -1))}
                >
                  上一页内容
                </button>
                <button
                  className="secondary"
                  disabled={result.loading || page.next_offset === null}
                  onClick={() => {
                    if (page.next_offset !== null)
                      setOffsets((current) => [...current, page.next_offset!]);
                  }}
                >
                  下一页内容
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function ToolCallCard({
  executionId,
  step,
  call,
  references,
  operations,
  generation,
  version,
  showTitle,
}: {
  executionId: string;
  step: ModelStep;
  call: SDKToolCall;
  references: ToolResultReference[];
  operations: RunOperation[];
  showTitle: boolean;
  generation: number;
  version: number;
}) {
  const presentation = callPresentation(call);
  const messages = step.messages.filter(
    (message) => message.role === "tool" && message.tool_call_id === call.id,
  );
  const receipts = references.filter(
    (reference) => reference.kind === "tool_message",
  );
  const receipt = receipts[0];
  const receiptMatched =
    receipts.length === 1 &&
    messages.length === 1 &&
    receipt.tool_result_id === `${step.run_id}/${messages[0].message_id}`;
  const receiptInvalid =
    receipts.length > 0 || messages.length > 0
      ? !receiptMatched
      : step.state === "completed";
  return (
    <article
      className="tool-call"
      data-call-id={call.id}
      aria-label={presentation.title}
    >
      {showTitle && <h4>{presentation.title}</h4>}
      {receiptInvalid ? (
        <p role="alert" className="inline-error">
          这项操作的结果记录不完整，请刷新后查看。
        </p>
      ) : (
        !receiptMatched && (
          <p className="muted">
            {step.state === "aborted" ? "已中断" : "等待结果"}
          </p>
        )
      )}
      {presentation.context && (
        <p className="activity-context">{presentation.context}</p>
      )}
      {presentation.command && (
        <div className="activity-command">
          <h4>执行的命令</h4>
          <pre>{presentation.command}</pre>
        </div>
      )}
      {receiptMatched && (
        <ToolResultPanel
          executionId={executionId}
          reference={receipt}
          generation={generation}
          version={version}
        />
      )}
      {operations.map((operation) => {
        const results = references.filter(
          (reference) =>
            reference.kind === "operation" &&
            reference.operation_id === operation.operation_id,
        );
        const result = results[0];
        const resultMatched =
          results.length === 1 &&
          result.kind === "operation" &&
          result.state === operation.state &&
          result.tool_result_id ===
            `operation/${step.run_id}/${operation.operation_id}`;
        return (
          <section
            key={operation.operation_id}
            className="background-operation"
            data-operation-id={operation.operation_id}
            aria-label="后台执行"
          >
            <h4>运行状态</h4>
            <p className={`status status-${operation.state}`}>
              {
                {
                  prepared: "等待开始",
                  running: "进行中",
                  succeeded: "已完成",
                  failed: "执行失败",
                  cancel_requested: "正在取消",
                  cancelled: "已取消",
                  unknown: "结果待核实",
                }[operation.state]
              }
            </p>
            {results.length > 0 && !resultMatched ? (
              <p role="alert" className="inline-error">
                后台结果与所属调用不一致，请刷新后查看。
              </p>
            ) : resultMatched ? (
              <ToolResultPanel
                executionId={executionId}
                reference={result}
                generation={generation}
                version={version}
              />
            ) : (
              <p className="muted">
                {operation.state === "unknown"
                  ? "结果待核实"
                  : operation.state === "cancelled"
                    ? "无返回结果"
                    : "等待结果"}
              </p>
            )}
          </section>
        );
      })}
    </article>
  );
}

function stepAction(step: ModelStep): string {
  const actions = step.messages
    .filter((message) => message.role === "assistant")
    .flatMap((message) => message.tool_calls ?? [])
    .map((call) => callPresentation(call).title);
  const names = [...new Set(actions)];
  return names.length > 0 ? names.join("、") : "处理消息";
}

function stepDisplayState(
  step: ModelStep,
  execution: Execution,
  runState: string,
): string {
  if (step.state !== "active") return step.state;
  if (
    ["succeeded", "failed", "cancelled"].includes(runState) ||
    ["succeeded", "failed", "cancelled"].includes(execution.status)
  )
    return "incomplete";
  if (runState === "paused" || execution.status === "paused") return "paused";
  if (execution.pause_requested) return "pausing";
  return execution.status === "running" ? "active" : execution.status;
}

function stepStateLabel(state: string): string {
  return (
    (
      {
        active: "进行中",
        completed: "已完成",
        aborted: "已中断",
        paused: "已暂停",
        pausing: "正在暂停",
        incomplete: "未完成",
        queued: "等待开始",
        waiting_input: "等待补充",
        waiting_approval: "等待批准",
        waiting_children: "等待子任务",
        blocked: "暂不可执行",
        cancelling: "正在取消",
      } as Record<string, string>
    )[state] ?? "未完成"
  );
}

function TimelineStep({
  executionId,
  execution,
  step,
  detail,
  generation,
  version,
  initialOpen,
  inputs,
}: {
  executionId: string;
  execution: Execution;
  step: ModelStep;
  detail: RunDetail;
  generation: number;
  version: number;
  initialOpen: boolean;
  inputs: ExecutionInput[];
}) {
  const [opened, setOpened] = useState(initialOpen);
  const displayState = stepDisplayState(step, execution, detail.run.state);
  const calls = step.messages
    .filter((message) => message.role === "assistant")
    .flatMap((message) => message.tool_calls ?? []);
  const failedOperation = detail.operations.some(
    (operation) =>
      operation.step_id === step.step_id && operation.state === "failed",
  );
  return (
    <details
      className={`step timeline-step state-${step.state}${failedOperation ? " has-failed-operation" : ""}`}
      data-step-id={step.step_id}
      data-display-state={displayState}
      open={opened}
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary className="timeline-heading">
        <span className="timeline-marker" aria-hidden="true">
          {failedOperation || step.state === "aborted" ? (
            <X size={14} weight="bold" />
          ) : step.state === "completed" ? (
            <Check size={14} weight="bold" />
          ) : displayState === "paused" || displayState === "pausing" ? (
            <Pause size={14} weight="bold" />
          ) : (
            <Circle size={9} weight="fill" />
          )}
        </span>
        <span className="timeline-title">{stepAction(step)}</span>
        <span className="step-state">
          步骤 {step.run_sequence} · {stepStateLabel(displayState)}
        </span>
      </summary>
      {opened && (
        <div className="timeline-content step-body">
          {step.messages
            .filter(
              (message) =>
                message.role !== "tool" && messageText(message, inputs).trim(),
            )
            .map((message) => (
              <section key={message.message_id} className="step-message">
                <h4>{message.role === "user" ? "收到的内容" : "助手回复"}</h4>
                <div className="markdown">
                  <Markdown remarkPlugins={[remarkGfm]}>
                    {messageText(message, inputs)}
                  </Markdown>
                </div>
              </section>
            ))}
          {calls.map((call) => (
            <ToolCallCard
              key={call.id}
              executionId={executionId}
              step={step}
              call={call}
              showTitle={calls.length > 1}
              references={detail.tool_results.filter(
                (reference) =>
                  reference.step_id === step.step_id &&
                  reference.call_id === call.id,
              )}
              operations={detail.operations.filter(
                (operation) =>
                  operation.step_id === step.step_id &&
                  operation.call_id === call.id,
              )}
              generation={generation}
              version={version}
            />
          ))}
        </div>
      )}
    </details>
  );
}

function RunSteps({
  executionId,
  execution,
  runId,
  detail,
  inputs,
  generation,
  version,
}: {
  executionId: string;
  execution: Execution;
  runId: string;
  detail: RunDetail;
  inputs: ExecutionInput[];
  generation: number;
  version: number;
}) {
  if (!Array.isArray(detail.tool_results) || !Array.isArray(detail.operations))
    return (
      <p className="inline-error" role="alert">
        执行记录缺少完整的工具关联信息，请刷新后查看。
      </p>
    );
  const steps = detail.steps.items.filter((step) => step.run_id === runId);
  const calls = steps.flatMap((step) =>
    step.messages.flatMap((message) =>
      message.role === "assistant"
        ? (message.tool_calls ?? []).map((call) => ({
            stepId: step.step_id,
            callId: call.id,
          }))
        : [],
    ),
  );
  const unmatched = [
    ...detail.tool_results.filter(
      (reference) =>
        !calls.some(
          (call) =>
            call.stepId === reference.step_id &&
            call.callId === reference.call_id,
        ) ||
        (reference.kind === "operation" &&
          !detail.operations.some(
            (operation) =>
              operation.operation_id === reference.operation_id &&
              operation.step_id === reference.step_id &&
              operation.call_id === reference.call_id &&
              operation.state === reference.state,
          )),
    ),
    ...detail.operations.filter(
      (operation) =>
        !calls.some(
          (call) =>
            call.stepId === operation.step_id &&
            call.callId === operation.call_id,
        ),
    ),
  ];
  return (
    <>
      {(steps.length !== detail.steps.items.length || unmatched.length > 0) && (
        <p role="alert" className="inline-error">
          部分执行记录的所属步骤无法核验，请刷新后查看。
        </p>
      )}
      <div className="execution-timeline">
        {steps.map((step, index) => (
          <TimelineStep
            key={step.step_id}
            executionId={executionId}
            execution={execution}
            step={step}
            detail={detail}
            generation={generation}
            version={version}
            inputs={inputs}
            initialOpen={index === 0}
          />
        ))}
      </div>
    </>
  );
}

function ProcessWorkspace({
  controls,
  execution,
  graphRunId,
  runId,
  nodeId: selectedNodeId,
  nodeAttemptId,
  generation,
  version,
  onNavigate,
  onReference,
}: {
  controls: WorkspaceControls;
  execution: ExecutionDetail;
  graphRunId?: string;
  runId?: string;
  nodeId?: string;
  nodeAttemptId?: string;
  generation: number;
  version: number;
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
  onReference: (ref: ContextReference) => void;
}) {
  const graphPageKey = `execution/${execution.execution_id}/graphs`;
  const runPageKey = `execution/${execution.execution_id}/runs`;
  const graphCursor = controls.pages[graphPageKey] ?? 0;
  const runCursor = controls.pages[runPageKey] ?? 0;
  const setGraphCursor = (value: number) =>
    controls.setPage(graphPageKey, value);
  const setRunCursor = (value: number) => controls.setPage(runPageKey, value);
  const graphs = useRecord(
    `graphs/${execution.execution_id}/${graphCursor}`,
    (signal) => api.graphRuns(execution.execution_id, graphCursor, signal),
    generation,
    version,
  );
  const runs = useRecord(
    `runs/${execution.execution_id}/${runCursor}`,
    (signal) =>
      api.runs(execution.execution_id, runCursor || undefined, signal),
    generation,
    version,
  );
  const inspector = useRef<HTMLElement>(null);
  const focusInspector = useRef(false);
  useEffect(() => {
    if (!focusInspector.current || !inspector.current) return;
    inspector.current.focus({ preventScroll: true });
    focusInspector.current = false;
  });
  const selectedGraph =
    graphRunId ??
    graphs.value?.items
      .filter(
        (item) => item.root_run_id === (runId ?? runs.value?.latest_run_id),
      )
      .at(-1)?.graph_run_id;
  const nodePageKey = `graph/${selectedGraph}/attempts`;
  const nodeCursor = controls.pages[nodePageKey] ?? 0;
  const setNodeCursor = (value: number) => controls.setPage(nodePageKey, value);
  const graph = useRecord<GraphDetail | null>(
    `graph/${execution.execution_id}/${selectedGraph}/${nodeCursor}`,
    (signal) =>
      selectedGraph
        ? api.graphRun(
            execution.execution_id,
            selectedGraph,
            nodeCursor,
            signal,
          )
        : Promise.resolve(null),
    generation,
    version,
  );
  const selectedRun =
    runId ??
    (graphRunId
      ? graph.value?.graph_run.root_run_id
      : runs.value?.latest_run_id);
  const selectedAttempt = useRecord(
    `node-attempt/${execution.execution_id}/${nodeAttemptId}`,
    (signal) =>
      nodeAttemptId
        ? api.nodeAttempt(execution.execution_id, nodeAttemptId, signal)
        : Promise.resolve(null),
    generation,
    version,
  );
  const ownedAttempt =
    selectedAttempt.value?.node_attempt_id === nodeAttemptId &&
    selectedAttempt.value?.graph_run_id === selectedGraph
      ? selectedAttempt.value
      : null;
  const nodeId = selectedNodeId ?? ownedAttempt?.node_id ?? "";
  const select = (selection: {
    runId?: string;
    graphRunId?: string;
    nodeId?: string;
    nodeAttemptId?: string;
  }) =>
    onNavigate({
      view: "execution",
      execution_id: execution.execution_id,
      tab: "process",
      run_id: selection.runId,
      graph_run_id: selection.graphRunId,
      node_id: selection.nodeId,
      node_attempt_id: selection.nodeAttemptId,
    });
  useEffect(() => {
    if (
      graph.loading ||
      runs.loading ||
      (runId === (selectedRun ?? undefined) && graphRunId === selectedGraph) ||
      (!selectedGraph && !selectedRun)
    )
      return;
    onNavigate({
      view: "execution",
      execution_id: execution.execution_id,
      tab: "process",
      run_id: selectedRun ?? undefined,
      graph_run_id: selectedGraph,
      node_id: selectedNodeId,
      node_attempt_id: nodeAttemptId,
    });
  }, [
    execution.execution_id,
    graph.loading,
    runs.loading,
    runId,
    graphRunId,
    selectedGraph,
    selectedRun,
    selectedNodeId,
    nodeAttemptId,
    onNavigate,
  ]);
  const stepPageKey = `run/${selectedRun}/steps`;
  const stepCursor = controls.pages[stepPageKey] ?? 0;
  const setStepCursor = (value: number) => controls.setPage(stepPageKey, value);
  const windowOwner = `${execution.execution_id}/${selectedRun}/${generation}`;
  const latestWindow = useRef<{ owner: string; before?: number }>({
    owner: windowOwner,
  });
  if (latestWindow.current.owner !== windowOwner)
    latestWindow.current = { owner: windowOwner };
  const [runRefresh, setRunRefresh] = useState(0);
  const run = useRecord(
    `run/${execution.execution_id}/${selectedRun}/${stepCursor}`,
    async (signal) => {
      if (!selectedRun) return null;
      const window = latestWindow.current;
      const result = await api.run(
        execution.execution_id,
        selectedRun,
        stepCursor || window.before,
        signal,
      );
      if (
        !signal.aborted &&
        !stepCursor &&
        window.before === undefined &&
        result.steps.latest_sequence !== null
      )
        window.before = result.steps.latest_sequence + 1;
      return result;
    },
    generation,
    version + runRefresh,
  );
  const showLatestSteps = () => {
    latestWindow.current.before = undefined;
    setStepCursor(0);
    setRunRefresh((value) => value + 1);
  };
  const newerStepsAvailable =
    run.value !== null &&
    run.value.steps.latest_sequence !== null &&
    run.value.steps.latest_sequence >
      (run.value.steps.items[0]?.run_sequence ?? 0);
  const nextRunPage = runs.value?.next_before_sequence ?? null;
  const nextStepPage = run.value?.steps.next_before_sequence ?? null;
  const latestRunId = runs.value?.latest_run_id;
  const nodeAttempts = [
    ...new Map(
      [
        ...(graph.value?.node_attempts ?? []),
        ...(ownedAttempt ? [ownedAttempt] : []),
      ]
        .filter((value) => value.node_id === nodeId)
        .map((value) => [value.node_attempt_id, value]),
    ).values(),
  ].sort((one, two) => one.attempt - two.attempt);
  const attempt = nodeAttemptId ? ownedAttempt : nodeAttempts.at(-1);
  const canSelectRun =
    (runs.value?.total ?? 0) > 1 ||
    new Set([
      ...(runs.value?.items.flatMap((run) =>
        run.run_id ? [run.run_id] : [],
      ) ?? []),
      ...(selectedRun ? [selectedRun] : []),
    ]).size > 1;
  const selectedNode = graph.value?.graph.nodes.find(
    (node) => node.node_id === nodeId,
  );
  return (
    <>
      {Boolean(selectedGraph || graphs.value?.items.length) && (
        <div className="row process-toolbar">
          <label>
            任务流程
            <select
              aria-label="选择图运行"
              value={selectedGraph ?? ""}
              onChange={(event) =>
                select({
                  graphRunId: event.target.value,
                  runId: graphs.value?.items.find(
                    (value) => value.graph_run_id === event.target.value,
                  )?.root_run_id,
                })
              }
            >
              <option value="">选择任务流程</option>
              {selectedGraph &&
                !graphs.value?.items.some(
                  (item) => item.graph_run_id === selectedGraph,
                ) && <option value={selectedGraph}>当前查看的任务流程</option>}
              {graphs.value?.items.map((item) => (
                <option key={item.graph_run_id} value={item.graph_run_id}>
                  {new Date(item.created_at).toLocaleString("zh-CN")} ·{" "}
                  {statusLabel(item.status)}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {selectedGraph &&
        graphs.value?.items.at(-1)?.graph_run_id !== selectedGraph &&
        graphs.value?.items.at(-1) && (
          <button
            className="text-button"
            onClick={() => {
              const latest = graphs.value!.items.at(-1)!;
              select({
                graphRunId: latest.graph_run_id,
                runId: latest.root_run_id,
              });
            }}
          >
            有新的运行记录
          </button>
        )}
      <PageControls
        cursor={graphCursor}
        page={graphs.value}
        loading={graphs.loading}
        onCursor={setGraphCursor}
      />
      <LoadState
        loading={graphs.loading || graph.loading}
        error={graphs.error || graph.error}
      />
      {graph.value ? (
        <>
          <GraphCanvas
            key={graph.value.graph_run.graph_run_id}
            graph={graph.value.graph}
            run={graph.value.graph_run}
            attempts={graph.value.node_attempts}
            selectedNodeId={nodeId}
            onNode={(id) => {
              focusInspector.current = true;
              const selected = graph.value?.node_attempts
                .filter((item) => item.node_id === id)
                .at(-1);
              select({
                runId: selectedRun ?? undefined,
                graphRunId: selectedGraph,
                nodeId: id,
                nodeAttemptId: selected?.node_attempt_id,
              });
            }}
          />
          <PageControls
            cursor={nodeCursor}
            pageSize={100}
            page={{
              total: graph.value.total,
              next_cursor: graph.value.next_cursor,
            }}
            loading={graph.loading}
            onCursor={setNodeCursor}
          />
          <LoadState
            loading={selectedAttempt.loading && Boolean(nodeAttemptId)}
            error={selectedAttempt.error}
          />
          {selectedAttempt.value && !ownedAttempt && (
            <p className="inline-error" role="alert">
              所选节点尝试不属于当前图运行。
            </p>
          )}
          {nodeId && (
            <section
              ref={inspector}
              className="node-inspector"
              tabIndex={-1}
              aria-label="节点详情"
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  select({
                    runId: selectedRun ?? undefined,
                    graphRunId: selectedGraph,
                  });
                }
              }}
            >
              <div className="row">
                <h2>{selectedNode?.objective ?? "任务节点"}</h2>
                <button
                  aria-label="关闭节点详情"
                  onClick={() => {
                    select({
                      runId: selectedRun ?? undefined,
                      graphRunId: selectedGraph,
                    });
                  }}
                >
                  <X />
                </button>
              </div>
              {nodeAttempts.length ? (
                <>
                  <label>
                    节点尝试
                    <select
                      aria-label="选择节点尝试"
                      value={attempt?.node_attempt_id ?? ""}
                      onChange={(event) =>
                        select({
                          runId: selectedRun ?? undefined,
                          graphRunId: selectedGraph,
                          nodeId,
                          nodeAttemptId: event.target.value,
                        })
                      }
                    >
                      {nodeAttempts.map((item) => (
                        <option
                          key={item.node_attempt_id}
                          value={item.node_attempt_id}
                        >
                          尝试 {item.attempt} · {statusLabel(item.status)}
                        </option>
                      ))}
                    </select>
                  </label>
                  {attempt && (
                    <>
                      <Status value={attempt.status} />
                      {selectedNode?.prompt && (
                        <div className="markdown">
                          <Markdown remarkPlugins={[remarkGfm]}>
                            {selectedNode.prompt}
                          </Markdown>
                        </div>
                      )}
                      <p>
                        开始于{" "}
                        {new Date(attempt.created_at).toLocaleString("zh-CN")}
                      </p>
                      {attempt.finished_at && (
                        <p>
                          结束于{" "}
                          {new Date(attempt.finished_at).toLocaleString(
                            "zh-CN",
                          )}
                        </p>
                      )}
                      {attempt.error && (
                        <p className="inline-error">{attempt.error.message}</p>
                      )}
                      <button
                        className="text-button"
                        onClick={() =>
                          onReference({
                            kind: "graph_node",
                            id: attempt.node_attempt_id,
                            execution_id: execution.execution_id,
                            graph_run_id: attempt.graph_run_id,
                            node_id: attempt.node_id,
                            node_attempt_id: attempt.node_attempt_id,
                          })
                        }
                      >
                        在主聊天询问此节点
                      </button>
                      {attempt.artifact_ids.map((id) => (
                        <button
                          className="text-button"
                          key={id}
                          data-content-id={`artifact/${id}`}
                          onClick={(event) =>
                            onNavigate(
                              { view: "artifact", artifact_id: id },
                              event.currentTarget,
                            )
                          }
                        >
                          查看节点产物
                          {execution.artifacts.find(
                            (artifact) => artifact.artifact_id === id,
                          )?.name
                            ? ` · ${execution.artifacts.find((artifact) => artifact.artifact_id === id)!.name}`
                            : ""}
                        </button>
                      ))}
                    </>
                  )}
                </>
              ) : (
                <p className="muted">此节点尚未形成独立尝试记录。</p>
              )}
            </section>
          )}
        </>
      ) : (
        !graphs.loading && !selectedRun && <Empty>暂无执行记录</Empty>
      )}
      <section className="run-history">
        <div className="row">
          <h2>执行过程</h2>
          {canSelectRun && (
            <select
              aria-label="选择执行调用"
              value={selectedRun ?? ""}
              onChange={(event) => {
                select({
                  runId: event.target.value,
                  graphRunId: graphs.value?.items
                    .filter((value) => value.root_run_id === event.target.value)
                    .at(-1)?.graph_run_id,
                });
              }}
            >
              <option value="">选择处理记录</option>
              {selectedRun &&
                !runs.value?.items.some(
                  (item) => item.run_id === selectedRun,
                ) && <option value={selectedRun}>当前处理记录</option>}
              {(runs.value?.items ?? [])
                .filter((value) => value.run_id !== null)
                .map((value) => (
                  <option key={value.invocation_id} value={value.run_id!}>
                    第 {value.attempt} 次处理 · {statusLabel(value.state)}
                  </option>
                ))}
            </select>
          )}
        </div>
        {(runCursor > 0 || nextRunPage !== null) && (
          <div className="row page-controls">
            {runCursor > 0 && (
              <button disabled={runs.loading} onClick={() => setRunCursor(0)}>
                最新处理记录
              </button>
            )}
            {nextRunPage !== null && (
              <button
                disabled={runs.loading}
                onClick={() => setRunCursor(nextRunPage)}
              >
                更早处理记录
              </button>
            )}
          </div>
        )}
        {latestRunId && selectedRun !== latestRunId && (
          <button
            className="text-button"
            onClick={() => {
              setRunCursor(0);
              select({ runId: latestRunId });
            }}
          >
            查看最新处理记录
          </button>
        )}
        {runs.value &&
          runs.value.total > 0 &&
          runs.value.latest_run_id === null && (
            <p className="muted">最新处理尚未开始</p>
          )}
        {(stepCursor > 0 || newerStepsAvailable) && (
          <button
            className="text-button"
            disabled={run.loading}
            onClick={showLatestSteps}
          >
            查看最新进展
          </button>
        )}
        <LoadState
          loading={run.loading || runs.loading}
          error={run.error || runs.error}
        />
        {run.value && selectedRun && (
          <RunSteps
            executionId={execution.execution_id}
            execution={execution}
            runId={selectedRun}
            detail={run.value}
            inputs={execution.inputs}
            generation={generation}
            version={version}
          />
        )}
        {nextStepPage !== null && (
          <button
            className="text-button"
            disabled={run.loading}
            onClick={() => setStepCursor(nextStepPage)}
          >
            查看更早步骤
          </button>
        )}
      </section>
    </>
  );
}

function ResourceFields({
  values,
}: {
  values: ResourceDetail["configuration"];
}) {
  const labels: Record<string, string> = {
    role: "用途",
    protocol: "接口协议",
    provider: "提供方",
    model: "模型",
    base_url: "服务地址",
    timeout_seconds: "超时（秒）",
    max_retries: "最大重试次数",
    provider_options: "模型参数",
    headers: "请求头",
    api_key: "API 密钥",
    policy: "调用策略",
    required_permissions: "所需权限",
    approval_required: "需要确认",
    supports_background: "支持后台执行",
    binding: "运行绑定",
    workspace: "工作目录",
    sandbox_policy: "沙箱限制",
    backend: "运行后端",
    name: "名称",
    code: "标识",
    description: "描述",
    version: "版本",
    license: "许可",
    compatibility: "兼容要求",
    metadata: "附加信息",
    allowed_tools: "可用工具",
    digest: "内容摘要",
  };
  return (
    <dl className="resource-fields">
      {Object.entries(values).map(([key, value]) => (
        <div key={key}>
          <dt>{labels[key] ?? key}</dt>
          <dd>
            {value !== null && typeof value === "object" ? (
              <pre>{JSON.stringify(value, null, 2)}</pre>
            ) : value === null ? (
              "未设置"
            ) : typeof value === "boolean" ? (
              value ? (
                "是"
              ) : (
                "否"
              )
            ) : (
              String(value)
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function ResourceInformation({ resource }: { resource: ResourceDetail }) {
  return (
    <div className="resource-information">
      <div className="resource-detail-toolbar">
        <span
          className={`status ${resource.available ? "status-active" : "status-blocked"}`}
        >
          {resource.available ? "可使用" : "未启用"}
        </span>
        <CopyButton
          text={JSON.stringify(resource, null, 2)}
          label="复制原始信息"
        />
      </div>
      {resource.disabled_reason && (
        <p className="inline-error">{resource.disabled_reason}</p>
      )}
      {resource.description?.trim() && (
        <div className="markdown">
          <Markdown remarkPlugins={[remarkGfm]}>
            {resource.description}
          </Markdown>
        </div>
      )}
      <section>
        <h3>基本信息</h3>
        <ResourceFields
          values={{ code: resource.code, ...resource.metadata }}
        />
      </section>
      {Object.keys(resource.configuration).length > 0 && (
        <section>
          <h3>配置</h3>
          <ResourceFields values={resource.configuration} />
        </section>
      )}
      {resource.configuration_location && (
        <p className="resource-location">
          配置文件 <code>{resource.configuration_location}</code>
        </p>
      )}
      {resource.input_schema !== null && (
        <details className="resource-schema">
          <summary>输入参数</summary>
          <pre>{JSON.stringify(resource.input_schema, null, 2)}</pre>
        </details>
      )}
      {resource.output_schema !== null && (
        <details className="resource-schema">
          <summary>返回内容</summary>
          <pre>{JSON.stringify(resource.output_schema, null, 2)}</pre>
        </details>
      )}
      {resource.content !== null && (
        <section>
          <h3>技能原文</h3>
          <div className="markdown resource-content">
            <Markdown remarkPlugins={[remarkGfm]}>{resource.content}</Markdown>
          </div>
        </section>
      )}
      {resource.files.length > 0 && (
        <section>
          <h3>资源文件</h3>
          <ul className="resource-files">
            {resource.files.map((file) => (
              <li key={file}>
                <code>{file}</code>
              </li>
            ))}
          </ul>
        </section>
      )}
      <details className="resource-schema">
        <summary>原始数据</summary>
        <pre>{JSON.stringify(resource, null, 2)}</pre>
      </details>
    </div>
  );
}

export function ResourcesWorkspace({
  active,
  controls,
  generation,
  onBack,
}: {
  active: boolean;
  controls: WorkspaceControls;
  generation: number;
  onBack: () => void;
}) {
  const kind = controls.queries["resources/kind"] ?? "tools";
  const query = controls.queries["resources/query"] ?? "";
  const cursor = controls.pages.resources ?? 0;
  const setCursor = (value: number) => controls.setPage("resources", value);
  const setKind = (value: string) => {
    controls.setQuery("resources/kind", value);
    setCursor(0);
  };
  const setQuery = (value: string) => {
    controls.setQuery("resources/query", value);
    setCursor(0);
  };
  const [selectedCode, setSelectedCode] = useState<string | null>(null),
    [version, setVersion] = useState(0);
  useEffect(() => {
    if (!active) setSelectedCode(null);
  }, [active]);
  const selection = useRecord(
    `resource/${kind}/${selectedCode}`,
    (signal) =>
      selectedCode
        ? api.resource(kind, selectedCode, signal)
        : Promise.resolve(null),
    generation,
    version,
  );
  const selected = selection.value;
  const list = useRecord(
    `resources/${kind}/${query}/${cursor}`,
    (signal) => api.resources(kind, query, cursor, signal),
    generation,
    version,
  );
  return (
    <>
      <button className="text-button" onClick={onBack}>
        <ArrowLeft />
        返回来源
      </button>
      <div className="eyebrow">工作区 / 资源</div>
      <header className="workspace-heading resource-heading">
        <div>
          <h1>资源目录</h1>
        </div>
        <input
          aria-label="搜索资源"
          placeholder="搜索资源"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setQuery("");
            } else if (
              event.key === "Enter" &&
              query.trim() &&
              !list.loading &&
              !list.error &&
              list.value?.total === 1 &&
              list.value.items.length === 1
            ) {
              event.preventDefault();
              setSelectedCode(list.value.items[0].code);
            }
          }}
        />
      </header>
      <div
        className="tabs"
        role="tablist"
        aria-label="资源类型"
        onKeyDown={navigateTabs}
      >
        {[
          ["models", "模型"],
          ["tools", "工具"],
          ["skills", "技能"],
          ["environments", "环境"],
        ].map(([value, label]) => (
          <button
            className={kind === value ? "selected" : ""}
            role="tab"
            aria-selected={kind === value}
            tabIndex={kind === value ? 0 : -1}
            key={value}
            onClick={() => {
              setKind(value);
              setSelectedCode(null);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <LoadState loading={list.loading} error={list.error} />
      <button
        className="text-button resource-refresh"
        disabled={list.loading}
        onClick={() => setVersion((value) => value + 1)}
      >
        <ArrowsClockwise size={18} aria-hidden="true" />
        刷新目录
      </button>
      <div className="resource-list">
        {list.value?.items.map((resource) => {
          const Icon =
            kind === "models"
              ? Robot
              : kind === "tools"
                ? Wrench
                : kind === "skills"
                  ? BookOpen
                  : HardDrives;
          const contents = (
            <>
              <Icon size={27} className="resource-icon" aria-hidden="true" />
              <div className="resource-summary">
                <div className="row">
                  <strong>{capabilityName(resource.name)}</strong>
                  <span
                    className={`status ${resource.available ? "status-active" : "status-blocked"}`}
                  >
                    {kind === "models"
                      ? resource.available
                        ? "已配置"
                        : "未配置"
                      : resource.available
                        ? "可使用"
                        : "未启用"}
                  </span>
                </div>
                {kind === "models" && (
                  <span className="resource-kind">
                    {modelCategory(resource.code)}
                  </span>
                )}
                {resource.disabled_reason && (
                  <p className="inline-error">{resource.disabled_reason}</p>
                )}
              </div>
            </>
          );
          return (
            <button
              className="resource-card resource-row"
              key={resource.code}
              onClick={() => setSelectedCode(resource.code)}
            >
              {contents}
            </button>
          );
        })}
      </div>
      <PageControls
        cursor={cursor}
        page={list.value}
        loading={list.loading}
        onCursor={setCursor}
      />
      {list.value?.total === 0 && <Empty>当前目录中没有匹配的资源。</Empty>}
      {active && selectedCode && (
        <Modal
          title={selected ? capabilityName(selected.name) : "资源详情"}
          size="resource"
          onClose={() => setSelectedCode(null)}
        >
          <LoadState loading={selection.loading} error={selection.error} />
          {selected && <ResourceInformation resource={selected} />}
        </Modal>
      )}
    </>
  );
}
export function SettingsWorkspace({ generation }: { generation: number }) {
  const settings = useRecord(
    "settings",
    (signal) => api.settings(signal),
    generation,
    0,
  );
  const [version, setVersion] = useState(0);
  const connection = useRecord(
    "connection",
    (signal) => api.connection(signal),
    generation,
    version,
  );
  const [density, setDensity] = useState(
    () => localStorage.getItem("weave-density") ?? "comfortable",
  );
  useEffect(() => {
    localStorage.setItem("weave-density", density);
    document.documentElement.dataset.density = density;
  }, [density]);
  return (
    <>
      <div className="eyebrow">工作区 / 设置</div>
      <h1>设置</h1>
      <section className="settings-form">
        <h2>显示偏好</h2>
        <label>
          信息密度
          <select
            value={density}
            onChange={(event) => setDensity(event.target.value)}
          >
            <option value="comfortable">舒适</option>
            <option value="compact">紧凑</option>
          </select>
        </label>
        <h2>连接检查</h2>
        <LoadState loading={connection.loading} error={connection.error} />
        {connection.value && (
          <div role="status">
            <p>HTTP 进程 · {connection.value.healthy ? "运行中" : "不可用"}</p>
            <p>
              工作区接收能力 · {connection.value.ready ? "已就绪" : "尚未就绪"}
              （HTTP {connection.value.readiness_status}）
            </p>
            <p className="muted">
              检查时间{" "}
              {new Date(connection.value.checked_at).toLocaleString("zh-CN")}
            </p>
          </div>
        )}
        <button
          className="secondary"
          disabled={connection.loading}
          onClick={() => setVersion((value) => value + 1)}
        >
          重新检查连接
        </button>
        <h2>连接与宿主配置</h2>
        <LoadState loading={settings.loading} error={settings.error} />
        {settings.value && (
          <dl>
            {Object.entries(settings.value).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>
                  {typeof value === "string" ? value : JSON.stringify(value)}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </section>
    </>
  );
}

export function ContentWorkspace({
  controls,
  kind,
  identity,
  generation,
  version,
  onBack,
  onNavigate,
  onReference,
}: {
  controls: WorkspaceControls;
  kind: "artifact" | "attachment";
  identity: string;
  generation: number;
  version: number;
  onBack: () => void;
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
  onReference: (reference: ContextReference) => void;
}) {
  const metadata = useRecord<Artifact | Attachment>(
    `${kind}/${identity}`,
    (signal) =>
      kind === "artifact"
        ? api.artifact(identity, signal)
        : api.attachment(identity, signal),
    generation,
    version,
  );
  const item = metadata.value;
  const contentUrl =
    kind === "artifact"
      ? api.artifactUrl(identity)
      : api.attachmentUrl(identity, false);
  const pageKey = `content/${kind}/${identity}`;
  const offset = controls.pages[pageKey] ?? 0;
  const text = useRecord<TextPage | null>(
    `content/${kind}/${identity}/${item?.digest}/${offset}`,
    (signal) => {
      if (
        !item ||
        ![
          "text/plain",
          "text/markdown",
          "text/csv",
          "application/json",
        ].includes(item.mime)
      )
        return Promise.resolve(null);
      return api.contentText(kind, identity, offset, signal);
    },
    generation,
    version,
  );
  const [imageError, setImageError] = useState(false);
  useEffect(() => setImageError(false), [identity, generation, version]);
  return (
    <>
      <button className="text-button" onClick={onBack}>
        <ArrowLeft />
        返回来源
      </button>
      <div className="eyebrow">资料与产物</div>
      <LoadState loading={metadata.loading} error={metadata.error} />
      {item && (
        <>
          <header className="workspace-heading">
            <div>
              <h1>{item.name}</h1>
              <p className="muted">
                {item.mime} · {item.size.toLocaleString()} 字节
              </p>
            </div>
            <div className="row">
              <a
                className="secondary"
                href={
                  kind === "artifact"
                    ? api.artifactUrl(identity, true)
                    : api.attachmentUrl(identity)
                }
              >
                下载
              </a>
              {kind === "artifact" && (
                <button
                  className="secondary"
                  onClick={() =>
                    onReference({ kind: "artifact", id: identity })
                  }
                >
                  引用产物
                </button>
              )}
            </div>
          </header>
          {"owner_execution_id" in item && (
            <button
              className="text-button"
              onClick={() =>
                onNavigate({
                  view: "execution",
                  execution_id: item.owner_execution_id,
                  tab: item.graph_run_id ? "process" : "overview",
                  graph_run_id: item.graph_run_id ?? undefined,
                  node_attempt_id: item.node_attempt_id ?? undefined,
                })
              }
            >
              查看来源工作
            </button>
          )}
          {"deletion_state" in item && item.deletion_state !== "active" ? (
            <p className="inline-error" role="alert">
              内容当前不可用（{item.deletion_state}）。
            </p>
          ) : item.mime.startsWith("image/") ? (
            <>
              {imageError ? (
                <p className="inline-error" role="alert">
                  图片内容无法读取，请检查产物是否仍完整。
                </p>
              ) : (
                <img
                  className="content-image"
                  src={contentUrl}
                  alt={item.name}
                  onError={() => setImageError(true)}
                />
              )}
            </>
          ) : item.mime === "application/pdf" ? (
            <iframe
              className="content-pdf"
              title={item.name}
              src={contentUrl}
              sandbox="allow-same-origin"
            />
          ) : text.value ? (
            <>
              <LoadState loading={text.loading} error={text.error} />
              {item.mime === "text/markdown" ? (
                <div className="markdown result">
                  <Markdown remarkPlugins={[remarkGfm]}>
                    {text.value.text}
                  </Markdown>
                </div>
              ) : (
                <pre>{text.value.text}</pre>
              )}
              <div className="row">
                <button
                  className="secondary"
                  disabled={text.loading || offset === 0}
                  onClick={() =>
                    controls.setPage(
                      pageKey,
                      controls.pages[`${pageKey}/previous/${offset}`] ?? 0,
                    )
                  }
                >
                  上一段资料
                </button>
                <button
                  className="secondary"
                  disabled={text.loading || text.value.next_offset === null}
                  onClick={() => {
                    const next = text.value?.next_offset;
                    if (next === null || next === undefined) return;
                    controls.setPage(`${pageKey}/previous/${next}`, offset);
                    controls.setPage(pageKey, next);
                  }}
                >
                  下一段资料
                </button>
              </div>
            </>
          ) : (
            <>
              <LoadState loading={text.loading} error={text.error} />
              {!text.loading && !text.error && <Empty>暂不支持预览</Empty>}
            </>
          )}
        </>
      )}
    </>
  );
}
function SpaceArtifacts({
  spaceId,
  generation,
  version,
  onNavigate,
  onReference,
}: {
  spaceId: string;
  generation: number;
  version: number;
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
  onReference: (reference: ContextReference) => void;
}) {
  const detail = useRecord<{ artifacts: Artifact[] }>(
    `space-artifacts/${spaceId}`,
    (signal) =>
      api
        .space(spaceId, signal)
        .then((value) => ({ artifacts: value.artifacts })),
    generation,
    version,
  );
  return (
    <>
      <LoadState loading={detail.loading} error={detail.error} />
      {detail.value && (
        <ArtifactList
          artifacts={detail.value.artifacts}
          onNavigate={onNavigate}
          onReference={onReference}
        />
      )}
    </>
  );
}

function PageControls({
  cursor,
  page,
  loading,
  onCursor,
  pageSize = 50,
}: {
  cursor: number;
  page: { next_cursor: number | null; total: number } | null;
  loading: boolean;
  onCursor: (cursor: number) => void;
  pageSize?: number;
}) {
  if (cursor === 0 && (page === null || page.next_cursor === null)) return null;
  return (
    <div className="pagination" aria-label="分页">
      <button
        type="button"
        className="secondary"
        disabled={loading || cursor === 0}
        onClick={() => onCursor(Math.max(0, cursor - pageSize))}
      >
        上一页
      </button>
      <span className="muted">
        {page
          ? `${cursor + 1}–${Math.min(cursor + pageSize, page.total)} / ${page.total}`
          : "读取中"}
      </span>
      <button
        type="button"
        className="secondary"
        disabled={loading || page === null || page.next_cursor === null}
        onClick={() => {
          if (page?.next_cursor !== null && page?.next_cursor !== undefined)
            onCursor(page.next_cursor);
        }}
      >
        下一页
      </button>
    </div>
  );
}

function navigateTabs(event: KeyboardEvent<HTMLDivElement>) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  const tabs = Array.from(
    event.currentTarget.querySelectorAll<HTMLButtonElement>(
      'button[role="tab"]',
    ),
  );
  const index = tabs.findIndex((tab) => tab === event.target);
  if (index < 0 || tabs.length === 0) return;
  const destination =
    event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) %
          tabs.length;
  event.preventDefault();
  tabs[destination].focus();
  tabs[destination].click();
}

export function AttentionsWorkspace({
  controls,
  generation,
  version,
  fresh,
  onAnswer,
  onLocate,
  onDecision,
  onNavigate,
  busy,
}: {
  controls: WorkspaceControls;
  generation: number;
  version: number;
  fresh: boolean;
  onAnswer: (attention: Attention) => void;
  onLocate: (attention: Attention) => void;
  onDecision: (attention: Attention, decision: "approve" | "reject") => void;
  onNavigate: (route: Route, trigger?: HTMLElement) => void;
  busy: boolean;
}) {
  const cursor = controls.pages.attentions ?? 0;
  const setCursor = (value: number) => controls.setPage("attentions", value);
  const list = useRecord(
    `attentions/${cursor}`,
    (signal) => api.attentions(cursor, signal),
    generation,
    version,
  );
  return (
    <>
      <div className="eyebrow">工作区 / 需要处理</div>
      <h1>需要你的决定</h1>
      <LoadState loading={list.loading} error={list.error} />
      {list.value?.total === 0 ? (
        <Empty>当前没有待处理事项。</Empty>
      ) : (
        list.value?.items.map((attention) => (
          <div key={attention.attention_id}>
            <button className="text-button" onClick={() => onLocate(attention)}>
              定位主聊天事项
            </button>
            <AttentionCard
              attention={attention}
              onNavigate={onNavigate}
              onAnswer={onAnswer}
              onDecision={onDecision}
              busy={busy}
              enabled={fresh}
            />
          </div>
        ))
      )}
      <PageControls
        cursor={cursor}
        page={list.value}
        loading={list.loading}
        onCursor={setCursor}
      />
    </>
  );
}
