import { BookMarked, MemoryStick } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { AuditSessionMemory } from "@/pages/AuditSession/types";

interface MemoryTracePanelProps {
  memories: AuditSessionMemory[];
}

export function MemoryTracePanel({ memories }: MemoryTracePanelProps) {
  return (
    <Card className="overflow-hidden rounded-none border-0 bg-white shadow-none">
      <CardHeader className="border-b border-[#e6ede8] bg-[linear-gradient(90deg,#ffffff,#eff8fd)] px-4 py-4">
        <CardTitle className="flex items-center gap-3 text-base font-semibold text-slate-900">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-[rgba(226,243,255,.95)] text-sky-700 shadow-sm">
            <MemoryStick className="h-5 w-5" />
          </span>
          Memory Trace
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 p-4">
        {memories.length === 0 ? (
          <p className="text-sm leading-6 text-muted-foreground">这次会话还没有附加任何 instruction 或 recall memory。</p>
        ) : (
          memories.map((memory) => (
            <div key={memory.id} className="min-w-0 rounded-[20px] border border-[#e2eae5] bg-white p-4 shadow-[0_10px_25px_rgba(86,105,97,.05)] [overflow-wrap:anywhere]">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <BookMarked className="h-4 w-4 text-sky-600" />
                    <p className="text-sm font-semibold text-slate-900">{memory.title}</p>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{memory.memory_kind} · {memory.source_type}</p>
                </div>
                <div className="rounded-full bg-sky-50 px-2.5 py-1 text-[11px] font-medium text-sky-700">
                  #{memory.sequence} · {memory.relevance_score ?? "-"}
                </div>
              </div>
              <p className="mt-3 break-all text-xs leading-6 text-muted-foreground">{memory.source_ref}</p>
              <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-2xl bg-[#f6f9f7] p-3 text-xs leading-6 text-slate-700 custom-scrollbar [overflow-wrap:anywhere]">{memory.content}</pre>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}
