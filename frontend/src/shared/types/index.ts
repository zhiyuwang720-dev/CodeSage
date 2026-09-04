// 通用选项接口
export interface Option {
  label: string;
  value: string;
  icon?: React.ComponentType<{ className?: string }>;
  withCount?: boolean;
}

// 用户相关类型
export interface Profile {
  id: string;
  phone?: string;
  email?: string;
  full_name?: string;
  avatar_url?: string;
  role: 'admin' | 'member';
  github_username?: string;
  gitlab_username?: string;
  created_at: string;
  updated_at: string;
}

// 项目来源类型
export type ProjectSourceType = 'repository' | 'zip' | 'local_directory' | 'pr_review';

// 仓库平台类型
export type RepositoryPlatform = 'github' | 'gitlab' | 'gitea' | 'other';

// 项目相关类型
export interface Project {
  id: string;
  name: string;
  description?: string;
  source_type: ProjectSourceType;  // 项目来源: 'repository' (远程仓库) 或 'zip' (ZIP上传)
  repository_url?: string;         // 仅 source_type='repository' 时有效
  repository_type?: RepositoryPlatform;  // 仓库平台: github, gitlab, other
  local_path?: string;
  workspace_mode?: string;
  default_branch: string;
  programming_languages: string;
  owner_id: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  owner?: Profile;
}

export interface ProjectMember {
  id: string;
  project_id: string;
  user_id: string;
  role: 'owner' | 'admin' | 'member' | 'viewer';
  permissions: string;
  joined_at: string;
  created_at: string;
  user?: Profile;
  project?: Project;
}

// 审计相关类型
// 产品收敛为 PR review(agent 任务)单类型;legacy AuditTask/AuditIssue/InstantAnalysis 已整体下架,
// 由 shared/api/agentTasks 的 AgentTask/AgentFinding 承接。

// ProjectDetail 页面：前端聚合层类型(把 AgentTask 的结果统一展示)
export type AggregatedAgentFinding = import("@/shared/api/agentTasks").AgentFinding & {
  task_created_at?: string;
  task_completed_at?: string | null;
};

export type IssuesSummary = {
  completedAgentTasksCount: number;
  fetchedAgentTasksCount: number;
  isLimited: boolean;
  maxTasks: number;
};

export type LatestProblem = {
  id: string;
  task_id: string;
  task_created_at?: string;
  created_at: string;
  severity: "critical" | "high" | "medium" | "low";
  title: string;
  description?: string | null;
  file_path?: string | null;
  line_number?: number | null;
  line_end?: number | null;
  category?: string | null;
  status?: string;
};

// 表单相关类型
export interface CreateProjectForm {
  name: string;
  description?: string;
  source_type?: ProjectSourceType;  // 项目来源类型
  repository_url?: string;          // 仅 source_type='repository' 时需要
  repository_type?: RepositoryPlatform;  // 仓库平台
  local_path?: string;
  workspace_mode?: string;
  default_branch?: string;
  programming_languages: string[];
}

export interface ManagedLocalDirectory {
  name: string;
  path: string;
}

export interface ProjectFileContent {
  path: string;
  content: string;
  size: number;
  truncated: boolean;
}

// 统计相关类型
export interface ProjectStats {
  total_projects: number;
  active_projects: number;
  total_tasks: number;
  completed_tasks: number;
  total_issues: number;
  resolved_issues: number;
  avg_quality_score: number;
  // PR 审查发现(PR 问题)严重度分布: 仅统计 task_type=pr_review 任务的 AgentFinding
  pr_issues_total: number;
  pr_issues_critical: number;
  pr_issues_high: number;
  pr_issues_medium: number;
  pr_issues_low: number;
}

export interface IssueStats {
  by_type: Record<string, number>;
  by_severity: Record<string, number>;
  by_status: Record<string, number>;
  trend_data: Array<{
    date: string;
    count: number;
  }>;
}

// API响应类型
export interface ApiResponse<T> {
  data: T;
  message?: string;
  success: boolean;
}

export interface PaginatedResponse<T> {
  data: T[];
  total: number;
  page: number;
  limit: number;
  has_more: boolean;
}

// 代码分析结果类型
export interface CodeAnalysisResult {
  issues: Array<{
    type: string;
    severity: string;
    title: string;
    description: string;
    suggestion: string;
    line: number;
    column?: number;
    code_snippet: string;
    ai_explanation: string;
    xai?: {
      what: string;
      why: string;
      how: string;
      learn_more?: string;
    };
  }>;
  quality_score: number;
  summary: {
    total_issues: number;
    critical_issues: number;
    high_issues: number;
    medium_issues: number;
    low_issues: number;
  };
  metrics: {
    complexity: number;
    maintainability: number;
    security: number;
    performance: number;
  };
  // 后端返回的额外字段
  analysis_id?: string;
  analysis_time?: number;
}

// GitHub/GitLab集成类型
export interface Repository {
  id: string;
  name: string;
  full_name: string;
  description?: string;
  html_url: string;
  clone_url: string;
  default_branch: string;
  language?: string;
  languages?: Record<string, number>;
  private: boolean;
  updated_at: string;
}

export interface Branch {
  name: string;
  commit: {
    sha: string;
    url: string;
  };
  protected: boolean;
}

// 通知类型
export interface Notification {
  id: string;
  type: 'task_completed' | 'task_failed' | 'new_issue' | 'issue_resolved';
  title: string;
  message: string;
  data?: any;
  read: boolean;
  created_at: string;
}

// 系统配置类型
export interface SystemConfig {
  max_file_size: number;
  supported_languages: string[];
  analysis_timeout: number;
  max_concurrent_tasks: number;
  notification_settings: {
    email_enabled: boolean;
    webhook_url?: string;
  };
}
