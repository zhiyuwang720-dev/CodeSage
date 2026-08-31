import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, Loader2, SendHorizonal } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { AuditSessionMessageMode } from "@/shared/api/auditSessions";
import { getSkills, type SkillMetadata } from "@/shared/api/skills";

export function FollowUpComposer({
  disabled,
  onSubmit,
}: {
  disabled?: boolean;
  onSubmit: (content: string, mode: AuditSessionMessageMode, selectedSkillRefs?: string[]) => Promise<void>;
}) {
  const [content, setContent] = useState("");
  const [submittingMode, setSubmittingMode] = useState<AuditSessionMessageMode | null>(null);
  const [skills, setSkills] = useState<SkillMetadata[]>([]);
  const [caret, setCaret] = useState(0);
  const [highlightedSkillIndex, setHighlightedSkillIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSkills()
      .then((result) => {
        if (!cancelled) {
          setSkills(result.items.filter((skill) => skill.is_active));
        }
      })
      .catch((error) => console.error("[FollowUpComposer] failed to load skills", error));
    return () => {
      cancelled = true;
    };
  }, []);

  const activeSkillToken = useMemo(() => {
    const prefix = content.slice(0, caret);
    const match = prefix.match(/(^|\s)\$([A-Za-z0-9_.:-]*)$/);
    if (!match) {
      return null;
    }
    return {
      start: prefix.length - (match[2]?.length || 0) - 1,
      query: (match[2] || "").toLowerCase(),
    };
  }, [content, caret]);

  const filteredSkills = useMemo(() => {
    if (!activeSkillToken) {
      return [];
    }
    const query = activeSkillToken.query;
    return skills
      .filter((skill) => {
        const haystack = `${skill.name} ${skill.slug} ${skill.description || ""}`.toLowerCase();
        return !query || haystack.includes(query);
      })
      .slice(0, 8);
  }, [activeSkillToken, skills]);

  const selectedSkillRefs = useMemo(() => {
    const refs = new Set<string>();
    const available = new Set(skills.map((skill) => skill.slug));
    for (const match of content.matchAll(/(?<![\w$])\$([A-Za-z0-9][A-Za-z0-9_.:-]*)/g)) {
      const ref = match[1];
      if (available.has(ref)) {
        refs.add(ref);
      }
    }
    return [...refs];
  }, [content, skills]);

  const chooseSkill = useCallback((skill: SkillMetadata) => {
    if (!activeSkillToken) {
      return;
    }
    const before = content.slice(0, activeSkillToken.start);
    const after = content.slice(caret);
    const insertion = `$${skill.slug}`;
    const needsSpace = after.length > 0 && !/^\s/.test(after);
    const nextContent = `${before}${insertion}${needsSpace ? " " : ""}${after}`;
    const nextCaret = before.length + insertion.length + (needsSpace ? 1 : 0);
    setContent(nextContent);
    setHighlightedSkillIndex(0);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(nextCaret, nextCaret);
      setCaret(nextCaret);
    });
  }, [activeSkillToken, caret, content]);

  const submitContent = useCallback(async (mode: AuditSessionMessageMode) => {
    const trimmed = content.trim();
    if (!trimmed || disabled || submittingMode) {
      return;
    }
    setSubmittingMode(mode);
    try {
      await onSubmit(trimmed, mode, selectedSkillRefs);
      setContent("");
    } catch (error) {
      console.error("[FollowUpComposer] submit failed", error);
    } finally {
      setSubmittingMode(null);
    }
  }, [content, disabled, onSubmit, selectedSkillRefs, submittingMode]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await submitContent("chat");
  }

  async function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (filteredSkills.length > 0) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setHighlightedSkillIndex((index) => (index + 1) % filteredSkills.length);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setHighlightedSkillIndex((index) => (index - 1 + filteredSkills.length) % filteredSkills.length);
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chooseSkill(filteredSkills[highlightedSkillIndex] || filteredSkills[0]);
        return;
      }
      if (event.key === "Escape") {
        setCaret(0);
        return;
      }
    }
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    await submitContent("chat");
  }

  const isSubmitting = submittingMode !== null;

  return (
    <form className="space-y-3" onSubmit={handleSubmit}>
      <div className="rounded-[28px] border border-[#dce8e1] bg-white p-3 shadow-[0_20px_60px_rgba(86,105,97,0.08)]">
        <div className="relative">
          {filteredSkills.length > 0 ? (
            <div className="absolute bottom-full left-0 right-0 z-20 mb-2 max-h-64 overflow-auto rounded-2xl border border-[rgba(154,180,163,.35)] bg-white/95 p-2 shadow-[0_18px_45px_rgba(65,86,70,.18)] backdrop-blur">
              {filteredSkills.map((skill, index) => (
                <button
                  key={skill.id}
                  type="button"
                  onMouseDown={(event) => {
                    event.preventDefault();
                    chooseSkill(skill);
                  }}
                  className={`flex w-full items-start gap-3 rounded-xl px-3 py-2 text-left transition ${
                    index === highlightedSkillIndex ? "bg-[rgba(137,169,141,.16)]" : "hover:bg-[rgba(137,169,141,.1)]"
                  }`}
                >
                  <Box className="mt-0.5 h-4 w-4 shrink-0 text-[#6fa27b]" />
                  <span className="min-w-0">
                    <span className="block text-sm font-semibold text-slate-800">{skill.name}</span>
                    <span className="block truncate text-xs text-muted-foreground">{skill.description || skill.slug}</span>
                  </span>
                </button>
              ))}
            </div>
          ) : null}
          <Textarea
            ref={textareaRef}
            className="min-h-[118px] resize-none rounded-[22px] border border-[#e2eae5] bg-[#fbfdfb] px-4 py-3 text-[15px] leading-7 shadow-none focus-visible:border-[#9fc4a7] focus-visible:ring-0"
          placeholder=""
          value={content}
          onChange={(event) => {
            setContent(event.target.value);
            setCaret(event.target.selectionStart || 0);
          }}
          onClick={(event) => setCaret(event.currentTarget.selectionStart || 0)}
          onKeyUp={(event) => setCaret(event.currentTarget.selectionStart || 0)}
          onKeyDown={(event) => void handleKeyDown(event)}
          disabled={disabled || isSubmitting}
        />
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-[#e6ede8] px-2 pt-3 text-xs text-muted-foreground">
          <span>按 Enter 发送，Shift + Enter 换行。</span>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              type="submit"
              disabled={disabled || isSubmitting || !content.trim()}
              className="h-11 rounded-full bg-[linear-gradient(135deg,#7fa48a,#5f8069)] px-5 text-white shadow-[0_16px_35px_rgba(95,128,105,.22)] hover:opacity-95"
            >
              {submittingMode === "chat" ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <SendHorizonal className="mr-2 h-4 w-4" />}
              {submittingMode === "chat" ? "发送中..." : "发送消息"}
            </Button>
          </div>
        </div>
      </div>
    </form>
  );
}
