import { apiClient } from "./serverClient";
import type { AuditSessionDetail, AuditSessionMessage, AuditSessionStreamEvent } from "./auditSessions";
import type { ManagedVulnerability } from "./vulnerabilities";

export interface CreateDirectAuditSessionRequest {
  project_id: string;
  content: string;
  guardrails_enabled?: boolean;
}

export type DirectAuditApprovalScope = "single_use" | "session";

export async function listDirectAuditSessions(projectId: string): Promise<AuditSessionDetail[]> {
  const response = await apiClient.get("/agent-direct-audit/sessions", {
    params: { project_id: projectId },
  });
  return response.data;
}

export async function createDirectAuditSession(
  payload: CreateDirectAuditSessionRequest,
): Promise<AuditSessionDetail> {
  const response = await apiClient.post("/agent-direct-audit/sessions", payload);
  return response.data;
}

export async function streamCreateDirectAuditSession(
  payload: CreateDirectAuditSessionRequest,
  handlers: {
    onEvent?: (event: AuditSessionStreamEvent) => void;
    signal?: AbortSignal;
  } = {},
): Promise<void> {
  const token = getAccessToken();
  if (!token) {
    throw new Error("Not authenticated");
  }

  const response = await fetch("/api/v1/agent-direct-audit/sessions/stream", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
    signal: handlers.signal,
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Unable to read stream body");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.remaining;

    for (const event of parsed.events) {
      handlers.onEvent?.(event);
      if (event.type === "error") {
        throw new Error(event.message_text || "Streaming failed");
      }
    }
  }
}

export async function getDirectAuditSession(sessionId: string): Promise<AuditSessionDetail> {
  const response = await apiClient.get(`/agent-direct-audit/sessions/${sessionId}`);
  return response.data;
}

export async function updateDirectAuditGuardrails(
  sessionId: string,
  enabled: boolean,
): Promise<AuditSessionDetail> {
  const response = await apiClient.patch(`/agent-direct-audit/sessions/${sessionId}/guardrails`, { enabled });
  return response.data;
}

export async function getDirectAuditSessionMessages(sessionId: string): Promise<AuditSessionMessage[]> {
  const response = await apiClient.get(`/agent-direct-audit/sessions/${sessionId}/messages`);
  return response.data;
}

export async function listDirectAuditManagedVulnerabilities(
  sessionId: string,
): Promise<ManagedVulnerability[]> {
  const response = await apiClient.get(`/agent-direct-audit/sessions/${sessionId}/managed-vulnerabilities`);
  return response.data;
}

export async function syncLatestDirectAuditManagedVulnerability(
  sessionId: string,
): Promise<ManagedVulnerability> {
  const response = await apiClient.post(
    `/agent-direct-audit/sessions/${sessionId}/managed-vulnerabilities/sync-latest-report`,
  );
  return response.data;
}

export async function postDirectAuditSessionMessage(
  sessionId: string,
  content: string,
): Promise<AuditSessionMessage> {
  const response = await apiClient.post(`/agent-direct-audit/sessions/${sessionId}/messages`, { content });
  return response.data;
}

function getAccessToken(): string | null {
  return localStorage.getItem("access_token") || sessionStorage.getItem("access_token");
}

function parseSseChunk(buffer: string): { events: AuditSessionStreamEvent[]; remaining: string } {
  const parts = buffer.split("\n\n");
  const remaining = parts.pop() ?? "";
  const events: AuditSessionStreamEvent[] = [];

  for (const part of parts) {
    const dataLines = part
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trim());
    if (dataLines.length === 0) {
      continue;
    }
    try {
      events.push(JSON.parse(dataLines.join("\n")) as AuditSessionStreamEvent);
    } catch (error) {
      console.error("[DirectAuditStream] Failed to parse SSE event", error, dataLines);
    }
  }

  return { events, remaining };
}

export async function streamDirectAuditSessionMessage(
  sessionId: string,
  content: string,
  handlers: {
    onEvent?: (event: AuditSessionStreamEvent) => void;
    signal?: AbortSignal;
  } = {},
): Promise<void> {
  const token = getAccessToken();
  if (!token) {
    throw new Error("Not authenticated");
  }

  const response = await fetch(`/api/v1/agent-direct-audit/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ content }),
    signal: handlers.signal,
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Unable to read stream body");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.remaining;

    for (const event of parsed.events) {
      handlers.onEvent?.(event);
      if (event.type === "error") {
        throw new Error(event.message_text || "Streaming failed");
      }
    }
  }
}

export async function streamApproveDirectAuditToolCall(
  sessionId: string,
  toolCallId: string,
  scope: DirectAuditApprovalScope,
  handlers: {
    onEvent?: (event: AuditSessionStreamEvent) => void;
    signal?: AbortSignal;
  } = {},
): Promise<void> {
  const token = getAccessToken();
  if (!token) {
    throw new Error("Not authenticated");
  }

  const response = await fetch(`/api/v1/agent-direct-audit/sessions/${sessionId}/tool-calls/${toolCallId}/approve/stream`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ scope }),
    signal: handlers.signal,
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Unable to read stream body");
  }

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }

    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.remaining;

    for (const event of parsed.events) {
      handlers.onEvent?.(event);
      if (event.type === "error") {
        throw new Error(event.message_text || "Streaming failed");
      }
    }
  }
}
