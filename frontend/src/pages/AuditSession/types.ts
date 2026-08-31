import type {
  AuditSessionDetail,
  AuditSessionHandoff,
  AuditSessionMemory,
  AuditSessionMessage,
  AuditSessionSkill,
  AuditSessionSkillInvocation,
  AuditSessionToolCall,
} from "@/shared/api/auditSessions";

export type {
  AuditSessionDetail,
  AuditSessionHandoff,
  AuditSessionMemory,
  AuditSessionMessage,
  AuditSessionSkill,
  AuditSessionSkillInvocation,
  AuditSessionToolCall,
};

export interface AuditSessionViewState {
  session: AuditSessionDetail | null;
  messages: AuditSessionMessage[];
  toolCalls: AuditSessionToolCall[];
  skills: AuditSessionSkill[];
  skillInvocations: AuditSessionSkillInvocation[];
  memories: AuditSessionMemory[];
  handoffs: AuditSessionHandoff[];
  loading: boolean;
  error: string | null;
}