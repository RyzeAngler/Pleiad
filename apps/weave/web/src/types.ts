export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export interface Page<T> {
  items: T[];
  total: number;
  next_cursor: number | null;
  read_sequence: number;
}
export type ExecutionStatus =
  | "queued"
  | "running"
  | "waiting_input"
  | "waiting_approval"
  | "waiting_children"
  | "paused"
  | "blocked"
  | "cancelling"
  | "succeeded"
  | "failed"
  | "cancelled";
export interface Actions {
  available_actions: string[];
  disabled_reasons: Record<string, string>;
}
export interface ContextReference {
  kind: "space" | "execution" | "artifact" | "graph_node";
  id: string;
  execution_id?: string;
  graph_run_id?: string;
  node_id?: string;
  node_attempt_id?: string;
}
export interface Message {
  message_id: string;
  chat_sequence: number;
  role: "user" | "assistant" | "system";
  body: string;
  source_key: string;
  created_at: string;
  attachment_ids: string[];
  target_execution_ids: string[];
  context_refs: ContextReference[];
  reply_to_message_id: string | null;
  report_ids: string[];
  source_message_ids?: string[];
  reports: ReportSummary[];
  artifacts: Artifact[];
  input_dispositions: (InputDisposition & {
    execution_objective: string;
    space_name: string;
    reassignment_pending: boolean;
  })[];
  handbacks: Handback[];
  handback_resolutions: HandbackResolution[];
}
export interface ReportSummary {
  report_id: string;
  execution_id: string;
  kind: "progress" | "needs_input" | "ready" | "failed";
  summary: string;
  artifact_ids: string[];
}
export interface SpaceConfiguration {
  name: string;
  purpose: string;
  instructions: string;
  model_role: string;
  tool_codes: string[];
  skill_codes: string[];
  resource_bindings: string[];
}
export interface Space extends SpaceConfiguration, Actions {
  space_id: string;
  revision: number;
  status: "active" | "archived";
  deletion_pending: boolean;
  deleted: boolean;
  created_at: string;
  active_execution_count: number;
  pending_attention_count: number;
}
export interface Execution extends Actions {
  execution_id: string;
  space_id: string;
  parent_execution_id: string | null;
  source_execution_id: string | null;
  objective: string;
  delivery_contract: {
    require_text: boolean;
    required_artifact_ids: string[];
    required_verifications: (
      | { kind: "artifact_integrity"; name: string; artifact_name: string }
      | {
          kind: "tool_result";
          name: string;
          tool_code: string;
          arguments: JsonObject;
          expected_output: Json;
        }
    )[];
    required_children: string[];
  };
  session_id: string;
  revision: number;
  status: ExecutionStatus;
  control_intent: "none" | "pause" | "cancel";
  reason: string | null;
  config_snapshot: Space;
  input_watermark: number;
  budget_limit: number;
  budget_consumed: number;
  budget_reserved: number;
  result: string | null;
  unknown_effects: string[];
  checkpoint_ref: string | null;
  current_run_id: string | null;
  run_attempt: number;
  created_at: string;
  pause_requested: boolean;
  pending_prerequisites: string[];
}
export interface Verification {
  name: string;
  producer: "host" | "worker";
  status: "passed" | "failed" | "unknown";
  evidence_refs: string[];
  executed_at: string | null;
}
export interface Report {
  report_id: string;
  execution_id: string;
  run_id: string;
  input_watermark: number;
  kind: "progress" | "needs_input" | "ready" | "failed";
  summary: string;
  artifact_ids: string[];
  verification: Verification[];
  unresolved: string[];
  question: string | null;
  rejection_reasons: string[];
}
export interface Attention extends Actions {
  attention_id: string;
  execution_objective: string | null;
  space_name: string | null;
  execution_id: string | null;
  kind: "clarification" | "approval" | "review" | "recovery";
  question: string;
  options: string[];
  action_digest: string | null;
  action: {
    tool_code: string;
    tool_name: string;
    arguments: JsonObject;
  } | null;
  checkpoint_ref: string | null;
  revision: number;
  status: "pending" | "resolved" | "expired" | "withdrawn";
  expires_at: string | null;
  resolution: string | null;
}
export interface Artifact {
  artifact_id: string;
  created_at: string;
  name: string;
  mime: string;
  size: number;
  digest: string;
  owner_execution_id: string;
  graph_run_id: string | null;
  node_attempt_id: string | null;
  previewable: boolean;
  downloadable: boolean;
  deletion_state: "active" | "deleting" | "deleted" | "corrupt";
}
export interface Attachment {
  attachment_id: string;
  client_attachment_id: string;
  name: string;
  mime: string;
  size: number;
  digest: string;
  status: "ready" | "referenced" | "deleted";
  expires_at: string | null;
}
export interface Delivery extends Actions {
  delivery_id: string;
  input_id: string;
  target_kind: "router" | "execution";
  target_id: string;
  state: "pending" | "delivered" | "failed";
  generation: number;
  attempts: number;
  sdk_sequence: number | null;
  last_error: string | null;
}
export interface ExecutionInput {
  input_id: string;
  origin_input_id: string;
  target_sequence: number;
  source: string;
  content: string;
  causation_id: string | null;
  status: "pending" | "claimed" | "handled";
  sdk_consumption: {
    sequence: number;
    accepted_at: string;
    state: "accepted" | "claimed" | "consumed";
    run_id: string | null;
    step_id: string | null;
  } | null;
}
export interface InputDisposition {
  origin_input_id: string;
  target_execution_id: string;
  input_id: string;
  revision: number;
  state: "owned" | "handled" | "returned";
  outcome_refs: string[];
  handback_id: string | null;
}
export interface HandbackResolution {
  handback_id: string;
  run_id: string;
  action_refs: {
    run_id: string;
    call_id: string;
    operation_id: string | null;
  }[];
  reconciliation_id: string;
  evidence_ids: string[];
  reconciled_revision: number;
  resolved_at: string;
}
export interface Handback {
  handback_id: string;
  origin_input_id: string;
  target_execution_id: string;
  input_id: string;
  reason: string;
  already_handled_refs: string[];
  effect_refs: string[];
  unknown_effects: boolean;
  run_id: string | null;
  action_refs: {
    run_id: string;
    call_id: string;
    operation_id: string | null;
  }[];
  reroute_round: number;
}
export interface ExecutionDetail extends Execution {
  read_sequence: number;
  session_read_sequence: number;
  inputs: ExecutionInput[];
  deliveries: Delivery[];
  dispositions: InputDisposition[];
  handbacks: Handback[];
  handback_resolutions: HandbackResolution[];
  reports: Report[];
  children: Execution[];
  artifacts: Artifact[];
  attentions: Attention[];
  runs: ExecutionRun[];
}
export interface SpaceDetail extends Space {
  executions: Page<Execution>;
  artifacts: Artifact[];
  attentions: Attention[];
}
export interface Snapshot {
  snapshot_sequence: number;
  routing: { state: "idle" | "processing"; pending_input_count: number };
  messages: Page<Message>;
  spaces: Page<Space>;
  executions: Page<Execution>;
  attentions: Page<Attention>;
  capabilities: {
    csrf_token: string;
    max_message_bytes: number;
    max_attachment_bytes: number;
    max_message_attachment_bytes: number;
    max_attachments: number;
    max_context_refs: number;
    max_target_executions: number;
    attachment_mime_types?: string[];
  };
}
export interface ProductEvent<T = JsonObject> {
  sequence: number;
  event_id: string;
  subject: string;
  type: string;
  data: T;
  created_at: string;
}
export interface DeletionPreview {
  space_id: string;
  space_revision: number;
  preview_token: string;
  expires_at: string;
  execution_ids: string[];
  artifact_ids: string[];
  shared_artifact_ids: string[];
  blockers: string[];
  scopes: {
    sdk_session_count: number;
    task_file_count: number;
    space_fact_count: number;
    digest: string;
    blockers: string[];
  };
}
export interface Resource {
  kind: string;
  code: string;
  name: string;
  description: string | null;
  available: boolean;
  disabled_reason: string | null;
  configuration_location: string | null;
}
export interface ResourceDetail extends Resource {
  metadata: JsonObject;
  configuration: JsonObject;
  input_schema: JsonObject | null;
  output_schema: JsonObject | null;
  content: string | null;
  files: string[];
}
export interface ExecutionRun {
  execution_id: string;
  attempt: number;
  invocation_id: string;
  run_id: string | null;
  state: string;
  checkpoint_ref: string | null;
  usage: JsonObject;
}
export interface SequencePage<T> {
  items: T[];
  total: number;
  next_before_sequence: number | null;
  latest_sequence: number | null;
  read_sequence: number;
}
export interface ExecutionRunPage extends SequencePage<ExecutionRun> {
  latest_run_id: string | null;
}
export interface SDKToolCall {
  id: string;
  name: string;
  arguments: Json;
}
export interface SDKMessage {
  message_id: string;
  role: string;
  content: Json;
  tool_call_id?: string;
  tool_calls?: SDKToolCall[];
}
export interface ModelStep {
  step_id: string;
  run_id: string;
  sequence: number;
  run_sequence: number;
  agent_id: string;
  input_ids: string[];
  state: "active" | "completed" | "aborted";
  messages: SDKMessage[];
}
export interface RunOperation {
  operation_id: string;
  call_id: string;
  step_id: string;
  state:
    | "prepared"
    | "running"
    | "succeeded"
    | "failed"
    | "cancel_requested"
    | "cancelled"
    | "unknown";
}
export type ToolResultReference =
  | {
      kind: "tool_message";
      tool_result_id: string;
      step_id: string;
      call_id: string;
    }
  | {
      kind: "operation";
      tool_result_id: string;
      operation_id: string;
      state: RunOperation["state"];
      step_id: string;
      call_id: string;
    };
