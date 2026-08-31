import { Activity, AlertTriangle, CheckCircle, Code } from "lucide-react";

export type ProjectCombinedStats = {
  totalTasks: number;
  completedTasks: number;
  totalIssues: number;
  avgQualityScore: number;
};

export function ProjectStatsCards(props: { stats: ProjectCombinedStats }) {
  const { stats } = props;
  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-4 relative z-10">
      <div className="cyber-card p-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="stat-label">审计任务</p>
            <p className="stat-value">{stats.totalTasks}</p>
          </div>
          <div className="stat-icon text-sky-400">
            <Activity className="w-6 h-6" />
          </div>
        </div>
      </div>

      <div className="cyber-card p-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="stat-label">已完成</p>
            <p className="stat-value">{stats.completedTasks}</p>
          </div>
          <div className="stat-icon text-emerald-400">
            <CheckCircle className="w-6 h-6" />
          </div>
        </div>
      </div>

      <div className="cyber-card p-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="stat-label">发现问题</p>
            <p className="stat-value">{stats.totalIssues}</p>
          </div>
          <div className="stat-icon text-amber-400">
            <AlertTriangle className="w-6 h-6" />
          </div>
        </div>
      </div>

      <div className="cyber-card p-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="stat-label">平均质量分</p>
            <p className="stat-value">{stats.avgQualityScore.toFixed(1)}</p>
          </div>
          <div className="stat-icon text-violet-400">
            <Code className="w-6 h-6" />
          </div>
        </div>
      </div>
    </div>
  );
}


