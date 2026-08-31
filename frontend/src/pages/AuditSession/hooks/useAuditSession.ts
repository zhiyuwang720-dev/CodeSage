import { useCallback, useEffect, useRef, useState } from "react";

import {
  getAuditSession,
  getAuditSessionHandoffs,
  getAuditSessionMemories,
  getAuditSessionMessages,
  getAuditSessionSkillInvocations,
  getAuditSessionSkills,
  getAuditSessionToolCalls,
} from "@/shared/api/auditSessions";
import type {
  AuditSessionDetail,
  AuditSessionHandoff,
  AuditSessionMemory,
  AuditSessionMessage,
  AuditSessionSkill,
  AuditSessionSkillInvocation,
  AuditSessionToolCall,
} from "@/pages/AuditSession/types";

export function useAuditSession(sessionId?: string) {
  const [session, setSession] = useState<AuditSessionDetail | null>(null);
  const [messages, setMessages] = useState<AuditSessionMessage[]>([]);
  const [toolCalls, setToolCalls] = useState<AuditSessionToolCall[]>([]);
  const [skills, setSkills] = useState<AuditSessionSkill[]>([]);
  const [skillInvocations, setSkillInvocations] = useState<AuditSessionSkillInvocation[]>([]);
  const [memories, setMemories] = useState<AuditSessionMemory[]>([]);
  const [handoffs, setHandoffs] = useState<AuditSessionHandoff[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const hasLoadedRef = useRef(false);

  const refresh = useCallback(async (options?: { silent?: boolean }) => {
    if (!sessionId) {
      setSession(null);
      setMessages([]);
      setToolCalls([]);
      setSkills([]);
      setSkillInvocations([]);
      setMemories([]);
      setHandoffs([]);
      setError(null);
      setLoading(false);
      return;
    }

    const isInitialLoad = !hasLoadedRef.current;
    const silent = options?.silent === true;
    if (isInitialLoad || !silent) {
      setLoading(true);
    }
    if (!silent || isInitialLoad) {
      setError(null);
    }

    try {
      const [detail, transcript, runtimeToolCalls, runtimeSkills, runtimeSkillInvocations, runtimeMemories, runtimeHandoffs] = await Promise.all([
        getAuditSession(sessionId),
        getAuditSessionMessages(sessionId),
        getAuditSessionToolCalls(sessionId),
        getAuditSessionSkills(sessionId),
        getAuditSessionSkillInvocations(sessionId),
        getAuditSessionMemories(sessionId),
        getAuditSessionHandoffs(sessionId),
      ]);
      setSession(detail);
      setMessages(transcript);
      setToolCalls(runtimeToolCalls);
      setSkills(runtimeSkills);
      setSkillInvocations(runtimeSkillInvocations);
      setMemories(runtimeMemories);
      setHandoffs(runtimeHandoffs);
      setError(null);
    } catch (err) {
      if (!silent || isInitialLoad) {
        setError(err instanceof Error ? err.message : "Failed to load audit session");
      } else {
        console.error("[AuditSession] Silent refresh failed:", err);
      }
    } finally {
      hasLoadedRef.current = true;
      if (isInitialLoad || !silent) {
        setLoading(false);
      }
    }
  }, [sessionId]);

  useEffect(() => {
    hasLoadedRef.current = false;
    void refresh();
  }, [refresh]);

  return {
    session,
    messages,
    toolCalls,
    skills,
    skillInvocations,
    memories,
    handoffs,
    loading,
    error,
    refresh,
    setMessages,
  };
}
