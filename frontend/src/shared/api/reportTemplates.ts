import { apiClient } from './serverClient';

export interface ReportTemplate {
  id: string;
  slug: string;
  name: string;
  description?: string;
  report_type: string;
  output_format: string;
  content: string;
  variables: Record<string, unknown>;
  metadata_json: Record<string, unknown>;
  is_default: boolean;
  is_system: boolean;
  is_active: boolean;
  sort_order: number;
  folder_path?: string;
  template_file?: string;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface ReportTemplateListResponse {
  items: ReportTemplate[];
  total: number;
}

export interface ReportTemplatePayload {
  name: string;
  slug?: string;
  description?: string;
  report_type?: string;
  output_format?: string;
  content: string;
  variables?: Record<string, unknown>;
  metadata_json?: Record<string, unknown>;
  is_active?: boolean;
  sort_order?: number;
  is_default?: boolean;
  is_system?: boolean;
}

export async function getReportTemplates(): Promise<ReportTemplateListResponse> {
  const response = await apiClient.get('/report-templates');
  return response.data;
}

export async function createReportTemplate(data: ReportTemplatePayload): Promise<ReportTemplate> {
  const response = await apiClient.post('/report-templates', data);
  return response.data;
}

export async function updateReportTemplate(id: string, data: Partial<ReportTemplatePayload>): Promise<ReportTemplate> {
  const response = await apiClient.put(`/report-templates/${id}`, data);
  return response.data;
}

export async function deleteReportTemplate(id: string): Promise<void> {
  await apiClient.delete(`/report-templates/${id}`);
}

export async function resyncReportTemplates(): Promise<ReportTemplateListResponse> {
  const response = await apiClient.post('/report-templates/resync');
  return response.data;
}

