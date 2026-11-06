export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export interface GraphReference {
  graph_id: string;
  revision: number;
}
export interface SpaceDraft {
  name: string;
  instructions: string;
  preferred_tool_codes: string[];
  preferred_skill_codes: string[];
}
export interface SpaceDraftMessage {
  role: "user" | "assistant";
  content: string;
}
export interface Space {
  space_id: string;
  name: string;
  instructions: string;
  preferred_tool_codes: string[];
  preferred_skill_codes: string[];
  saved_graph: GraphReference | null;
  revision: number;
  created_at: string;
  updated_at: string;
}
export interface Conversation {
  conversation_id: string;
  space_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}
export type ExecutionStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "resume_queued"
  | "succeeded"
  | "failed"
  | "cancelled";
export interface Failure {
  code: string;
  message: string;
  type: string;
}
export interface Execution {
  execution_id: string;
  conversation_id: string;
  objective: string;
  status: ExecutionStatus;
  graph_id: string | null;
  graph_revision: number | null;
  output: JsonObject | null;
  error: Failure | null;
  created_at: string;
  updated_at: string;
}
export interface GraphNode {
  node_id: string;
  objective: string;
  prompt: string;
  model_role: string;
  tool_codes: string[];
  skill_codes: string[];
  input_schema: JsonObject;
  output_schema: JsonObject;
}
export interface Graph {
  graph_id: string;
  revision: number;
  objective: string;
  nodes: GraphNode[];
  edges: { source_node_id: string; target_node_id: string }[];
}
export interface NodeRun {
  node_run_id: string;
  graph_run_id: string | null;
  execution_id: string;
  graph_id: string;
  graph_revision: number;
  node_id: string;
  status: string;
  input: JsonObject | null;
  model_name: string | null;
  artifact_id: string | null;
  error: Failure | null;
  sdk_run_id: string | null;
  sdk_run_status: string | null;
  started_at: string;
  finished_at: string | null;
}
export interface Artifact {
  artifact_id: string;
  media_type: string;
  node_run_id: string;
  value: JsonObject;
}
export interface GraphRun {
  graph_run_id: string;
  graph_id: string;
  graph_revision: number;
  status: string;
  started_at: string;
  state: { node_states: { node_id: string; status: string }[] } | null;
}
export interface ExecutionDetails {
  execution: Execution;
  graphs: Graph[];
  node_runs: NodeRun[];
  graph_runs: GraphRun[];
  artifacts: Artifact[];
}
export interface ExecutionEvent {
  event_id: string;
  execution_id: string;
  event_type: string;
  timestamp: string;
  sequence: number;
  data: JsonObject;
  trace_id: string | null;
  span_id: string | null;
}
export interface ConversationMessage {
  message_id: string;
  turn_id: string;
  turn_sequence: number;
  agent_id: string;
  turn_completed: boolean;
  role: string;
  content: string | null;
  refusal: string | null;
  tool_call_id: string | null;
  tool_calls: { id: string; name: string; arguments: JsonObject | string }[];
}
export interface Approval {
  execution_id: string;
  deadline: string;
  interruption: {
    interruption_id: string;
    tool_code: string;
    tool_name: string;
    reason: string;
    arguments: JsonObject;
    details: JsonObject;
  };
}
export interface Resource {
  code: string;
  name: string;
  description: string;
}
export interface Resources {
  models: { role: string; model: string }[];
  tools: Resource[];
  skills: Resource[];
  workspace_path: string;
  capability_file: string;
}
