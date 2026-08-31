import { ArrowRightLeft, ShieldCheck } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { AuditSessionHandoff } from "@/pages/AuditSession/types";

interface HandoffTracePanelProps {
  handoffs: AuditSessionHandoff[];
}

export function HandoffTracePanel({ handoffs }: HandoffTracePanelProps) {
  return (
    <Card className="overflow-hidden rounded-none border-0 bg-white shadow-none">
      <CardHeader className="border-b border-[#e6ede8] bg-[linear-gradient(90deg,#ffffff,#fbf7ef)] px-4 py-4">
        <CardTitle className="flex items-center gap-3 text-base font-semibold text-slate-900">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-[rgba(227,247,235,.95)] text-emerald-700 shadow-sm">
            <ArrowRightLeft className="h-5 w-5" />
          </span>
          Verification Handoff
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 p-4">
        {handoffs.length === 0 ? (
          <p className="text-sm leading-6 text-muted-foreground">这次会话还没有产生 verification handoff。</p>
        ) : (
          handoffs.map((handoff) => (
            <div key={handoff.id} className="min-w-0 rounded-[20px] border border-[#e2eae5] bg-white p-4 shadow-[0_10px_25px_rgba(86,105,97,.05)] [overflow-wrap:anywhere]">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <ShieldCheck className="h-4 w-4 text-emerald-600" />
                    <p className="text-sm font-semibold text-slate-900">{handoff.target}</p>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{handoff.status}</p>
                </div>
                <p className="text-[11px] text-slate-400">{new Date(handoff.created_at).toLocaleString("zh-CN")}</p>
              </div>
              {handoff.payload.summary ? (
                <p className="mt-3 max-h-48 overflow-y-auto rounded-2xl bg-[#f7f8f6] px-3 py-2 text-sm leading-6 text-slate-700 custom-scrollbar [overflow-wrap:anywhere]">{String(handoff.payload.summary)}</p>
              ) : null}
              <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-[#f6f9f7] p-3 text-xs leading-6 text-slate-700 custom-scrollbar [overflow-wrap:anywhere]">
                {JSON.stringify(handoff.payload, null, 2)}
              </pre>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}