export interface RunDetail {
  run: ExecutionRun;
  steps: SequencePage<ModelStep>;
  operations: RunOperation[];
  tool_results: ToolResultReference[];
}
export interface GraphNode {
  node_id: string;
  objective: string;
  prompt: string;
  tool_codes: string[];
  skill_codes: string[];
}
export interface Graph {
  graph_id: string;
  revision: number;
  objective: string;
  nodes: GraphNode[];
  edges: { source_node_id: string; target_node_id: string }[];
}
export interface GraphRun {
  graph_run_id: string;
  execution_id: string;
  graph_id: string;
  graph_revision: number;
  parent_run_id: string;
  root_run_id: string;
  status: string;
  revision: number;
  created_at: string;
  state: {
    node_states: { node_id: string; status: string; attempt: number }[];
  } | null;
}
export interface NodeAttempt {
  node_attempt_id: string;
  execution_id: string;
  graph_run_id: string;
  node_id: string;
  attempt: number;
  input: JsonObject;
  status: string;
  session_id: string | null;
  sdk_run_id: string | null;
  model_name: string | null;
  usage: JsonObject | null;
  artifact_ids: string[];
  error: { code: string; message: string } | null;
  created_at: string;
  finished_at: string | null;
}
export interface GraphDetail {
  graph_run: GraphRun;
  graph: Graph;
  node_attempts: NodeAttempt[];
  total: number;
  next_cursor: number | null;
  read_sequence: number;
}
export interface TextPage {
  id: string;
  text: string;
  offset: number;
  next_offset: number | null;
  total_bytes: number;
  read_scope: "product" | "session";
  read_sequence: number;
}
export interface ToolResultDisplay extends TextPage {
  format: "text" | "markdown" | "code";
}
export type Route =
  | { view: "chat" }
  | { view: "spaces" }
  | { view: "space"; space_id: string; tab?: string }
  | {
      view: "execution";
      execution_id: string;
      tab?: string;
      graph_run_id?: string;
      run_id?: string;
      node_id?: string;
      node_attempt_id?: string;
    }
  | { view: "resources" }
  | { view: "artifact"; artifact_id: string }
  | { view: "attachment"; attachment_id: string }
  | { view: "settings" }
  | { view: "attentions" };
export interface Draft {
  text: string;
  attachment_ids: string[];
  target_execution_ids: string[];
  context_refs: ContextReference[];
  reply_to_message_id: string | null;
  attention_id: string | null;
}
export interface WorkspaceControls {
  queries: Readonly<Record<string, string>>;
  pages: Readonly<Record<string, number>>;
  setQuery: (key: string, value: string) => void;
  setPage: (key: string, value: number) => void;
}
