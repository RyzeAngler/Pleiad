import type {
  Approval,
  Conversation,
  ConversationMessage,
  Execution,
  ExecutionDetails,
  ExecutionEvent,
  Graph,
  Resources,
  Space,
  SpaceDraft,
  SpaceDraftMessage,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/v1${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    throw new ApiError("无法连接 Weave 服务。请检查后端是否正在运行。", 0);
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body: unknown = await response.json();
      if (typeof body === "object" && body !== null && "detail" in body) {
        detail =
          typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail);
      }
    } catch {
      /* Preserve the HTTP status when the server does not send JSON. */
    }
    throw new ApiError(detail, response.status);
  }
  return response.json() as Promise<T>;
}

export const api = {
  spaces: (signal?: AbortSignal) => request<Space[]>("/spaces", { signal }),
  deleteSpace: (id: string) =>
    request<{ deleted: true }>(`/spaces/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  deleteConversation: (id: string) =>
    request<{ deleted: true }>(`/conversations/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  draftSpace: (
    messages: SpaceDraftMessage[],
    draft: SpaceDraft | null,
    signal?: AbortSignal,
  ) =>
    request<{ reply: string; draft: SpaceDraft }>("/spaces/draft", {
      method: "POST",
      signal,
      body: JSON.stringify({ messages, draft }),
    }),
  createSpace: (draft: SpaceDraft) =>
    request<Space>("/spaces", {
      method: "POST",
      body: JSON.stringify(draft),
    }),
  updateSpace: (space: Space) =>
    request<Space>(`/spaces/${encodeURIComponent(space.space_id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        name: space.name,
        instructions: space.instructions,
        preferred_tool_codes: space.preferred_tool_codes,
        preferred_skill_codes: space.preferred_skill_codes,
        saved_graph: space.saved_graph,
        expected_revision: space.revision,
      }),
    }),
  conversations: (spaceId: string, signal?: AbortSignal) =>
    request<Conversation[]>(
      `/spaces/${encodeURIComponent(spaceId)}/conversations`,
      { signal },
    ),
  createConversation: (spaceId: string, title: string) =>
    request<Conversation>(
      `/spaces/${encodeURIComponent(spaceId)}/conversations`,
      { method: "POST", body: JSON.stringify({ title }) },
    ),
  resources: (signal?: AbortSignal) =>
    request<Resources>("/resources", { signal }),
  savedGraph: (spaceId: string, signal?: AbortSignal) =>
    request<Graph>(`/spaces/${encodeURIComponent(spaceId)}/saved-graph`, {
      signal,
    }),
  executions: (conversationId: string, signal?: AbortSignal) =>
    request<Execution[]>(
      `/executions?conversation_id=${encodeURIComponent(conversationId)}`,
      { signal },
    ),
  messages: (conversationId: string, signal?: AbortSignal) =>
    request<ConversationMessage[]>(
      `/conversations/${encodeURIComponent(conversationId)}/messages`,
      { signal },
    ),
  submit: (conversationId: string, objective: string) =>
    request<{ execution: Execution }>("/executions", {
      method: "POST",
      body: JSON.stringify({
        conversation_id: conversationId,
        objective,
        input: {},
      }),
    }),
  details: (id: string, signal?: AbortSignal) =>
    request<ExecutionDetails>(`/executions/${encodeURIComponent(id)}/details`, {
      signal,
    }),
  events: (id: string, signal?: AbortSignal) =>
    request<ExecutionEvent[]>(`/executions/${encodeURIComponent(id)}/events`, {
      signal,
    }),
  approvals: (id: string, signal?: AbortSignal) =>
    request<Approval[]>(`/executions/${encodeURIComponent(id)}/approvals`, {
      signal,
    }),
  decide: (approval: Approval, decision: "approve" | "reject") =>
    request<{ execution: Execution }>(
      `/executions/${encodeURIComponent(approval.execution_id)}/approvals/${encodeURIComponent(approval.interruption.interruption_id)}`,
      { method: "POST", body: JSON.stringify({ decision }) },
    ),
  cancel: (id: string) =>
    request<{ execution: Execution }>(
      `/executions/${encodeURIComponent(id)}/cancel`,
      { method: "POST" },
    ),
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求未完成，请重试。";
}
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
