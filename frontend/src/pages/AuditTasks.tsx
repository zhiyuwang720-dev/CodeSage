/**
 * Audit Tasks Page
 * Cyberpunk Terminal Aesthetic
 * 产品收敛为 PR review:仅展示 Agent 任务列表
 */

import { useState, useEffect } from "react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import {
	Activity,
	AlertTriangle,
	CheckCircle,
	Clock,
	Search,
	FileText,
	Calendar,
	XCircle,
	Terminal,
	Bot,
	Download,
	MessagesSquare,
} from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
	getAgentTasks,
	cancelAgentTask,
	getAgentFindings,
	type AgentTask,
	type AgentFinding,
} from "@/shared/api/agentTasks";
import ReportExportDialog from "@/pages/AgentAudit/components/ReportExportDialog";

export default function AuditTasks() {
	const navigate = useNavigate();

	// Agent 任务状态
	const [agentTasks, setAgentTasks] = useState<AgentTask[]>([]);
	const [loading, setLoading] = useState(true);
	const [searchTerm, setSearchTerm] = useState("");
	const [statusFilter, setStatusFilter] = useState<string>("all");
	const [cancellingAgentTaskId, setCancellingAgentTaskId] = useState<
		string | null
	>(null);
	const [exportingTaskId, setExportingTaskId] = useState<string | null>(null);
	const [showAgentExportDialog, setShowAgentExportDialog] = useState(false);
	const [exportAgentTask, setExportAgentTask] = useState<AgentTask | null>(
		null,
	);
	const [exportAgentFindings, setExportAgentFindings] = useState<
		AgentFinding[]
	>([]);

	useEffect(() => {
		loadAgentTasks();
	}, []);

	// 加载 Agent 任务(支持静默更新,不触发 loading 状态)
	const loadAgentTasks = async (silent = false) => {
		try {
			if (!silent) {
				setLoading(true);
			}
			const data = await getAgentTasks();
			setAgentTasks(data);
		} catch (error) {
			console.error("Failed to load agent tasks:", error);
			if (!silent) {
				toast.error("加载 Agent 任务失败");
			}
		} finally {
			if (!silent) {
				setLoading(false);
			}
		}
	};

	// 自动刷新运行中的 Agent 任务(静默更新,不显示 loading)
	useEffect(() => {
		const activeAgentTasks = agentTasks.filter(
			(task) => task.status === "running" || task.status === "pending",
		);

		if (activeAgentTasks.length === 0) return;

		const intervalId = setInterval(() => loadAgentTasks(true), 5000);
		return () => clearInterval(intervalId);
	}, [agentTasks.map((t) => t.id + t.status).join(",")]);

	const handleCancelAgentTask = async (taskId: string) => {
		if (cancellingAgentTaskId) return;

		try {
			setCancellingAgentTaskId(taskId);
			await cancelAgentTask(taskId);
			toast.success("Agent 任务已取消");
			// 取消后刷新列表,不使用静默模式以显示最新状态
			await loadAgentTasks(false);
		} catch (error: any) {
			console.error("取消 Agent 任务失败:", error);
			toast.error(error?.response?.data?.detail || "取消 Agent 任务失败");
		} finally {
			setCancellingAgentTaskId(null);
		}
	};

	// 打开 Agent 任务导出对话框
	const handleOpenAgentExportDialog = async (task: AgentTask) => {
		try {
			setExportingTaskId(task.id);
			// 获取任务的 findings 列表
			const findings = await getAgentFindings(task.id);
			setExportAgentTask(task);
			setExportAgentFindings(findings);
			setShowAgentExportDialog(true);
		} catch (error: any) {
			console.error("获取 findings 列表失败:", error);
			toast.error("获取审计结果失败");
		} finally {
			setExportingTaskId(null);
		}
	};

	const getStatusBadge = (status: string) => {
		switch (status) {
			case "completed":
				return <Badge className="cyber-badge-success">完成</Badge>;
			case "running":
				return <Badge className="cyber-badge-info">运行中</Badge>;
			case "failed":
				return <Badge className="cyber-badge-danger">失败</Badge>;
			case "cancelled":
				return <Badge className="cyber-badge-muted">已取消</Badge>;
			default:
				return <Badge className="cyber-badge-muted">等待中</Badge>;
		}
	};

	const formatDate = (dateString: string) => {
		return new Date(dateString).toLocaleDateString("zh-CN", {
			year: "numeric",
			month: "short",
			day: "numeric",
			hour: "2-digit",
			minute: "2-digit",
		});
	};

	const filteredAgentTasks = agentTasks.filter((task) => {
		const matchesSearch =
			(task.name || "").toLowerCase().includes(searchTerm.toLowerCase()) ||
			task.task_type.toLowerCase().includes(searchTerm.toLowerCase());
		const matchesStatus =
			statusFilter === "all" || task.status === statusFilter;
		return matchesSearch && matchesStatus;
	});

	// 统计数据
	const agentStats = {
		total: agentTasks.length,
		completed: agentTasks.filter((t) => t.status === "completed").length,
		running: agentTasks.filter((t) => t.status === "running").length,
		failed: agentTasks.filter((t) => t.status === "failed").length,
	};

	if (loading) {
		return (
			<div className="flex items-center justify-center min-h-[60vh]">
				<div className="text-center space-y-4">
					<div className="loading-spinner mx-auto" />
					<p className="text-muted-foreground font-mono text-sm uppercase tracking-wider">
						加载任务数据...
					</p>
				</div>
			</div>
		);
	}

	return (
		<div className="space-y-6 p-6 cyber-bg-elevated min-h-screen font-mono relative">
			{/* Grid background */}
			<div className="absolute inset-0 cyber-grid-subtle pointer-events-none" />

			{/* Stats Cards */}
			<div className="grid grid-cols-2 md:grid-cols-4 gap-4 relative z-10">
				<div className="cyber-card p-4">
					<div className="flex items-center justify-between">
						<div>
							<p className="stat-label">总任务数</p>
							<p className="stat-value">{agentStats.total}</p>
						</div>
						<div className="stat-icon text-primary">
							<Activity className="w-6 h-6" />
						</div>
					</div>
				</div>

				<div className="cyber-card p-4">
					<div className="flex items-center justify-between">
						<div>
							<p className="stat-label">已完成</p>
							<p className="stat-value">{agentStats.completed}</p>
						</div>
						<div className="stat-icon text-emerald-400">
							<CheckCircle className="w-6 h-6" />
						</div>
					</div>
				</div>

				<div className="cyber-card p-4">
					<div className="flex items-center justify-between">
						<div>
							<p className="stat-label">运行中</p>
							<p className="stat-value">{agentStats.running}</p>
						</div>
						<div className="stat-icon text-sky-400">
							<Clock className="w-6 h-6" />
						</div>
					</div>
				</div>

				<div className="cyber-card p-4">
					<div className="flex items-center justify-between">
						<div>
							<p className="stat-label">失败</p>
							<p className="stat-value">{agentStats.failed}</p>
						</div>
						<div className="stat-icon text-rose-400">
							<AlertTriangle className="w-6 h-6" />
						</div>
					</div>
				</div>
			</div>

			{/* Search and Filter */}
			<div className="cyber-card p-4 relative z-10">
				<div className="flex flex-col md:flex-row items-center gap-4">
					<div className="flex-1 relative w-full">
						<Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-muted-foreground w-4 h-4 z-10" />
						<Input
							placeholder="搜索任务名称或类型..."
							value={searchTerm}
							onChange={(e) => setSearchTerm(e.target.value)}
							className="cyber-input !pl-10"
						/>
					</div>
					<Button
						className="cyber-btn-primary h-10"
						onClick={() => navigate("/projects")}
					>
						<Bot className="w-4 h-4 mr-2" />
						新建 PR 审查
					</Button>
					<div className="flex gap-2 w-full md:w-auto overflow-x-auto pb-2 md:pb-0">
						<Button
							size="sm"
							onClick={() => setStatusFilter("all")}
							className={`h-10 ${statusFilter === "all" ? "cyber-btn-primary" : "cyber-btn-outline"}`}
						>
							全部
						</Button>
						<Button
							size="sm"
							onClick={() => setStatusFilter("running")}
							className={`h-10 ${statusFilter === "running" ? "bg-sky-500/90 border-sky-500/50 text-foreground hover:bg-sky-500" : "cyber-btn-outline"}`}
						>
							运行中
						</Button>
						<Button
							size="sm"
							onClick={() => setStatusFilter("completed")}
							className={`h-10 ${statusFilter === "completed" ? "bg-emerald-500/90 border-emerald-500/50 text-foreground hover:bg-emerald-500" : "cyber-btn-outline"}`}
						>
							已完成
						</Button>
						<Button
							size="sm"
							onClick={() => setStatusFilter("failed")}
							className={`h-10 ${statusFilter === "failed" ? "bg-rose-500/90 border-rose-500/50 text-foreground hover:bg-rose-500" : "cyber-btn-outline"}`}
						>
							失败
						</Button>
					</div>
				</div>
			</div>

			{/* Agent Task List */}
			{filteredAgentTasks.length > 0 ? (
				<div className="space-y-4 relative z-10">
					{filteredAgentTasks.map((task) => (
						<div key={task.id} className="cyber-card p-6">
							{/* Task Header */}
							<div className="flex items-center justify-between mb-4 pb-4 border-b border-border">
								<div className="flex items-center space-x-4">
									<div
										className={`w-12 h-12 rounded-lg flex items-center justify-center ${
											task.status === "completed"
												? "bg-emerald-500/20"
												: task.status === "running"
													? "bg-sky-500/20"
													: task.status === "failed"
														? "bg-rose-500/20"
														: "bg-muted"
										}`}
									>
										<Bot
											className={`w-6 h-6 ${
												task.status === "completed"
													? "text-emerald-400"
													: task.status === "running"
														? "text-sky-400"
														: task.status === "failed"
															? "text-rose-400"
															: "text-muted-foreground"
											}`}
										/>
									</div>
									<div>
										<h3 className="font-bold text-xl text-foreground uppercase tracking-wide">
											{task.name || "Agent 审计任务"}
										</h3>
										<p className="text-sm text-muted-foreground font-mono">
											{task.current_phase || task.task_type}
										</p>
									</div>
								</div>
								<div className="flex items-center gap-3">
									{getStatusBadge(task.status)}
									{task.status === "running" && (
										<div className="flex items-center gap-1.5 text-green-400">
											<span className="relative flex h-2 w-2">
												<span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
												<span className="relative inline-flex rounded-full h-2 w-2 bg-green-400"></span>
											</span>
										</div>
									)}
								</div>
							</div>

							{/* Stats Grid */}
							<div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4 font-mono">
								<div className="text-center p-3 bg-muted rounded-lg border border-border">
									<p className="text-2xl font-bold text-foreground">
										{task.total_files}
									</p>
									<p className="text-xs text-muted-foreground uppercase">
										文件数
									</p>
								</div>
								<div className="text-center p-3 bg-muted rounded-lg border border-border">
									<p className="text-2xl font-bold text-foreground">
										{task.analyzed_files}
									</p>
									<p className="text-xs text-muted-foreground uppercase">
										已分析
									</p>
								</div>
								<div className="text-center p-3 bg-muted rounded-lg border border-border">
									<p className="text-2xl font-bold text-amber-400">
										{task.findings_count}
									</p>
									<p className="text-xs text-muted-foreground uppercase">
										发现问题
									</p>
								</div>
								<div className="text-center p-3 bg-muted rounded-lg border border-border">
									<p className="text-2xl font-bold text-sky-400">
										{task.tool_calls_count || 0}
									</p>
									<p className="text-xs text-muted-foreground uppercase">
										工具调用
									</p>
								</div>
								<div className="text-center p-3 bg-muted rounded-lg border border-border">
									<p className="text-2xl font-bold text-primary">
										{task.security_score?.toFixed(1) || "-"}
									</p>
									<p className="text-xs text-muted-foreground uppercase">
										安全评分
									</p>
								</div>
							</div>

							{/* Severity Distribution */}
							{task.findings_count > 0 && (
								<div className="flex gap-4 mb-4 font-mono text-xs">
									{task.critical_count > 0 && (
										<span className="text-rose-500">
											Critical: {task.critical_count}
										</span>
									)}
									{task.high_count > 0 && (
										<span className="text-orange-500">
											High: {task.high_count}
										</span>
									)}
									{task.medium_count > 0 && (
										<span className="text-yellow-500">
											Medium: {task.medium_count}
										</span>
									)}
									{task.low_count > 0 && (
										<span className="text-green-500">
											Low: {task.low_count}
										</span>
									)}
								</div>
							)}

							{/* Progress Bar */}
							<div className="mb-4 font-mono">
								<div className="flex items-center justify-between mb-2">
									<span className="text-sm font-bold text-muted-foreground uppercase">
										审计进度
									</span>
									<span className="text-sm text-muted-foreground">
										{task.analyzed_files || 0} / {task.total_files || 0}{" "}
										文件
									</span>
								</div>
								<Progress
									value={task.progress_percentage || 0}
									className="h-2 bg-muted [&>div]:bg-primary"
								/>
								<div className="text-right mt-1">
									<span className="text-xs text-muted-foreground">
										{(task.progress_percentage || 0).toFixed(0)}% 完成
									</span>
								</div>
							</div>

							{/* Task Footer */}
							<div className="flex items-center justify-between pt-4 border-t border-border">
								<div className="flex items-center space-x-6 text-sm text-muted-foreground font-mono">
									<div className="flex items-center">
										<Calendar className="w-4 h-4 mr-2" />
										{formatDate(task.created_at)}
									</div>
									{task.completed_at && (
										<div className="flex items-center">
											<CheckCircle className="w-4 h-4 mr-2" />
											{formatDate(task.completed_at)}
										</div>
									)}
									{task.tokens_used > 0 && (
										<div className="flex items-center text-muted-foreground">
											<span>
												{task.tokens_used.toLocaleString()} tokens
											</span>
										</div>
									)}
								</div>

								<div className="flex gap-3">
									{(task.status === "running" ||
										task.status === "pending") && (
										<>
											{/* 🔥 查看终端实时流按钮 */}
											<Link to={`/agent-audit/${task.id}`}>
												<Button
													size="sm"
													className="cyber-btn bg-sky-500/90 border-sky-500/50 text-foreground hover:bg-sky-500 h-9"
												>
													<Terminal className="w-4 h-4 mr-2" />
													查看实时流
												</Button>
											</Link>
											<Button
												size="sm"
												className="cyber-btn bg-rose-500/90 border-rose-500/50 text-foreground hover:bg-rose-500 h-9"
												onClick={() => handleCancelAgentTask(task.id)}
												disabled={cancellingAgentTaskId === task.id}
											>
												<XCircle className="w-4 h-4 mr-2" />
												{cancellingAgentTaskId === task.id
													? "取消中..."
													: "取消"}
											</Button>
										</>
									)}
									{(task.status === "completed" ||
										(task.findings_count != null &&
											task.findings_count > 0)) && (
										<Button
											size="sm"
											className="cyber-btn-outline h-9"
											onClick={() => handleOpenAgentExportDialog(task)}
											disabled={exportingTaskId === task.id}
										>
											<Download className="w-4 h-4 mr-2" />
											{exportingTaskId === task.id
												? "加载中..."
												: "导出报告"}
										</Button>
									)}
									{task.runtime_session_id && (
										<Link to={`/audit-sessions/${task.runtime_session_id}`}>
											<Button size="sm" className="cyber-btn-outline h-9">
												<MessagesSquare className="w-4 h-4 mr-2" />
												会话入口
											</Button>
										</Link>
									)}
									{/* 任务详情按钮 */}
									<Link to={`/agent-audit/${task.id}`}>
										<Button size="sm" className="cyber-btn-outline h-9">
											<FileText className="w-4 h-4 mr-2" />
											查看详情
										</Button>
									</Link>
								</div>
							</div>
						</div>
					))}
				</div>
			) : (
				<div className="cyber-card p-16 text-center relative z-10 border-dashed">
					<Bot className="w-16 h-16 text-muted-foreground mx-auto mb-4" />
					<h3 className="text-xl font-bold text-foreground mb-2 uppercase">
						{searchTerm || statusFilter !== "all"
							? "未找到匹配的任务"
							: "暂无 PR 审查任务"}
					</h3>
					<p className="text-muted-foreground mb-6 font-mono">
						{searchTerm || statusFilter !== "all"
							? "尝试调整搜索条件或筛选器"
							: "创建第一个 PR 审查任务开始智能安全审查"}
					</p>
					{!searchTerm && statusFilter === "all" && (
						<Button
							className="cyber-btn-primary"
							onClick={() => navigate("/projects")}
						>
							<Bot className="w-4 h-4 mr-2" />
							创建 PR 审查
						</Button>
					)}
				</div>
			)}

			{/* Agent 任务导出对话框 */}
			{exportAgentTask && (
				<ReportExportDialog
					open={showAgentExportDialog}
					onOpenChange={setShowAgentExportDialog}
					task={exportAgentTask}
					findings={exportAgentFindings}
				/>
			)}
		</div>
	);
}
