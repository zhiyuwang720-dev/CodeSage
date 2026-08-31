import { useMemo, useState } from "react";
import { Bot, Check, ChevronDown, ChevronUp, Copy, Sparkles, WandSparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { AuditSessionSkill, AuditSessionSkillInvocation } from "@/pages/AuditSession/types";

interface SkillTracePanelProps {
  skills: AuditSessionSkill[];
  skillInvocations: AuditSessionSkillInvocation[];
}

function formatPayload(payload: Record<string, unknown>) {
  const serialized = JSON.stringify(payload, null, 2);
  return serialized === "{}" ? "{}" : serialized;
}

function SkillCatalogCard({ skill }: { skill: AuditSessionSkill }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const detailJson = useMemo(
    () => ({
      name: skill.name,
      skill_ref: skill.skill_ref,
      matched: skill.matched,
      description: skill.description,
    }),
    [skill],
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
        <div>
          <div className="flex items-center gap-2">
            <Bot className="h-4 w-4 text-violet-600" />
            <p className="text-sm font-semibold text-slate-900">{skill.name}</p>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">{skill.skill_ref}</p>
        </div>
        <span className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${skill.matched ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
          {skill.matched ? "matched" : "available"}
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

      {expanded ? (
        <div className="mt-4 space-y-3 text-xs">
          {skill.description ? <p className="rounded-2xl bg-[rgba(248,244,255,.92)] px-3 py-2 text-sm leading-6 text-slate-700">{skill.description}</p> : null}
          <div>
            <div className="mb-1 font-medium text-slate-500">Skill JSON</div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-2xl bg-[rgba(246,248,247,.95)] p-3 text-xs leading-6 text-slate-700 [overflow-wrap:anywhere]">{JSON.stringify(detailJson, null, 2)}</pre>
          </div>
        </div>
      ) : (
        skill.description ? <p className="mt-3 text-xs leading-6 text-muted-foreground line-clamp-2">{skill.description}</p> : null
      )}
    </div>
  );
}

function SkillInvocationCard({ invocation }: { invocation: AuditSessionSkillInvocation }) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const detailJson = useMemo(
    () => ({
      skill_ref: invocation.skill_ref,
      sequence: invocation.sequence,
      status: invocation.status,
      input_payload: invocation.input_payload,
      output_payload: invocation.output_payload,
      error_message: invocation.error_message,
    }),
    [invocation],
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
        <div>
          <div className="flex items-center gap-2">
            <WandSparkles className="h-4 w-4 text-violet-600" />
            <p className="text-sm font-semibold text-slate-900">{invocation.skill_ref}</p>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">序号 #{invocation.sequence} · {invocation.status}</p>
        </div>
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

      {expanded ? (
        <div className="mt-4 space-y-3 text-xs">
          <div>
            <div className="mb-1 font-medium text-slate-500">Input</div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-2xl bg-[rgba(246,248,247,.95)] p-3 text-xs leading-6 text-slate-700 [overflow-wrap:anywhere]">{formatPayload(invocation.input_payload)}</pre>
          </div>
          <div>
            <div className="mb-1 font-medium text-slate-500">Output</div>
            <pre className="overflow-x-auto whitespace-pre-wrap break-words rounded-2xl bg-[rgba(246,248,247,.95)] p-3 text-xs leading-6 text-slate-700 [overflow-wrap:anywhere]">{formatPayload(invocation.output_payload)}</pre>
          </div>
          {invocation.error_message ? (
            <div className="rounded-2xl border border-rose-200 bg-rose-50 p-3 text-rose-700">
              <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide">Error</div>
              <pre className="overflow-x-auto whitespace-pre-wrap text-xs leading-6">{invocation.error_message}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function SkillTracePanel({ skills, skillInvocations }: SkillTracePanelProps) {
  return (
    <Card className="overflow-hidden rounded-none border-0 bg-white shadow-none">
      <CardHeader className="border-b border-[#e6ede8] bg-[linear-gradient(90deg,#ffffff,#f6f2ff)] px-4 py-4">
        <CardTitle className="flex items-center gap-3 text-base font-semibold text-slate-900">
          <span className="flex h-10 w-10 items-center justify-center rounded-2xl bg-[rgba(237,232,255,.95)] text-violet-700 shadow-sm">
            <Sparkles className="h-5 w-5" />
          </span>
          Skill Trace
        </CardTitle>
      </CardHeader>
      <CardContent className="max-h-[360px] space-y-5 overflow-y-auto p-4 custom-scrollbar">
        <section className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">已注册 Skills</div>
          {skills.length === 0 ? (
            <p className="text-sm leading-6 text-muted-foreground">这次会话暂未注册任何技能。</p>
          ) : (
            skills.map((skill) => <SkillCatalogCard key={skill.id} skill={skill} />)
          )}
        </section>

        <section className="space-y-3">
          <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">调用记录</div>
          {skillInvocations.length === 0 ? (
            <p className="text-sm leading-6 text-muted-foreground">这次会话还没有记录 skill 调用。</p>
          ) : (
            skillInvocations.map((invocation) => <SkillInvocationCard key={invocation.id} invocation={invocation} />)
          )}
        </section>
      </CardContent>
    </Card>
  );
}
