/**
 * ZIP文件存储工具
 * 通过后端API管理项目的ZIP文件
 */

import { apiClient } from '@/shared/api/serverClient';

export interface ZipFileMeta {
  has_file: boolean;
  original_filename?: string;
  file_size?: number;
  uploaded_at?: string;
  has_persistent_source?: boolean;
  persistent_source_path?: string;
  persistent_source_updated_at?: string;
  import_status?: 'processing' | 'ready' | 'error' | string;
  import_error?: string | null;
  import_started_at?: string;
  import_completed_at?: string;
}

/**
 * 获取项目ZIP文件信息
 */
export async function getZipFileInfo(projectId: string): Promise<ZipFileMeta> {
  try {
    const response = await apiClient.get(`/projects/${projectId}/zip`);
    return response.data;
  } catch (error) {
    console.error('获取ZIP文件信息失败:', error);
    return { has_file: false };
  }
}

/**
 * 上传项目ZIP文件
 */
export async function uploadZipFile(projectId: string, file: File, options?: {
  keepArchive?: boolean;
  onUploadProgress?: (progress: number) => void;
}): Promise<{
  success: boolean;
  message?: string;
  original_filename?: string;
  file_size?: number;
  has_file?: boolean;
  has_persistent_source?: boolean;
  persistent_source_path?: string;
  persistent_source_updated_at?: string;
  import_status?: string;
  import_error?: string | null;
}> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('keep_archive', String(options?.keepArchive ?? true));

  try {
    const response = await apiClient.post(`/projects/${projectId}/zip`, formData, {
      headers: {
        'Content-Type': 'multipart/form-data',
      },
      onUploadProgress: (event) => {
        if (!options?.onUploadProgress || !event.total) return;
        options.onUploadProgress(Math.round((event.loaded / event.total) * 100));
      },
    });
    return {
      success: true,
      message: response.data.message,
      original_filename: response.data.original_filename,
      file_size: response.data.file_size,
      has_file: response.data.has_file,
      has_persistent_source: response.data.has_persistent_source,
      persistent_source_path: response.data.persistent_source_path,
      persistent_source_updated_at: response.data.persistent_source_updated_at,
      import_status: response.data.import_status,
      import_error: response.data.import_error,
    };
  } catch (error: any) {
    console.error('上传ZIP文件失败:', error);
    return {
      success: false,
      message: error.response?.data?.detail || '上传失败',
    };
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

export async function waitForZipImport(
  projectId: string,
  options?: {
    intervalMs?: number;
    timeoutMs?: number;
    onStatus?: (info: ZipFileMeta) => void;
  }
): Promise<ZipFileMeta> {
  const intervalMs = options?.intervalMs ?? 1200;
  const timeoutMs = options?.timeoutMs ?? 10 * 60 * 1000;
  const startedAt = Date.now();

  while (Date.now() - startedAt < timeoutMs) {
    const info = await getZipFileInfo(projectId);
    options?.onStatus?.(info);

    if (info.import_status === 'error') {
      throw new Error(info.import_error || 'ZIP source import failed');
    }
    if (info.import_status === 'processing') {
      await sleep(intervalMs);
      continue;
    }
    if (info.import_status === 'ready' || info.has_persistent_source) {
      return info;
    }

    await sleep(intervalMs);
  }

  throw new Error('ZIP source import timed out');
}

/**
 * 删除项目ZIP文件
 */
export async function deleteZipFile(projectId: string): Promise<boolean> {
  try {
    await apiClient.delete(`/projects/${projectId}/zip`);
    return true;
  } catch (error) {
    console.error('删除ZIP文件失败:', error);
    return false;
  }
}

export async function deleteProjectSourceArtifacts(
  projectId: string,
  payload: {
    deleteZip?: boolean;
    deletePersistentSource?: boolean;
  },
): Promise<{
  deleted_zip: boolean;
  deleted_persistent_source: boolean;
}> {
  const response = await apiClient.post(`/projects/${projectId}/source-artifacts/delete`, {
    delete_zip: Boolean(payload.deleteZip),
    delete_persistent_source: Boolean(payload.deletePersistentSource),
  });
  return response.data;
}

/**
 * 检查项目是否有ZIP文件
 */
export async function hasZipFile(projectId: string): Promise<boolean> {
  const info = await getZipFileInfo(projectId);
  return info.has_file;
}

/**
 * 格式化文件大小
 */
export function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  } else if (bytes >= 1024) {
    return `${(bytes / 1024).toFixed(2)} KB`;
  }
  return `${bytes} B`;
}


