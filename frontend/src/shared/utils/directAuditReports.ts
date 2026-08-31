import type { AuditSessionMessage } from "@/shared/api/auditSessions";
import type { ManagedVulnerability } from "@/shared/api/vulnerabilities";

export interface DirectAuditReportBundle {
  report_en: string;
  report_zh: string;
  report_cve: string;
}

export interface DirectAuditReportMessageMatch {
  message: AuditSessionMessage;
  bundle: DirectAuditReportBundle;
}

function isStringRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function parseDirectAuditReportBundle(content: string): DirectAuditReportBundle | null {
  const trimmed = content.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) {
    return null;
  }

  try {
    const parsed = JSON.parse(trimmed);
    if (!isStringRecord(parsed)) {
      return null;
    }
    if (
      typeof parsed.report_en !== "string" ||
      typeof parsed.report_zh !== "string" ||
      typeof parsed.report_cve !== "string"
    ) {
      return null;
    }
    return {
      report_en: parsed.report_en,
      report_zh: parsed.report_zh,
      report_cve: parsed.report_cve,
    };
  } catch {
    return null;
  }
}

export function getLatestDirectAuditReportMessage(
  messages: AuditSessionMessage[],
): DirectAuditReportMessageMatch | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== "assistant") {
      continue;
    }
    const bundle = parseDirectAuditReportBundle(message.content || "");
    if (!bundle) {
      continue;
    }
    return { message, bundle };
  }
  return null;
}

export function getSyncedDirectAuditMessageIds(
  vulnerabilities: ManagedVulnerability[],
): Set<string> {
  const messageIds = new Set<string>();
  vulnerabilities.forEach((item) => {
    const sourceMetadata = item.source_metadata;
    if (!isStringRecord(sourceMetadata)) {
      return;
    }
    const directAudit = sourceMetadata.direct_audit;
    if (!isStringRecord(directAudit) || typeof directAudit.message_id !== "string") {
      return;
    }
    const messageId = directAudit.message_id.trim();
    if (messageId) {
      messageIds.add(messageId);
    }
  });
  return messageIds;
}
