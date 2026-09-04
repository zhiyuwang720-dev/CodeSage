import { Link } from "react-router-dom";
import { FileText, Play, Activity } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import type { AgentTask } from "@/shared/api/agentTasks";

export function ProjectTasksTab(props: {
  tasks: AgentTask[];
  onCreateTask: () => void;
  formatDate: (dateString: string) => string;
  renderStatusBadge: (status: string) => React.ReactNode;
  renderStatusIcon: (status: string) => React.ReactNode;
}) {
  const { tasks, onCreateTask, formatDate, renderStatusBadge, renderStatusIcon } = props;

  return (
    <>
      <div className="flex items-center justify-between">
        <div className="section-header mb-0 pb-0 border-0">
          <FileText className="w-5 h-5 text-primary" />
          <h3 className="section-title">任务列表</h3>
        </div>
        <Button onClick={onCreateTask} className="cyber-btn-primary">
          <Play className="w-4 h-4 mr-2" />
          新建任务
        </Button>
      </div>

      {tasks.length > 0 ? (
        <div className="space-y-4">
          {tasks.map((task) => {
            const findingsCount = task.findings_count ?? 0;
            const totalFiles = task.total_files ?? 0;
            const analyzedFiles = task.analyzed_files ?? 0;
            const qualityScore = typeof task.quality_score === "number" ? task.quality_score : 0;
            const progress = task.progress_percentage ?? 0;

            return (
              <div key={task.id} className="cyber-card p-6">
                <div className="flex items-center justify-between mb-4 pb-4 border-b border-border">
                  <div className="flex items-center space-x-3">
                    <div
                      className={`w-10 h-10 rounded-lg flex items-center justify-center ${
                        task.status === "completed"
                          ? "bg-emerald-500/20"
                          : task.status === "running"
                            ? "bg-sky-500/20"
                            : task.status === "failed"
                              ? "bg-rose-500/20"
                              : "bg-muted"
                      }`}
                    >
                      {renderStatusIcon(task.status)}
                    </div>
                    <div>
                      <h4 className="font-bold text-foreground uppercase">
                        {task.name || "Agent 审计任务"}
                      </h4>
                      <p className="text-sm text-muted-foreground font-mono">创建于 {formatDate(task.created_at)}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge className="cyber-badge-info">AGENT</Badge>
                    {renderStatusBadge(task.status)}
                  </div>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4 font-mono">
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-foreground">{totalFiles}</p>
                    <p className="text-xs text-muted-foreground uppercase">总文件数</p>
                  </div>
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-foreground">{analyzedFiles}</p>
                    <p className="text-xs text-muted-foreground uppercase">已分析文件</p>
                  </div>
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-amber-400">{findingsCount}</p>
                    <p className="text-xs text-muted-foreground uppercase">漏洞发现</p>
                  </div>
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-primary">{qualityScore.toFixed(1)}</p>
                    <p className="text-xs text-muted-foreground uppercase">质量评分</p>
                  </div>
                </div>

                {task.status === "completed" && typeof qualityScore === "number" ? (
                  <div className="space-y-2 mb-4">
                    <div className="flex items-center justify-between text-sm font-mono">
                      <span className="text-muted-foreground">质量评分</span>
                      <span className="text-foreground font-bold">{qualityScore.toFixed(1)}/100</span>
                    </div>
                    <Progress value={qualityScore} className="h-2 bg-muted [&>div]:bg-primary" />
                  </div>
                ) : task.status === "running" || task.status === "pending" ? (
                  <div className="space-y-2 mb-4">
                    <div className="flex items-center justify-between text-sm font-mono">
                      <span className="text-muted-foreground">任务进度</span>
                      <span className="text-foreground font-bold">{progress.toFixed(0)}%</span>
                    </div>
                    <Progress value={progress} className="h-2 bg-muted [&>div]:bg-primary" />
                  </div>
                ) : null}

                <div className="flex justify-end space-x-2 pt-4 border-t border-border">
                  <Link to={`/agent-audit/${task.id}`}>
                    <Button variant="outline" size="sm" className="cyber-btn-outline">
                      <FileText className="w-4 h-4 mr-2" />
                      查看详情
                    </Button>
                  </Link>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="cyber-card p-12 text-center">
          <Activity className="w-16 h-16 text-muted-foreground mx-auto mb-4" />
          <h3 className="text-lg font-bold text-foreground mb-2 uppercase">暂无任务</h3>
          <p className="text-sm text-muted-foreground mb-6 font-mono">创建第一个审查任务开始代码分析</p>
          <Button onClick={onCreateTask} className="cyber-btn-primary">
            <Play className="w-4 h-4 mr-2" />
            创建任务
          </Button>
        </div>
      )}
    </>
  );
}
