import type {
  Artifact,
  Attachment,
  Attention,
  DeletionPreview,
  Delivery,
  Execution,
  ExecutionDetail,
  ExecutionRunPage,
  GraphDetail,
  GraphRun,
  JsonObject,
  Message,
  NodeAttempt,
  Page,
  Resource,
  ResourceDetail,
  RunDetail,
  Snapshot,
  Space,
  SpaceDetail,
  SpaceConfiguration,
  TextPage,
  ToolResultDisplay,
} from "./types";
let csrfToken = "";
export function configureCsrf(token: string) {
  csrfToken = token;
}
export function operationId(scope: string, payload: unknown): string {
  const key = `weave-pending-operation/${scope}`;
  const digest = JSON.stringify(payload);
  const saved = localStorage.getItem(key);
  if (saved !== null) {
    const value: unknown = JSON.parse(saved);
    if (
      value !== null &&
      typeof value === "object" &&
      "digest" in value &&
      "id" in value &&
      value.digest === digest &&
      typeof value.id === "string"
    )
      return value.id;
  }
  const id = crypto.randomUUID();
  localStorage.setItem(key, JSON.stringify({ digest, id }));
  return id;
}
export function completeOperation(scope: string) {
  localStorage.removeItem(`weave-pending-operation/${scope}`);
}
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly retryAfter: string | null = null,
  ) {
    super(message);
  }
}
export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  if (options.method && !["GET", "HEAD"].includes(options.method)) {
    if (!csrfToken) throw new ApiError("连接状态尚未核验，请先刷新。", 503);
    headers.set("X-Weave-CSRF", csrfToken);
  }
  try {
    response = await fetch(`/v1${path}`, { ...options, headers });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError("无法连接 Weave 服务。请检查连接后重试。", 0);
  }
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    let message = `请求失败（${response.status}）`;
    if (body && typeof body === "object") {
      if ("message" in body && typeof body.message === "string")
        message = body.message;
      else if ("detail" in body && typeof body.detail === "string")
        message = body.detail;
    }
    throw new ApiError(
      message,
      response.status,
      response.headers.get("Retry-After"),
    );
  }
  return response.json() as Promise<T>;
}
const id = (value: string) => encodeURIComponent(value);
const write = <T>(path: string, body: unknown, method = "POST") =>
  request<T>(path, { method, body: JSON.stringify(body) });
