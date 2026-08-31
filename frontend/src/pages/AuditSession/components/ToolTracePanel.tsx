import { useMemo, useState } from "react";
import { Check, ChevronDown, ChevronUp, Copy, DatabaseZap, Hammer, Loader2, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { AuditSessionToolCall } from "@/pages/AuditSession/types";
import type { DirectAuditApprovalScope } from "@/shared/api/agentDirectAudit";

interface ToolTracePanelProps {
  toolCalls: AuditSessionToolCall[];
  onApproveToolCall?: (toolCall: AuditSessionToolCall, scope: DirectAuditApprovalScope) => void | Promise<void>;
  approvalLoadingToolCallId?: string | null;
}

function formatPayload(payload: Record<string, unknown>) {
  const serialized = JSON.stringify(payload, null, 2);
  return serialized === "{}" ? "{}" : serialized;
}

function statusTone(status: string) {
  if (status === "completed" || status === "success") return "bg-emerald-50 text-emerald-700 border-emerald-200";
  if (status === "failed" || status === "error") return "bg-rose-50 text-rose-700 border-rose-200";
  return "bg-amber-50 text-amber-700 border-amber-200";
}

function getApprovalState(toolCall: AuditSessionToolCall) {
  const permissionMode = typeof toolCall.output_payload?.permission_mode === "string" ? toolCall.output_payload.permission_mode : null;
  const guardrailCode = typeof toolCall.output_payload?.guardrail_code === "string" ? toolCall.output_payload.guardrail_code : null;
  const permissionReason = typeof toolCall.output_payload?.permission_reason === "string" ? toolCall.output_payload.permission_reason : null;
  if (toolCall.status !== "denied" || permissionMode !== "ask") {
    return null;
  }
  return {
    guardrailCode,
    permissionReason,
  };
}

function getApprovalLabel(toolCall: AuditSessionToolCall, approvalState: ReturnType<typeof getApprovalState>) {
  if (!approvalState) {
    return null;
  }
  if (toolCall.tool_name === "Write") {
    if (approvalState.guardrailCode === "overwrite_existing_requires_approval") {
      return "批准覆盖此文件";
    }
    return "批准写入此文件";
  }
  if (toolCall.tool_name === "Bash" || toolCall.tool_name === "PowerShell") {
    if (approvalState.guardrailCode === "shell_destructive_command_requires_approval") {
      return `批准执行高风险${toolCall.tool_name}`;
    }
    if (approvalState.guardrailCode === "shell_outside_project_root_requires_approval") {
      return `批准执行跨目录${toolCall.tool_name}`;
    }
    return `批准运行${toolCall.tool_name}`;
  }
  return "批准此次操作";
}

function ToolTraceCard({
  toolCall,
  onApproveToolCall,
  approvalLoadingToolCallId,
}: {
  toolCall: AuditSessionToolCall;
  onApproveToolCall?: (toolCall: AuditSessionToolCall, scope: DirectAuditApprovalScope) => void | Promise<void>;
  approvalLoadingToolCallId?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const approvalState = getApprovalState(toolCall);
  const canApprove = Boolean(
    approvalState &&
      onApproveToolCall &&
      (
        (toolCall.tool_name === "Write" && approvalState.guardrailCode !== "artifact_exists_requires_overwrite") ||
        ((toolCall.tool_name === "Bash" || toolCall.tool_name === "PowerShell") &&
          (
            approvalState.guardrailCode === "shell_command_requires_approval" ||
            approvalState.guardrailCode === "shell_destructive_command_requires_approval" ||
            approvalState.guardrailCode === "shell_outside_project_root_requires_approval"
          ))
      ),
  );
  const approvalLoading = approvalLoadingToolCallId === toolCall.id;
  const approvalLabel = getApprovalLabel(toolCall, approvalState);

  const detailJson = useMemo(
    () => ({
      tool_name: toolCall.tool_name,
      sequence: toolCall.sequence,
      status: toolCall.status,
      duration_ms: toolCall.duration_ms,
      input_payload: toolCall.input_payload,
      output_payload: toolCall.output_payload,
      error_message: toolCall.error_message,
    }),
    [toolCall],
  );

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(JSON.stringify(detailJson, null, 2));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="min-w-0 rounded-[20px] border border-[#e2eae5] bg-white p-4 shadow-[0_10px_25px_rgba(86,105,97,.05)] [overflow-wrap:anywhere]">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <DatabaseZap className="h-4 w-4 text-amber-600" />
            <p className="text-sm font-semibold text-slate-900">{toolCall.tool_name}</p>
          </div>
          <p className="text-xs text-muted-foreground">序号 #{toolCall.sequence} · {toolCall.duration_ms ?? 0} ms</p>
        </div>
        <span className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${statusTone(toolCall.status)}`}>
          {toolCall.status}
        </span>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" onClick={handleCopy} className="h-8 rounded-full border-[rgba(209,218,213,.95)] bg-white px-3 text-xs">
          {copied ? <Check className="mr-1.5 h-3.5 w-3.5" /> : <Copy className="mr-1.5 h-3.5 w-3.5" />}
          {copied ? "已复制" : "复制 JSON"}
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={() => setExpanded((value) => !value)} className="h-8 rounded-full border-[rgba(209,218,213,.95)] bg-white px-3 text-xs">
          {expanded ? <ChevronUp className="mr-1.5 h-3.5 w-3.5" /> : <ChevronDown className="mr-1.5 h-3.5 w-3.5" />}
          {expanded ? "收起详情" : "展开详情"}
        </Button>
      </div>

      {approvalState ? (
        <div className="mt-4 rounded-2xl border border-amber-200 bg-amber-50/90 p-3 text-amber-900">
          <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide">
            <ShieldCheck className="h-3.5 w-3.5" />
            Needs Approval
          </div>
          <p className="text-xs leading-6">
            {approvalState.permissionReason || "This tool call was blocked by a Phase 3 guardrail and needs explicit approval."}
          </p>
          {approvalState.guardrailCode ? (
            <p className="mt-1 text-[11px] text-amber-800/80">Guardrail: {approvalState.guardrailCode}</p>
          ) : null}
          {canApprove ? (
            <div className="mt-3 space-y-2">
              <p className="text-[11px] text-amber-800/85">{approvalLabel}</p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={approvalLoading}
                  className="h-8 rounded-full border-amber-300 bg-white px-3 text-xs text-amber-900 hover:bg-amber-100"
                  onClick={() => void onApproveToolCall?.(toolCall, "single_use")}
                >
                  {approvalLoading ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="mr-1.5 h-3.5 w-3.5" />}
                  仅本次批准
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={approvalLoading}
                  className="h-8 rounded-full border-amber-300 bg-white px-3 text-xs text-amber-900 hover:bg-amber-100"
                  onClick={() => void onApproveToolCall?.(toolCall, "session")}
                >
                  {approvalLoading ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <ShieldCheck className="mr-1.5 h-3.5 w-3.5" />}
                  本会话批准
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {expanded ? (
        <div className="mt-4 space-y-3 text-xs">
          <div>
            <div className="mb-1 font-medium text-slate-500">Input</div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-2xl bg-[rgba(246,248,247,.95)] p-3 text-xs leading-6 text-slate-700 [overflow-wrap:anywhere]">{formatPayload(toolCall.input_payload)}</pre>
          </div>
          <div>
            <div className="mb-1 font-medium text-slate-500">Output</div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-2xl bg-[rgba(246,248,247,.95)] p-3 text-xs leading-6 text-slate-700 [overflow-wrap:anywhere]">{formatPayload(toolCall.output_payload)}</pre>
          </div>
          {toolCall.error_message && (
            <div className="rounded-2xl border border-rose-200 bg-rose-50 p-3 text-rose-700">
              <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide">
                <ShieldCheck className="h-3.5 w-3.5" />
                Error
              </div>
              <pre className="overflow-x-auto whitespace-pre-wrap text-xs leading-6">{toolCall.error_message}</pre>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function ToolTracePanel({ toolCalls, onApproveToolCall, approvalLoadingToolCallId }: ToolTracePanelProps) {
  return (
    <Card className="overflow-hidden rounded-none border-0 bg-white shadow-none">
      <CardHeader className="border-b border-[#e6ede8] bg-[linear-gradient(90deg,#ffffff,#fff8ed)] px-4 py-4">
        <CardTitle className="flex items-center gap-3 text-base font-semibold text-slate-900">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-[rgba(255,240,214,.95)] text-amber-700 shadow-sm">
            <Hammer className="h-5 w-5" />
          </span>
          Tool Trace
        </CardTitle>
      </CardHeader>
      <CardContent className="max-h-[360px] space-y-4 overflow-y-auto p-4 custom-scrollbar">
        {toolCalls.length === 0 ? (
          <p className="text-sm leading-6 text-muted-foreground">这次会话还没有记录任何工具调用。</p>
        ) : (
          toolCalls.map((toolCall) => (
            <ToolTraceCard
              key={toolCall.id}
              toolCall={toolCall}
              onApproveToolCall={onApproveToolCall}
              approvalLoadingToolCallId={approvalLoadingToolCallId}
            />
          ))
        )}
      </CardContent>
    </Card>
  );
}
