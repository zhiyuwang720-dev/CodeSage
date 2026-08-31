import type { ComponentType } from "react";
import { ChevronDown, Clock, Database, FolderGit2, ListChecks } from "lucide-react";

import type { AuditSessionDetail } from "@/pages/AuditSession/types";

export function AuditSessionHeader({ session }: { session: AuditSessionDetail }) {
  const createdAt = new Date(session.created_at).toLocaleString("zh-CN");
  const updatedAt = new Date(session.updated_at).toLocaleString("zh-CN");

  return (
    <div className="min-w-0 flex-1 rounded-[26px] border border-[#dce8e1] bg-white px-3 py-2 shadow-[0_14px_40px_rgba(86,105,97,0.06)]">
      <details className="group">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3">
          <span className="flex min-w-0 items-center gap-2 rounded-full bg-[#f6faf7] px-3 py-2 text-sm font-semibold text-slate-800">
            <Database className="h-4 w-4 text-[#6fa27b]" />
            <span>会话详情</span>
            <ChevronDown className="h-4 w-4 text-slate-400 transition group-open:rotate-180" />
          </span>
          <div className="hidden min-w-0 items-center justify-end gap-2 sm:flex">
            <InfoPill icon={Clock} label="创建" value={createdAt} />
            <InfoPill icon={Clock} label="更新" value={updatedAt} />
          </div>
        </summary>
        <div className="mt-3 grid gap-3 border-t border-[#e6ede8] px-1 pt-3 text-sm text-slate-600 md:grid-cols-3">
          <div className="flex gap-2 sm:hidden">
            <InfoPill icon={Clock} label="创建" value={createdAt} />
            <InfoPill icon={Clock} label="更新" value={updatedAt} />
          </div>
          <DetailItem icon={Database} label="会话 ID" value={session.id} />
          <DetailItem icon={FolderGit2} label="项目 ID" value={session.project_id} />
          <DetailItem icon={ListChecks} label="任务 ID" value={session.task_id || "未绑定"} />
        </div>
      </details>
    </div>
  );
}

function InfoPill({
  icon: Icon,
  label,
  value,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
}) {
  return (
    <div className="inline-flex min-w-0 items-center gap-2 rounded-full border border-[#e0e8e3] bg-white px-3 py-2 text-slate-600 shadow-sm">
      <Icon className="h-4 w-4 shrink-0 text-slate-400" />
      <span className="shrink-0 text-xs font-semibold text-slate-500">{label}</span>
      <span className="truncate text-xs">{value}</span>
    </div>
  );
}

function DetailItem({
  icon: Icon,
  label,
  value,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
}) {
  return (
    <div className="min-w-0 rounded-2xl border border-[#e2eae5] bg-[#fbfdfb] p-3">
      <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase text-slate-400">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="break-all text-sm leading-6 text-slate-700">{value}</div>
    </div>
  );
}