export const api = {
  async snapshot(signal?: AbortSignal) {
    const result = await request<Snapshot>("/snapshot", { signal });
    return result;
  },
  messages: (before?: number, signal?: AbortSignal, limit = 50) =>
    request<Page<Message>>(
      `/chat/messages?limit=${limit}${before === undefined ? "" : `&before_sequence=${before}`}`,
      { signal },
    ),
  submit: (body: unknown) =>
    write<{ message_id: string; sequence: number; accepted: true }>(
      "/chat/messages",
      body,
    ),
  spaces: (query = "", status = "active", cursor = 0, signal?: AbortSignal) =>
    request<Page<Space>>(
      `/spaces?limit=50&query=${id(query)}&status=${id(status)}&cursor=${cursor}`,
      { signal },
    ),
  space: (spaceId: string, signal?: AbortSignal) =>
    request<SpaceDetail>(`/spaces/${id(spaceId)}`, { signal }),
  updateSpace: (
    space: Space,
    configuration: SpaceConfiguration,
    requestId: string,
  ) =>
    write<Space>(
      `/spaces/${id(space.space_id)}`,
      {
        ...configuration,
        request_id: requestId,
        expected_revision: space.revision,
      },
      "PATCH",
    ),
  archive: (space: Space, restore: boolean, requestId: string) =>
    write<Space>(
      `/spaces/${id(space.space_id)}/${restore ? "restore" : "archive"}`,
      { request_id: requestId, expected_revision: space.revision },
    ),
  deletionPreview: (spaceId: string, signal?: AbortSignal) =>
    request<DeletionPreview>(`/spaces/${id(spaceId)}/deletion-preview`, {
      signal,
    }),
  deleteSpace: (space: Space, preview: DeletionPreview, requestId: string) =>
    write<{ deleted: true }>(
      `/spaces/${id(space.space_id)}`,
      {
        request_id: requestId,
        expected_revision: space.revision,
        preview_token: preview.preview_token,
      },
      "DELETE",
    ),
  executions: (spaceId?: string, cursor = 0, signal?: AbortSignal) =>
    request<Page<Execution>>(
      `/executions?limit=50&cursor=${cursor}${spaceId ? `&space_id=${id(spaceId)}` : ""}`,
      { signal },
    ),
  execution: (executionId: string, signal?: AbortSignal) =>
    request<ExecutionDetail>(`/executions/${id(executionId)}`, { signal }),
  control: (
    execution: Execution,
    action: "pause" | "resume" | "cancel",
    requestId: string,
  ) =>
    write<Execution>(`/executions/${id(execution.execution_id)}/${action}`, {
      request_id: requestId,
      expected_revision: execution.revision,
    }),
  retryExecution: (
    execution: Execution,
    requestId: string,
    supplement: string,
  ) =>
    write<{ new_execution_id: string; execution: ExecutionDetail }>(
      `/executions/${id(execution.execution_id)}/retry`,
      {
        request_id: requestId,
        expected_revision: execution.revision,
        supplement,
      },
    ),
  retryDelivery: (delivery: Delivery, requestId: string) =>
    write<Omit<Delivery, "available_actions" | "disabled_reasons">>(
      `/deliveries/${id(delivery.delivery_id)}/retry`,
      {
        request_id: requestId,
        expected_generation: delivery.generation,
      },
    ),
  attentions: (cursor = 0, signal?: AbortSignal) =>
    request<Page<Attention>>(
      `/attentions?status=pending&limit=50&cursor=${cursor}`,
      { signal },
    ),
  attention: (attentionId: string, signal?: AbortSignal) =>
    request<Attention>(`/attentions/${id(attentionId)}`, { signal }),
  resolve: (attentionId: string, body: unknown) =>
    write<{
      message_id: string;
      attention_revision: number;
      execution: Execution | null;
    }>(`/attentions/${id(attentionId)}/resolve`, body),
  async upload(file: File, clientId: string) {
    const form = new FormData();
    form.append("client_attachment_id", clientId);
    form.append("file", file);
    return request<Attachment>("/attachments", { method: "POST", body: form });
  },
  attachment: (attachmentId: string, signal?: AbortSignal) =>
    request<Attachment>(`/attachments/${id(attachmentId)}`, { signal }),
  deleteAttachment: (attachmentId: string, requestId: string) =>
    write<Attachment>(
      `/attachments/${id(attachmentId)}`,
      { request_id: requestId },
      "DELETE",
    ),
  artifact: (artifactId: string, signal?: AbortSignal) =>
    request<Artifact>(`/artifacts/${id(artifactId)}`, { signal }),
  contentText: (
    kind: "artifact" | "attachment",
    identity: string,
    offset: number,
    signal?: AbortSignal,
  ) =>
    request<TextPage>(
      `/${kind}s/${id(identity)}/text?offset=${offset}&max_bytes=8192`,
      { signal },
    ),
  artifactUrl: (artifactId: string, download = false) =>
    `/v1/artifacts/${id(artifactId)}/content?disposition=${download ? "attachment" : "inline"}`,
  attachmentUrl: (attachmentId: string, download = true) =>
    `/v1/attachments/${id(attachmentId)}/content?disposition=${download ? "attachment" : "inline"}`,
  runs: (executionId: string, before?: number, signal?: AbortSignal) =>
    request<ExecutionRunPage>(
      `/executions/${id(executionId)}/runs?limit=50${before === undefined ? "" : `&before_sequence=${before}`}`,
      { signal },
    ),
  run: (
    executionId: string,
    runId: string,
    before?: number,
    signal?: AbortSignal,
  ) =>
    request<RunDetail>(
      `/executions/${id(executionId)}/runs/${id(runId)}?limit=50${before === undefined ? "" : `&before_sequence=${before}`}`,
      { signal },
    ),
  graphRuns: (executionId: string, cursor = 0, signal?: AbortSignal) =>
    request<Page<GraphRun>>(
      `/executions/${id(executionId)}/graph-runs?limit=50&cursor=${cursor}`,
      { signal },
    ),
  graphRun: (
    executionId: string,
    graphRunId: string,
    cursor = 0,
    signal?: AbortSignal,
  ) =>
    request<GraphDetail>(
      `/executions/${id(executionId)}/graph-runs/${id(graphRunId)}?cursor=${cursor}&limit=100`,
      { signal },
    ),
  nodeAttempt: (executionId: string, attemptId: string, signal?: AbortSignal) =>
    request<NodeAttempt>(
      `/executions/${id(executionId)}/node-attempts/${id(attemptId)}`,
      { signal },
    ),
  toolResult: (
    executionId: string,
    resultId: string,
    offset = 0,
    signal?: AbortSignal,
  ) =>
    request<TextPage>(
      `/executions/${id(executionId)}/tool-results/${id(resultId)}?offset=${offset}&limit=8192`,
      { signal },
    ),
  toolResultDisplay: (
    executionId: string,
    resultId: string,
    offset = 0,
    signal?: AbortSignal,
  ) =>
    request<ToolResultDisplay>(
      `/executions/${id(executionId)}/tool-results/${id(resultId)}?view=display&offset=${offset}&limit=8192`,
      { signal },
    ),
  resources: (kind = "tools", query = "", cursor = 0, signal?: AbortSignal) =>
    request<Page<Resource>>(
      `/resources?kind=${id(kind)}&query=${id(query)}&cursor=${cursor}`,
      { signal },
    ),
  resource: (kind: string, code: string, signal?: AbortSignal) =>
    request<ResourceDetail>(`/resources/${id(kind)}/${id(code)}`, { signal }),
  async connection(signal?: AbortSignal) {
    const [health, readiness] = await Promise.all([
      fetch("/healthz", { signal }),
      fetch("/readyz", { signal }),
    ]);
    if (!health.ok || (readiness.status !== 200 && readiness.status !== 503))
      throw new ApiError(
        "连接检查请求失败，请重试。",
        !health.ok ? health.status : readiness.status,
      );
    const healthBody: unknown = await health.json();
    const readyBody: unknown = await readiness.json();
    if (
      healthBody === null ||
      typeof healthBody !== "object" ||
      !("status" in healthBody) ||
      healthBody.status !== "ok" ||
      readyBody === null ||
      typeof readyBody !== "object" ||
      !("ready" in readyBody) ||
      typeof readyBody.ready !== "boolean"
    )
      throw new ApiError("连接检查响应格式无效。", 502);
    return {
      healthy: true,
      ready: readiness.ok && readyBody.ready,
      readiness_status: readiness.status,
      checked_at: new Date().toISOString(),
    };
  },
  settings: (signal?: AbortSignal) =>
    request<JsonObject>("/settings", { signal }),
};
export const errorMessage = (error: unknown) =>
  error instanceof ApiError
    ? `${error.message}${error.retryAfter ? `，${error.retryAfter} 秒后可重试。` : ""}`
    : error instanceof Error
      ? error.message
      : "请求失败，请重试。";
export const isAbort = (error: unknown) =>
  error instanceof DOMException && error.name === "AbortError";
