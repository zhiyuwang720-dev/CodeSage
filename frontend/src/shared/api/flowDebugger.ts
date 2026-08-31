import { apiClient } from './serverClient';

export interface DebugTaskListItem {
  id: string;
  project_id: string;
  name?: string | null;
  status: string;
  created_at: string;
  latest_event_at?: string | null;
  event_count: number;
  agent_count: number;
  tool_call_count: number;
}

export interface DebugTimelineEvent {
  id: string;
  event_type: string;
  sequence: number;
  phase?: string | null;
  message?: string | null;
  tool_name?: string | null;
  tool_input?: Record<string, unknown> | null;
  tool_output?: Record<string, unknown> | null;
  tool_duration_ms?: number | null;
  timestamp?: string | null;
  agent_name?: string | null;
  agent_type?: string | null;
  provider?: string | null;
  model?: string | null;
  iteration?: number | null;
  payload?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
}

export interface DebugHandoff {
  event_id: string;
  event_type: string;
  sequence: number;
  timestamp?: string | null;
  from_agent?: string | null;
  to_agent?: string | null;
  summary?: string | null;
  payload?: Record<string, unknown> | null;
}

export interface DebugTraceResponse {
  task: {
    id: string;
    name?: string | null;
    status: string;
  };
  summary: {
    event_count: number;
    agents: string[];
    phases: string[];
    tool_calls: number;
    handoff_count: number;
  };
  timeline: DebugTimelineEvent[];
  handoffs: DebugHandoff[];
}

export async function getDebugTasks(params?: { project_id?: string; status?: string; limit?: number }) {
  const response = await apiClient.get('/agent-tasks/debug-tasks', { params });
  return response.data as DebugTaskListItem[];
}

export async function getDebugTrace(taskId: string) {
  const response = await apiClient.get(`/agent-tasks/${taskId}/debug-trace`);
  return response.data as DebugTraceResponse;
}
