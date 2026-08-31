import { apiClient } from './serverClient';

export interface VulnerabilityReport {
  id: string;
  vulnerability_id: string;
  report_kind: 'en' | 'zh' | 'cve' | string;
  markdown_content: string;
  generation_status: string;
  source_type: string;
  template_key: string;
  template_version: string;
  template_snapshot?: string | null;
  last_generated_at?: string | null;
  last_edited_at?: string | null;
  created_at: string;
  updated_at?: string | null;
}

export interface ManagedVulnerability {
  id: string;
  project_id: string;
  task_id: string;
  finding_id: string;
  project_name: string;
  version_label: string;
  version_tag?: string | null;
  branch_name?: string | null;
  commit_sha?: string | null;
  repository_url_snapshot?: string | null;
  vulnerability_name: string;
  vulnerability_type?: string | null;
  severity: string;
  file_path?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  human_review_result: string;
  cve_request_status: string;
  cve_failure_reason?: string | null;
  cve_id?: string | null;
  report_generation_status: string;
  source_finding_fingerprint?: string | null;
  source_metadata?: Record<string, unknown> | null;
  reports?: VulnerabilityReport[];
  created_at: string;
  updated_at?: string | null;
}

export interface VulnerabilityQuery {
  project_name?: string;
  version_label?: string;
  project_link?: string;
  vulnerability_name?: string;
  vulnerability_type?: string;
  human_review_result?: string;
  cve_request_status?: string;
  cve_id?: string;
  skip?: number;
  limit?: number;
}

export interface VulnerabilityUpdatePayload {
  vulnerability_name?: string;
  vulnerability_type?: string;
  severity?: string;
  human_review_result?: string;
  cve_request_status?: string;
  cve_failure_reason?: string | null;
  cve_id?: string | null;
}

export interface VulnerabilityReportUpdatePayload {
  markdown_content: string;
  source_type?: string;
}

export async function listVulnerabilities(params?: VulnerabilityQuery): Promise<ManagedVulnerability[]> {
  const response = await apiClient.get('/vulnerabilities', { params });
  return response.data;
}

export async function getVulnerability(vulnerabilityId: string): Promise<ManagedVulnerability> {
  const response = await apiClient.get(`/vulnerabilities/${vulnerabilityId}`);
  return response.data;
}

export async function updateVulnerability(
  vulnerabilityId: string,
  payload: VulnerabilityUpdatePayload
): Promise<ManagedVulnerability> {
  const response = await apiClient.patch(`/vulnerabilities/${vulnerabilityId}`, payload);
  return response.data;
}

export async function updateVulnerabilityReport(
  vulnerabilityId: string,
  reportKind: string,
  payload: VulnerabilityReportUpdatePayload
): Promise<VulnerabilityReport> {
  const response = await apiClient.patch(
    `/vulnerabilities/${vulnerabilityId}/reports/${reportKind}`,
    payload
  );
  return response.data;
}

export async function deleteVulnerability(vulnerabilityId: string): Promise<void> {
  await apiClient.delete(`/vulnerabilities/${vulnerabilityId}`);
}

export async function exportVulnerabilityReport(vulnerabilityId: string, reportKind: string): Promise<string> {
  const response = await apiClient.get(`/vulnerabilities/${vulnerabilityId}/reports/${reportKind}/export`, {
    responseType: 'text',
  });
  return response.data;
}
