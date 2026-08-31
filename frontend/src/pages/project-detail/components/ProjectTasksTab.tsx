import { Link } from "react-router-dom";
import { FileText, Play, Activity } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import type { AuditTask } from "@/shared/types";
import type { UnifiedTask } from "@/shared/types";

export function ProjectTasksTab(props: {
  unifiedTasks: UnifiedTask[];
  onCreateTask: () => void;
  formatDate: (dateString: string) => string;
  renderStatusBadge: (status: string) => React.ReactNode;
  renderStatusIcon: (status: string) => React.ReactNode;
}) {
  const { unifiedTasks, onCreateTask, formatDate, renderStatusBadge, renderStatusIcon } = props;

  return (
    <>
      <div className="flex items-center justify-between">
        <div className="section-header mb-0 pb-0 border-0">
          <FileText className="w-5 h-5 text-primary" />
          <h3 className="section-title">审计任务列表</h3>
        </div>
        <Button onClick={onCreateTask} className="cyber-btn-primary">
          <Play className="w-4 h-4 mr-2" />
          新建任务
        </Button>
      </div>

      {unifiedTasks.length > 0 ? (
        <div className="space-y-4">
          {unifiedTasks.map((wrappedTask) => {
            const isAuditTask = wrappedTask.kind === "audit";
            const task: any = wrappedTask.task as any;

            const issueCount = isAuditTask ? (task.issues_count ?? 0) : (task.findings_count ?? 0);
            const totalFiles = task.total_files ?? 0;
            const totalLines = task.total_lines ?? "-";
            const qualityScore = typeof task.quality_score === "number" ? task.quality_score : 0;

            return (
              <div key={`${wrappedTask.kind}:${task.id}`} className="cyber-card p-6">
                <div className="flex items-center justify-between mb-4 pb-4 border-b border-border">
                  <div className="flex items-center space-x-3">
                    <div
                      className={`w-10 h-10 rounded-lg flex items-center justify-center ${task.status === "completed"
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
                        {isAuditTask
                          ? ((task as AuditTask).task_type === "repository" ? "审计任务" : "即时分析任务")
                          : "Agent 审计任务"}
                      </h4>
                      <p className="text-sm text-muted-foreground font-mono">创建于 {formatDate(task.created_at)}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge className={wrappedTask.kind === "agent" ? "cyber-badge-info" : "cyber-badge-muted"}>
                      {wrappedTask.kind === "agent" ? "AGENT" : "AUDIT"}
                    </Badge>
                    {renderStatusBadge(task.status)}
                  </div>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4 font-mono">
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-foreground">{totalFiles}</p>
                    <p className="text-xs text-muted-foreground uppercase">总文件数</p>
                  </div>
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-foreground">{totalLines}</p>
                    <p className="text-xs text-muted-foreground uppercase">代码行数</p>
                  </div>
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-amber-400">{issueCount}</p>
                    <p className="text-xs text-muted-foreground uppercase">{isAuditTask ? "发现问题" : "发现漏洞"}</p>
                  </div>
                  <div className="text-center p-3 bg-muted rounded-lg border border-border">
                    <p className="text-2xl font-bold text-primary">{qualityScore.toFixed(1)}</p>
                    <p className="text-xs text-muted-foreground uppercase">质量评分</p>
                  </div>
                </div>

                {task.status === "completed" && typeof qualityScore === "number" && (
                  <div className="space-y-2 mb-4">
                    <div className="flex items-center justify-between text-sm font-mono">
                      <span className="text-muted-foreground">质量评分</span>
                      <span className="text-foreground font-bold">{qualityScore.toFixed(1)}/100</span>
                    </div>
                    <Progress value={qualityScore} className="h-2 bg-muted [&>div]:bg-primary" />
                  </div>
                )}

                <div className="flex justify-end space-x-2 pt-4 border-t border-border">
                  <Link to={isAuditTask ? `/tasks/${task.id}` : `/agent-audit/${task.id}`}>
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
          <h3 className="text-lg font-bold text-foreground mb-2 uppercase">暂无审计任务</h3>
          <p className="text-sm text-muted-foreground mb-6 font-mono">创建第一个审计任务开始代码质量分析</p>
          <Button onClick={onCreateTask} className="cyber-btn-primary">
            <Play className="w-4 h-4 mr-2" />
            创建任务
          </Button>
        </div>
      )}
    </>
  );
}


