/**
 * Create Task Dialog
 * Cyberpunk Terminal Aesthetic
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
	ChevronRight,
	FolderOpen,
	Globe,
	Loader2,
	Package,
	Search,
	Settings2,
	Shield,
} from "lucide-react";
import { toast } from "sonner";

import {
	Dialog,
	DialogContent,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Checkbox } from "@/components/ui/checkbox";
import {
	Collapsible,
	CollapsibleContent,
	CollapsibleTrigger,
} from "@/components/ui/collapsible";
import AgentModeSelector, {
	type AuditMode,
} from "@/components/agent/AgentModeSelector";
import {
	buildAgentTaskAuditScope,
	getAuditModeLabel,
} from "@/components/agent/auditModeConfig";
import { api } from "@/shared/config/database";
import { createAgentTask } from "@/shared/api/agentTasks";
import { isRepositoryProject, isZipProject } from "@/shared/utils/projectUtils";
import type { Project } from "@/shared/types";

import FileSelectionDialog from "./FileSelectionDialog";
import { useProjects } from "./hooks/useTaskForm";
import { useZipFile } from "./hooks/useZipFile";

interface CreateTaskDialogProps {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	onTaskCreated: () => void;
	onFastScanStarted?: (taskId: string) => void;
	preselectedProjectId?: string;
}

const DEFAULT_EXCLUDES = [
	"node_modules/**",
	".git/**",
	"dist/**",
	"build/**",
	"*.log",
];

export default function CreateTaskDialog({
	open,
	onOpenChange,
	onTaskCreated,
	preselectedProjectId,
}: CreateTaskDialogProps) {
	const navigate = useNavigate();
	const [selectedProjectId, setSelectedProjectId] = useState("");
	const [searchTerm, setSearchTerm] = useState("");
	const [branch, setBranch] = useState("main");
	const [versionLabel, setVersionLabel] = useState("");
	const [excludePatterns, setExcludePatterns] = useState(DEFAULT_EXCLUDES);
	const [selectedFiles, setSelectedFiles] = useState<string[] | undefined>();
	const [showAdvanced, setShowAdvanced] = useState(false);
	const [showFileSelection, setShowFileSelection] = useState(false);
	const [creating, setCreating] = useState(false);
	const [auditMode, setAuditMode] = useState<AuditMode>("intelligent_audit");
	const [dynamicVerificationEnabled, setDynamicVerificationEnabled] =
		useState(false);
	const [versionError, setVersionError] = useState(false);
	const versionInputRef = useRef<HTMLInputElement>(null);

	const { projects, loading, loadProjects } = useProjects();
	const selectedProject = projects.find(
		(project) => project.id === selectedProjectId,
	);
	const zipState = useZipFile(selectedProject, projects);

	useEffect(() => {
		const loadBranches = async () => {
			const project = projects.find((item) => item.id === selectedProjectId);
			if (!project || !isRepositoryProject(project)) {
				return;
			}

			try {
				const result = await api.getProjectBranches(project.id);
				if (result.error) {
					toast.error(`加载分支失败: ${result.error}`);
				}
				if (result.default_branch) {
					setBranch(result.default_branch);
				}
			} catch (error) {
				const message = error instanceof Error ? error.message : "未知错误";
				toast.error(`加载分支失败: ${message}`);
				setBranch(project.default_branch || "main");
			}
		};

		loadBranches();
	}, [projects, selectedProjectId]);

	const wasOpenRef = useRef(false);

	useEffect(() => {
		const justOpened = open && !wasOpenRef.current;

		if (justOpened) {
			loadProjects();
			if (preselectedProjectId) {
				setSelectedProjectId(preselectedProjectId);
			}
			setSearchTerm("");
			setShowAdvanced(false);
			setVersionLabel("");
			setVersionError(false);
			setAuditMode("intelligent_audit");
			setDynamicVerificationEnabled(false);
			zipState.reset();
		}

		wasOpenRef.current = open;
	}, [loadProjects, open, preselectedProjectId, zipState]);

	const filteredProjects = useMemo(() => {
		if (!searchTerm) {
			return projects;
		}

		const term = searchTerm.toLowerCase();
		return projects.filter(
			(project) =>
				project.name.toLowerCase().includes(term) ||
				project.description?.toLowerCase().includes(term),
		);
	}, [projects, searchTerm]);

	const excludePatternsRef = useRef(excludePatterns);
	useEffect(() => {
		if (excludePatternsRef.current !== excludePatterns && selectedFiles) {
			setSelectedFiles(undefined);
			toast.info("排除模式已更改，请重新选择文件");
		}
		excludePatternsRef.current = excludePatterns;
	}, [excludePatterns, selectedFiles]);

	const zipHasPersistentSource = useMemo(() => {
		if (!selectedProject || !isZipProject(selectedProject)) {
			return false;
		}
		return Boolean(
			selectedProject.local_path ||
				zipState.storedZipInfo?.has_persistent_source,
		);
	}, [selectedProject, zipState.storedZipInfo?.has_persistent_source]);

	const zipHasUsableSource = useMemo(() => {
		if (!selectedProject || !isZipProject(selectedProject)) {
			return false;
		}
		return (
			zipHasPersistentSource ||
			Boolean(zipState.useStoredZip && zipState.storedZipInfo?.has_file)
		);
	}, [
		selectedProject,
		zipHasPersistentSource,
		zipState.storedZipInfo?.has_file,
		zipState.useStoredZip,
	]);

	const canStart = useMemo(() => {
		if (!selectedProject) {
			return false;
		}

		if (isZipProject(selectedProject)) {
			return zipHasUsableSource;
		}

		return Boolean(selectedProject.repository_url) && Boolean(branch.trim());
	}, [branch, selectedProject, zipHasUsableSource]);

	const handleStartScan = async () => {
		if (!selectedProject) {
			toast.error("请选择一个项目");
			return;
		}

		if (isZipProject(selectedProject) && !zipHasUsableSource) {
			toast.error("项目源码不可用，请先在项目中上传或同步源码");
			return;
		}

		if (!versionLabel.trim()) {
			setVersionError(true);
			requestAnimationFrame(() => {
				versionInputRef.current?.scrollIntoView({
					block: "center",
					behavior: "smooth",
				});
				versionInputRef.current?.focus();
			});
			toast.error("请填写审计版本号");
			return;
		}

		try {
			setCreating(true);

			const agentTask = await createAgentTask({
				project_id: selectedProject.id,
				name: `Agent Audit - ${selectedProject.name}`,
				version_label: versionLabel.trim(),
				branch_name: isRepositoryProject(selectedProject) ? branch : undefined,
				exclude_patterns: excludePatterns,
				target_files: selectedFiles,
				audit_scope: buildAgentTaskAuditScope(
					auditMode,
					dynamicVerificationEnabled,
				),
				verification_level: dynamicVerificationEnabled
					? "sandbox"
					: "analysis_only",
			});

			onOpenChange(false);
			onTaskCreated();
			toast.success("审计任务已创建");
			navigate(`/agent-audit/${agentTask.id}`);

			setSelectedProjectId("");
			setVersionLabel("");
			setVersionError(false);
			setSelectedFiles(undefined);
			setExcludePatterns(DEFAULT_EXCLUDES);
			setAuditMode("intelligent_audit");
			setDynamicVerificationEnabled(false);
		} catch (error) {
			const message = error instanceof Error ? error.message : "未知错误";
			toast.error(`启动失败: ${message}`);
		} finally {
			setCreating(false);
		}
	};

	return (
		<>
			<Dialog open={open} onOpenChange={onOpenChange}>
				<DialogContent className="!w-[min(92vw,1120px)] !max-w-none max-h-[88vh] flex flex-col overflow-hidden p-0 gap-0 border border-border rounded-2xl bg-background shadow-[0_28px_90px_-42px_rgba(15,23,42,0.65)]">
					<DialogHeader className="px-6 py-5 border-b border-border flex-shrink-0 bg-[linear-gradient(135deg,rgba(236,253,245,0.95),rgba(255,255,255,0.98))]">
						<DialogTitle className="flex items-center gap-3 font-mono text-foreground">
							<div className="p-2.5 bg-primary/10 rounded-xl border border-primary/20 shadow-sm">
								<Shield className="w-5 h-5 text-primary" />
							</div>
							<span className="text-lg font-bold tracking-tight">
								开始代码审计
							</span>
						</DialogTitle>
					</DialogHeader>

					<div className="flex-1 overflow-y-auto bg-[radial-gradient(circle_at_top_right,rgba(16,185,129,0.08),transparent_34%),linear-gradient(180deg,rgba(248,250,252,0.9),rgba(255,255,255,0.96))]">
						<div className="grid gap-5 p-5 lg:grid-cols-[310px_minmax(0,1fr)]">
							<aside className="rounded-2xl border border-border bg-background/95 p-4 shadow-sm">
								<div className="flex items-center justify-between">
									<span className="text-sm font-semibold text-foreground">
										选择项目
									</span>
									<Badge className="border-border bg-muted text-muted-foreground">
										{filteredProjects.length} 个
									</Badge>
								</div>

								<div className="relative mt-3">
									<Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
									<Input
										placeholder="搜索项目..."
										value={searchTerm}
										onChange={(event) => setSearchTerm(event.target.value)}
										className="!pl-9 h-10 cyber-input"
									/>
								</div>

								<ScrollArea className="mt-3 h-[300px] rounded-xl border border-border bg-muted/35">
									{loading ? (
										<div className="flex items-center justify-center h-full">
											<Loader2 className="w-5 h-5 animate-spin text-primary" />
										</div>
									) : filteredProjects.length === 0 ? (
										<div className="flex flex-col items-center justify-center h-full text-muted-foreground font-mono">
											<Package className="w-8 h-8 mb-2 opacity-50" />
											<span className="text-sm">
												{searchTerm ? "未找到" : "暂无项目"}
											</span>
										</div>
									) : (
										<div className="p-1.5">
											{filteredProjects.map((project) => (
												<ProjectCard
													key={project.id}
													project={project}
													selected={selectedProjectId === project.id}
													onSelect={() => setSelectedProjectId(project.id)}
												/>
											))}
										</div>
									)}
								</ScrollArea>
							</aside>

							<section className="min-w-0 space-y-4">
								{!selectedProject ? (
									<div className="flex min-h-[420px] flex-col items-center justify-center rounded-2xl border border-dashed border-border bg-background text-center shadow-sm">
										<Package className="mb-3 h-9 w-9 text-muted-foreground/55" />
										<p className="text-base font-semibold text-foreground">
											先选择一个项目
										</p>
										<p className="mt-1 text-sm text-muted-foreground">
											选择后即可配置审计模式和版本号。
										</p>
									</div>
								) : (
									<>
										<div className="rounded-2xl border border-emerald-200/80 bg-[linear-gradient(135deg,rgba(236,253,245,0.88),rgba(255,255,255,0.96))] p-4 shadow-[0_16px_42px_-36px_rgba(16,185,129,0.6)]">
											<div className="flex items-center justify-between gap-3">
												<div className="flex items-center gap-2">
													<div className="flex h-8 w-8 items-center justify-center rounded-lg border border-emerald-200 bg-emerald-100/80 text-emerald-700">
														<Package className="h-4 w-4" />
													</div>
													<label
														htmlFor="audit-version-label"
														className="text-sm font-semibold text-foreground"
													>
														审计版本号
													</label>
												</div>
												<span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
													必填
												</span>
											</div>
											<Input
												id="audit-version-label"
												ref={versionInputRef}
												value={versionLabel}
												onChange={(event) => {
													setVersionLabel(event.target.value);
													if (versionError && event.target.value.trim()) {
														setVersionError(false);
													}
												}}
												className={`mt-3 h-11 bg-white/90 font-mono shadow-sm ${
													versionError
														? "border-primary ring-2 ring-primary/15"
														: "border-emerald-200/80"
												}`}
											/>
											{versionError && (
												<p className="mt-2 text-xs text-primary">
													请填写审计版本号
												</p>
											)}
										</div>

										<AgentModeSelector
											value={auditMode}
											onChange={setAuditMode}
											verificationEnabled={dynamicVerificationEnabled}
											onVerificationChange={setDynamicVerificationEnabled}
											disabled={creating}
										/>

										<Collapsible
											open={showAdvanced}
											onOpenChange={setShowAdvanced}
										>
											<CollapsibleTrigger className="flex items-center gap-2 rounded-xl border border-border bg-background/80 px-4 py-3 text-xs font-mono text-muted-foreground shadow-sm transition-colors hover:text-foreground">
												<ChevronRight
													className={`w-4 h-4 transition-transform ${showAdvanced ? "rotate-90" : ""}`}
												/>
												<Settings2 className="w-4 h-4" />
												<span className="uppercase font-bold">高级选项</span>
											</CollapsibleTrigger>
											<CollapsibleContent className="mt-3 space-y-3">
												<div className="p-3 border border-dashed border-border rounded bg-muted/50 space-y-3">
													<div className="flex items-center justify-between">
														<span className="font-mono text-xs uppercase font-bold text-muted-foreground">
															排除模式
														</span>
														<button
															type="button"
															onClick={() =>
																setExcludePatterns(DEFAULT_EXCLUDES)
															}
															className="text-xs font-mono text-primary hover:text-primary/80"
														>
															重置为默认
														</button>
													</div>

													<div className="flex flex-wrap gap-1.5">
														{excludePatterns.map((pattern) => (
															<Badge
																key={pattern}
																className="bg-muted text-foreground border-0 font-mono text-xs cursor-pointer hover:bg-rose-100 dark:hover:bg-rose-900/50 hover:text-rose-600 dark:hover:text-rose-400"
																onClick={() =>
																	setExcludePatterns((prev) =>
																		prev.filter((item) => item !== pattern),
																	)
																}
															>
																{pattern} ×
															</Badge>
														))}
														{excludePatterns.length === 0 && (
															<span className="text-xs text-muted-foreground font-mono">
																无排除模式
															</span>
														)}
													</div>

													<div className="flex flex-wrap gap-1">
														<span className="text-xs text-muted-foreground font-mono mr-1">
															快捷添加:
														</span>
														{[
															".test.",
															".spec.",
															".min.",
															"coverage/",
															"docs/",
															".md",
														].map((pattern) => (
															<button
																key={pattern}
																type="button"
																disabled={excludePatterns.includes(pattern)}
																onClick={() => {
																	if (!excludePatterns.includes(pattern)) {
																		setExcludePatterns((prev) => [
																			...prev,
																			pattern,
																		]);
																	}
																}}
																className="text-xs font-mono px-1.5 py-0.5 border border-border bg-muted hover:bg-muted text-muted-foreground hover:text-foreground disabled:opacity-40 disabled:cursor-not-allowed rounded"
															>
																+{pattern}
															</button>
														))}
													</div>

													<Input
														placeholder="添加自定义排除模式，回车确认"
														className="h-8 cyber-input text-sm"
														onKeyDown={(event) => {
															if (
																event.key === "Enter" &&
																event.currentTarget.value
															) {
																const value = event.currentTarget.value.trim();
																if (value && !excludePatterns.includes(value)) {
																	setExcludePatterns((prev) => [
																		...prev,
																		value,
																	]);
																}
																event.currentTarget.value = "";
															}
														}}
													/>
												</div>

												{(() => {
													const hasStoredZip = zipState.storedZipInfo?.has_file;
													const canSelectFiles =
														isRepositoryProject(selectedProject) ||
														(isZipProject(selectedProject) &&
															(zipHasPersistentSource ||
																(zipState.useStoredZip && hasStoredZip)));

													return (
														<div className="flex items-center justify-between p-3 border border-dashed border-border rounded bg-muted/50">
															<div>
																<p className="font-mono text-xs uppercase font-bold text-muted-foreground">
																	目标文件
																</p>
																<p className="text-sm font-bold text-foreground mt-1">
																	{selectedFiles
																		? `已选择 ${selectedFiles.length} 个文件`
																		: "审计全部文件"}
																</p>
															</div>
															<div className="flex gap-2">
																{selectedFiles && canSelectFiles && (
																	<Button
																		size="sm"
																		variant="ghost"
																		onClick={() => setSelectedFiles(undefined)}
																		className="h-8 text-xs text-rose-600 dark:text-rose-400 hover:bg-rose-100 dark:hover:bg-rose-900/30 hover:text-rose-700 dark:hover:text-rose-300"
																	>
																		清空
																	</Button>
																)}
																<Button
																	size="sm"
																	variant="outline"
																	onClick={() => setShowFileSelection(true)}
																	disabled={!canSelectFiles}
																	className="h-8 text-xs cyber-btn-outline font-mono font-bold disabled:opacity-50"
																>
																	<FolderOpen className="w-3 h-3 mr-1" />
																	选择文件
																</Button>
															</div>
														</div>
													);
												})()}
											</CollapsibleContent>
										</Collapsible>
									</>
								)}
							</section>
						</div>
					</div>

					<div className="flex-shrink-0 flex items-center justify-between gap-3 px-6 py-4 bg-background/95 border-t border-border">
						<p className="hidden text-xs text-muted-foreground sm:block">
							{selectedProject
								? `将启动 ${getAuditModeLabel(auditMode)}`
								: "请选择项目后启动审计"}
						</p>
						<div className="flex gap-3">
							<Button
								variant="ghost"
								onClick={() => onOpenChange(false)}
								disabled={creating}
								className="px-4 h-10 text-muted-foreground hover:text-foreground hover:bg-muted"
							>
								取消
							</Button>
							<Button
								onClick={handleStartScan}
								disabled={!canStart || creating}
								className="px-5 h-10 cyber-btn-primary font-semibold"
							>
								{creating ? (
									<>
										<Loader2 className="w-4 h-4 animate-spin mr-2" />
										启动中...
									</>
								) : (
									<>
										<Shield className="w-4 h-4 mr-2" />
										启动{getAuditModeLabel(auditMode)}
									</>
								)}
							</Button>
						</div>
					</div>
				</DialogContent>
			</Dialog>

			<FileSelectionDialog
				open={showFileSelection}
				onOpenChange={setShowFileSelection}
				projectId={selectedProjectId}
				branch={branch}
				excludePatterns={excludePatterns}
				onConfirm={setSelectedFiles}
			/>
		</>
	);
}

function ProjectCard({
	project,
	selected,
	onSelect,
}: {
	project: Project;
	selected: boolean;
	onSelect: () => void;
}) {
	const isRepo = isRepositoryProject(project);

	return (
		<div
			className={`flex items-center gap-3 p-3 cursor-pointer rounded-xl border transition-all ${
				selected
					? "bg-primary/10 border-primary/35"
					: "border-transparent hover:bg-muted/70"
			}`}
			onClick={onSelect}
		>
			<Checkbox
				checked={selected}
				className="border-border data-[state=checked]:bg-primary data-[state=checked]:border-primary"
			/>

			<div
				className={`p-1.5 rounded-lg ${selected ? "bg-primary/10" : "bg-muted"}`}
			>
				{isRepo ? (
					<Globe
						className={`w-4 h-4 ${selected ? "text-primary" : "text-muted-foreground"}`}
					/>
				) : (
					<Package
						className={`w-4 h-4 ${selected ? "text-primary" : "text-muted-foreground"}`}
					/>
				)}
			</div>

			<div className="flex-1 min-w-0 overflow-hidden">
				<div className="flex items-center gap-2">
					<span
						className={`text-sm truncate ${selected ? "text-foreground font-semibold" : "text-foreground"}`}
					>
						{project.name}
					</span>
					<Badge className="border-border bg-muted px-1.5 py-0 text-[10px] font-medium text-muted-foreground">
						{isRepo ? "REPO" : "ZIP"}
					</Badge>
				</div>
				{project.description && (
					<p
						className="mt-0.5 line-clamp-2 text-xs text-muted-foreground"
						title={project.description}
					>
						{project.description}
					</p>
				)}
			</div>
		</div>
	);
}
