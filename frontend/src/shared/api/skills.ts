import { apiClient } from './serverClient';

export interface AgentSkillBinding {
  id: string;
  skill_id: string;
  agent_type: string;
  enabled: boolean;
  always_include: boolean;
  sort_order: number;
  match_keywords: string[];
  match_config: Record<string, unknown>;
  bindings_file?: string;
  skill_file?: string;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface SkillMetadata {
  id: string;
  name: string;
  slug: string;
  description: string;
  tags: string[];
  source_type: string;
  source_url?: string;
  metadata_json: Record<string, unknown>;
  is_system: boolean;
  is_active: boolean;
  bindings: AgentSkillBinding[];
  folder_path?: string;
  skill_file?: string;
  bindings_file?: string;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface Skill extends SkillMetadata {
  content?: string;
  extension_manifest: Array<Record<string, unknown>>;
  extension_payload: Record<string, unknown>;
}

export interface SkillListResponse {
  items: SkillMetadata[];
  total: number;
}

export interface SkillPayload {
  name: string;
  slug: string;
  description: string;
  source_type?: string;
  source_url?: string;
  content?: string;
  metadata_json?: Record<string, unknown>;
  tags?: string[];
  extension_manifest?: Array<Record<string, unknown>>;
  extension_payload?: Record<string, unknown>;
  is_active?: boolean;
  bindings?: Array<{
    agent_type: string;
    enabled?: boolean;
    always_include?: boolean;
    sort_order?: number;
    match_keywords?: string[];
    match_config?: Record<string, unknown>;
  }>;
}

export interface SkillImportPayload {
  repo_url: string;
  agent_type?: string;
  bind_to_agent?: boolean;
  enabled?: boolean;
  always_include?: boolean;
  match_keywords?: string[];
}

export async function getSkills(agentType?: string): Promise<SkillListResponse> {
  const response = await apiClient.get('/skills', { params: agentType ? { agent_type: agentType } : undefined });
  return response.data;
}

export async function getSkill(id: string): Promise<Skill> {
  const response = await apiClient.get(`/skills/${id}`);
  return response.data;
}

export async function createSkill(data: SkillPayload): Promise<Skill> {
  const response = await apiClient.post('/skills', data);
  return response.data;
}

export async function updateSkill(id: string, data: Partial<SkillPayload>): Promise<Skill> {
  const response = await apiClient.put(`/skills/${id}`, data);
  return response.data;
}

export async function deleteSkill(id: string): Promise<void> {
  await apiClient.delete(`/skills/${id}`);
}

export async function importGithubSkill(data: SkillImportPayload): Promise<Skill> {
  const response = await apiClient.post('/skills/import-github', data);
  return response.data;
}

export async function uploadSkillZip(file: File): Promise<Skill> {
  const formData = new FormData();
  formData.append('file', file);
  const response = await apiClient.post('/skills/upload-zip', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return response.data;
}

export async function createSkillBinding(skillId: string, data: Omit<AgentSkillBinding, 'id' | 'skill_id'>): Promise<AgentSkillBinding> {
  const response = await apiClient.post(`/skills/${skillId}/bindings`, data);
  return response.data;
}

export async function updateSkillBinding(skillId: string, bindingId: string, data: Partial<AgentSkillBinding>): Promise<AgentSkillBinding> {
  const response = await apiClient.put(`/skills/${skillId}/bindings/${bindingId}`, data);
  return response.data;
}

export async function deleteSkillBinding(skillId: string, bindingId: string): Promise<void> {
  await apiClient.delete(`/skills/${skillId}/bindings/${bindingId}`);
}

export async function resyncSkills(): Promise<SkillListResponse> {
  const response = await apiClient.post('/skills/resync');
  return response.data;
}

