import type { Project, ProjectSourceType } from '@/shared/types';
import { REPOSITORY_PLATFORM_LABELS } from '@/shared/constants/projectTypes';

export function isRepositoryProject(project: Project): boolean {
  return project.source_type === 'repository';
}

export function isZipProject(project: Project): boolean {
  return project.source_type === 'zip';
}

export function isLocalDirectoryProject(project: Project): boolean {
  return project.source_type === 'local_directory';
}

export function getSourceTypeLabel(sourceType: ProjectSourceType): string {
  const labels: Record<ProjectSourceType, string> = {
    repository: '远程仓库',
    zip: 'ZIP上传',
    local_directory: '本地目录',
  };
  return labels[sourceType] || '未知';
}

export function getSourceTypeBadge(sourceType: ProjectSourceType): string {
  const badges: Record<ProjectSourceType, string> = {
    repository: 'REPO',
    zip: 'ZIP',
    local_directory: 'LOCAL',
  };
  return badges[sourceType] || 'UNKNOWN';
}

export function getRepositoryPlatformLabel(platform?: string): string {
  return REPOSITORY_PLATFORM_LABELS[platform as keyof typeof REPOSITORY_PLATFORM_LABELS] || REPOSITORY_PLATFORM_LABELS.other;
}

export function canSelectBranch(project: Project): boolean {
  return isRepositoryProject(project) && !!project.repository_url;
}

export function requiresZipUpload(project: Project): boolean {
  return isZipProject(project);
}

export function getScanMethodDescription(project: Project): string {
  if (isRepositoryProject(project)) {
    return `从 ${getRepositoryPlatformLabel(project.repository_type)} 仓库拉取代码`;
  }
  if (isLocalDirectoryProject(project)) {
    return '使用受管 projects 目录中的本地代码';
  }
  return '上传 ZIP 文件进行扫描';
}

export function validateProjectConfig(project: Project): { valid: boolean; errors: string[] } {
  const errors: string[] = [];

  if (!project.name?.trim()) {
    errors.push('项目名称不能为空');
  }

  if (isRepositoryProject(project) && !project.repository_url?.trim()) {
    errors.push('仓库地址不能为空');
  }

  if (isLocalDirectoryProject(project) && !project.local_path?.trim()) {
    errors.push('本地目录路径不能为空');
  }

  return {
    valid: errors.length === 0,
    errors,
  };
}
