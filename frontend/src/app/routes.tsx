import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';

import Account from '@/pages/Account';
import AdminDashboard from '@/pages/AdminDashboard';
import AgentAudit from '@/pages/AgentAudit';
import AuditSession from '@/pages/AuditSession';
import AuditTasks from '@/pages/AuditTasks';
import Dashboard from '@/pages/Dashboard';
import ProjectDetail from '@/pages/ProjectDetail';
import Projects from '@/pages/Projects';
import ReportTemplatesPage from '@/pages/ReportTemplatesPage';
import SkillsManager from '@/pages/SkillsManager';

export interface RouteConfig {
  name: string;
  labelKey: string;
  path: string;
  element: ReactNode;
  visible?: boolean;
}

const routes: RouteConfig[] = [
  // '/' 仅作登录后/直接访问时的落地重定向, 直落仪表盘; 侧栏不再展示首页 tab
  { name: '首页', labelKey: "routes.home", path: '/', element: <Navigate to="/dashboard" replace />, visible: false },
  { name: 'Agent审计详情', labelKey: "routes.agentAuditDetail", path: '/agent-audit/:taskId', element: <AgentAudit />, visible: false },
  { name: '审计会话', labelKey: "routes.auditSession", path: '/audit-sessions/:sessionId', element: <AuditSession />, visible: false },
  { name: '仪表盘', labelKey: "routes.dashboard", path: '/dashboard', element: <Dashboard />, visible: true },
  { name: '项目管理', labelKey: "routes.projects", path: '/projects', element: <Projects />, visible: true },
  { name: '项目详情', labelKey: "routes.projectDetail", path: '/projects/:id', element: <ProjectDetail />, visible: false },
  { name: '审计任务', labelKey: "routes.auditTasks", path: '/audit-tasks', element: <AuditTasks />, visible: true },
  { name: 'Skills管理', labelKey: "routes.skills", path: '/skills', element: <SkillsManager />, visible: true },
  { name: '报告模板', labelKey: "routes.reportTemplates", path: '/report-templates', element: <ReportTemplatesPage />, visible: false },
  { name: '系统设置', labelKey: "routes.settings", path: '/admin', element: <AdminDashboard />, visible: true },
  { name: '账号管理', labelKey: "routes.account", path: '/account', element: <Account />, visible: false },
];

export default routes;
