import { useEffect, useMemo, useState } from "react";
import { useRef } from "react";
import {
	BookOpen,
	FileArchive,
	FolderOpen,
	Github,
	Link2,
	Pencil,
	RefreshCw,
	Search,
	Trash2,
	UploadCloud,
} from "lucide-react";
import { toast } from "sonner";

import {
	AlertDialog,
	AlertDialogAction,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
	createSkillBinding,
	deleteSkill,
	getSkill,
	getSkills,
	importGithubSkill,
	resyncSkills,
	updateSkill,
	updateSkillBinding,
	uploadSkillZip,
	type AgentSkillBinding,
	type Skill,
	type SkillMetadata,
	type SkillPayload,
} from "@/shared/api/skills";
import { syncLocalLibraries } from "@/shared/api/modelConfig";

const AGENT_OPTIONS = [
	{ value: "orchestrator", label: "orchestrator Agent" },
	{ value: "recon", label: "recon Agent" },
	{ value: "scan", label: "scan Agent" },
	{ value: "triage", label: "triage Agent" },
	{ value: "finding", label: "finding Agent" },
	{ value: "verification", label: "verification Agent" },
	{ value: "audit_chat", label: "用户会话" },
] as const;

const HOST_PROJECT_ROOT =
	(import.meta.env.VITE_HOST_PROJECT_ROOT as string | undefined) || "";
const DEFAULT_HOST_SKILL_LIBRARY_ROOT = "[项目根目录]/skill_library";
const ALL_AGENTS_VALUE = "__all_agents__";
const IMPORT_AGENT_OPTIONS = [
	{ value: ALL_AGENTS_VALUE, label: "所有 Agent" },
	...AGENT_OPTIONS,
] as const;
const EMPTY_FORM: SkillPayload = {
	name: "",
	slug: "",
	description: "",
	source_type: "manual",
	source_url: "",
	content: "",
	tags: [],
	is_active: true,
	bindings: [],
};

function errorMessage(error: unknown, fallback: string) {
	if (typeof error === "object" && error && "response" in error) {
		const detail = (error as { response?: { data?: { detail?: string } } })
			.response?.data?.detail;
		if (detail) return detail;
	}
	return error instanceof Error ? error.message : fallback;
}

function trimTrailingSeparators(path: string) {
	return path.replace(/[\\/]+$/, "");
}

function parentPath(path: string) {
	const normalized = trimTrailingSeparators(path);
	const index = Math.max(
		normalized.lastIndexOf("/"),
		normalized.lastIndexOf("\\"),
	);
	return index > 0 ? normalized.slice(0, index) : normalized;
}

function hostPathFromWorkspace(relativePath: string) {
	if (!HOST_PROJECT_ROOT || !relativePath) return "";
	const separator = HOST_PROJECT_ROOT.includes("\\") ? "\\" : "/";
	const normalizedRelative = relativePath
		.replace(/^[/\\]+/, "")
		.replace(/[\\/]+/g, separator);
	return `${trimTrailingSeparators(HOST_PROJECT_ROOT)}${separator}${normalizedRelative}`;
}

function copyText(value: string, message: string) {
	if (!value) return;
	navigator.clipboard
		.writeText(value)
		.then(() => toast.success(message))
		.catch(() => toast.error("复制失败"));
}

function metadataString(skill: SkillMetadata | Skill, key: string) {
	const value = skill.metadata_json?.[key];
	return typeof value === "string" ? value : "";
}

function firstPath(...values: Array<string | undefined>) {
	return (
		values.find((value) => typeof value === "string" && value.trim()) || ""
	);
}

function bindingFor(
	skill: SkillMetadata,
	agent: string,
): AgentSkillBinding | undefined {
	return skill.bindings.find((binding) => binding.agent_type === agent);
}

function skillFolderPath(skill: SkillMetadata | Skill) {
	return firstPath(
		metadataString(skill, "host_storage_path"),
		hostPathFromWorkspace(metadataString(skill, "workspace_relative_path")),
		`${DEFAULT_HOST_SKILL_LIBRARY_ROOT}/${skill.slug}`,
	);
}

function agentBindingCount(skills: SkillMetadata[], agent: string) {
	return skills.filter((skill) => bindingFor(skill, agent)?.enabled).length;
}

function sortBindings(bindings: AgentSkillBinding[]) {
	return [...bindings].sort(
		(left, right) =>
			left.agent_type.localeCompare(right.agent_type) ||
			left.sort_order - right.sort_order ||
			left.skill_id.localeCompare(right.skill_id),
	);
}

function skillLibraryRootPath(skills: SkillMetadata[]) {
	const firstSkillRoot = skills
		.map((skill) => skillFolderPath(skill))
		.find(Boolean);
	return firstSkillRoot
		? parentPath(firstSkillRoot)
		: hostPathFromWorkspace("skill_library") || DEFAULT_HOST_SKILL_LIBRARY_ROOT;
}

function DetailRow({ label, value }: { label: string; value: string }) {
	return (
		<div className="rounded-2xl border border-border bg-white/90 px-4 py-3 shadow-sm">
			<div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
				{label}
			</div>
			<div className="mt-2 break-all text-sm leading-6 text-foreground">
				{value || "暂无"}
			</div>
		</div>
	);
}

export default function SkillsManager() {
	const [skills, setSkills] = useState<SkillMetadata[]>([]);
	const [search, setSearch] = useState("");
	const [selectedAgent, setSelectedAgent] = useState("finding");
	const [loading, setLoading] = useState(false);
	const [loaded, setLoaded] = useState(false);
	const [detailSkill, setDetailSkill] = useState<Skill | null>(null);
	const [detailOpen, setDetailOpen] = useState(false);
	const [editorOpen, setEditorOpen] = useState(false);
	const [importOpen, setImportOpen] = useState(false);
	const [uploadOpen, setUploadOpen] = useState(false);
	const [editingSkill, setEditingSkill] = useState<SkillMetadata | null>(null);
	const [skillForm, setSkillForm] = useState<SkillPayload>(EMPTY_FORM);
	const [importUrl, setImportUrl] = useState("");
	const [importAgent, setImportAgent] = useState(ALL_AGENTS_VALUE);
	const [uploadFile, setUploadFile] = useState<File | null>(null);
	const [syncing, setSyncing] = useState(false);
	const [uploading, setUploading] = useState(false);
	const [pendingDeleteSkill, setPendingDeleteSkill] =
		useState<SkillMetadata | null>(null);
	const [deletingSkill, setDeletingSkill] = useState(false);
	const requestedSkillsRef = useRef(false);

	const rootPath = useMemo(() => skillLibraryRootPath(skills), [skills]);
	const filteredSkills = useMemo(() => {
		const keyword = search.trim().toLowerCase();
		if (!keyword) return skills;
		return skills.filter((skill) =>
			[skill.name, skill.slug, skill.description]
				.join(" ")
				.toLowerCase()
				.includes(keyword),
		);
	}, [search, skills]);
	const showSkillSkeletons = loading && skills.length === 0;
	const showEmptySkills = loaded && !loading && filteredSkills.length === 0;

	const loadSkillsPage = async (options: { silent?: boolean } = {}) => {
		const shouldShowLoading = !options.silent;
		try {
			if (shouldShowLoading) {
				setLoading(true);
			}
			const response = await getSkills();
			setSkills(response.items);
		} catch (error) {
			toast.error(errorMessage(error, "加载 Skills 失败"));
		} finally {
			setLoaded(true);
			if (shouldShowLoading) {
				setLoading(false);
			}
		}
	};

	useEffect(() => {
		if (requestedSkillsRef.current) return;
		requestedSkillsRef.current = true;
		void loadSkillsPage();
	}, []);

	const openEdit = (skill: SkillMetadata) => {
		setEditingSkill(skill);
		setSkillForm({
			...EMPTY_FORM,
			name: skill.name,
			slug: skill.slug,
			description: skill.description,
			source_type: skill.source_type,
			source_url: skill.source_url,
			tags: skill.tags || [],
			is_active: skill.is_active,
			metadata_json: skill.metadata_json || {},
			content: "",
		});
		setEditorOpen(true);
	};

	const openDetail = async (skill: SkillMetadata) => {
		try {
			const full = await getSkill(skill.id);
			setDetailSkill(full);
			setDetailOpen(true);
		} catch (error) {
			toast.error(errorMessage(error, "加载 Skill 详情失败"));
		}
	};

	const saveSkill = async () => {
		if (!editingSkill) return;
		const payload: SkillPayload = {
			...skillForm,
			slug: (skillForm.slug || skillForm.name)
				.trim()
				.toLowerCase()
				.replace(/\s+/g, "-")
				.replace(/[^a-z0-9._-]+/g, "-"),
			tags: Array.isArray(skillForm.tags)
				? skillForm.tags
				: String(skillForm.tags || "")
						.split(",")
						.map((item) => item.trim())
						.filter(Boolean),
		};
		delete payload.content;

		try {
			await updateSkill(editingSkill.id, payload);
			toast.success("Skill 已更新");
			setEditorOpen(false);
			await loadSkillsPage({ silent: true });
		} catch (error) {
			toast.error(errorMessage(error, "保存 Skill 失败"));
		}
	};

	const requestDeleteSkill = (skill: SkillMetadata) => {
		setPendingDeleteSkill(skill);
	};

	const confirmDeleteSkill = async () => {
		if (!pendingDeleteSkill) return;
		try {
			setDeletingSkill(true);
			await deleteSkill(pendingDeleteSkill.id);
			toast.success("Skill 已删除");
			setPendingDeleteSkill(null);
			await loadSkillsPage({ silent: true });
		} catch (error) {
			toast.error(errorMessage(error, "删除 Skill 失败"));
		} finally {
			setDeletingSkill(false);
		}
	};

	const importSkill = async () => {
		try {
			const importedSkill = await importGithubSkill({
				repo_url: importUrl,
				agent_type: importAgent === ALL_AGENTS_VALUE ? undefined : importAgent,
				bind_to_agent: importAgent !== ALL_AGENTS_VALUE,
				enabled: true,
				always_include: importAgent === "finding",
			});
			if (importAgent === ALL_AGENTS_VALUE) {
				await Promise.all(
					AGENT_OPTIONS.map((agent) =>
						createSkillBinding(importedSkill.id, {
							agent_type: agent.value,
							enabled: true,
							always_include: agent.value === "finding",
							sort_order: 0,
							match_keywords: [],
							match_config: {},
						}),
					),
				);
			}
			await syncLocalLibraries();
			await resyncSkills();
			toast.success("GitHub Skill 导入成功");
			setImportOpen(false);
			await loadSkillsPage({ silent: true });
		} catch (error) {
			toast.error(errorMessage(error, "GitHub Skill 导入失败"));
		}
	};

	const uploadSkillArchive = async () => {
		if (!uploadFile) {
			toast.error("请选择 ZIP 格式的 Skill 压缩包");
			return;
		}
		try {
			setUploading(true);
			await uploadSkillZip(uploadFile);
			toast.success("Skill 压缩包已上传");
			setUploadOpen(false);
			setUploadFile(null);
			await loadSkillsPage({ silent: true });
		} catch (error) {
			toast.error(errorMessage(error, "上传 Skill 压缩包失败"));
		} finally {
			setUploading(false);
		}
	};

	const applyBindingUpdate = (
		skillId: string,
		nextBinding: AgentSkillBinding,
	) => {
		setSkills((currentSkills) =>
			currentSkills.map((skill) => {
				if (skill.id !== skillId) return skill;
				const bindings = skill.bindings.filter(
					(binding) =>
						binding.id !== nextBinding.id &&
						binding.agent_type !== nextBinding.agent_type,
				);
				return { ...skill, bindings: sortBindings([...bindings, nextBinding]) };
			}),
		);
	};

	const toggleBinding = async (skill: SkillMetadata, enabled: boolean) => {
		const binding = bindingFor(skill, selectedAgent);
		if (!binding && !enabled) return;
		const previousSkills = skills;
		const optimisticBinding: AgentSkillBinding = {
			id: binding?.id || `${selectedAgent}:${skill.slug}`,
			skill_id: binding?.skill_id || skill.id,
			agent_type: selectedAgent,
			enabled,
			always_include: binding?.always_include ?? selectedAgent === "finding",
			sort_order: binding?.sort_order ?? 0,
			match_keywords: binding?.match_keywords || [],
			match_config: binding?.match_config || {},
			bindings_file: binding?.bindings_file,
			skill_file: binding?.skill_file,
			created_by: binding?.created_by,
			created_at: binding?.created_at,
			updated_at: binding?.updated_at,
		};
		applyBindingUpdate(skill.id, optimisticBinding);
		try {
			if (!binding && enabled) {
				const created = await createSkillBinding(skill.id, {
					agent_type: selectedAgent,
					enabled: true,
					always_include: selectedAgent === "finding",
					sort_order: 0,
					match_keywords: [],
					match_config: {},
				});
				applyBindingUpdate(skill.id, created);
			} else if (binding) {
				const updated = await updateSkillBinding(skill.id, binding.id, {
					enabled,
				});
				applyBindingUpdate(skill.id, updated);
			}
		} catch (error) {
			setSkills(previousSkills);
			toast.error(errorMessage(error, "更新 Agent 绑定失败"));
		}
	};

	const removeBinding = async (skill: SkillMetadata) => {
		const binding = bindingFor(skill, selectedAgent);
		if (!binding) return;
		const previousSkills = skills;
		applyBindingUpdate(skill.id, { ...binding, enabled: false });
		try {
			const updated = await updateSkillBinding(skill.id, binding.id, {
				enabled: false,
			});
			applyBindingUpdate(skill.id, updated);
			toast.success("已停用当前 Agent 绑定");
		} catch (error) {
			setSkills(previousSkills);
			toast.error(errorMessage(error, "停用 Agent 绑定失败"));
		}
	};

	const syncSkillDirectory = async () => {
		try {
			setSyncing(true);
			await syncLocalLibraries();
			await resyncSkills();
			await loadSkillsPage({ silent: true });
			toast.success("Skill 目录已同步到本地文件夹");
		} catch (error) {
			toast.error(errorMessage(error, "同步 Skill 目录失败"));
		} finally {
			setSyncing(false);
		}
	};

	return (
		<div className="cyber-bg-elevated relative min-h-screen overflow-x-hidden px-6 py-8">
			<div className="absolute inset-0 cyber-grid-subtle pointer-events-none" />
			<div className="relative z-10 mx-auto max-w-7xl space-y-5">
				<section className="overflow-hidden rounded-2xl border border-slate-200 bg-[linear-gradient(135deg,rgba(255,255,255,0.98),rgba(242,248,245,0.98))] p-6 shadow-[0_18px_50px_rgba(15,23,42,0.06)]">
					<div className="flex flex-col gap-6 xl:flex-row xl:items-start xl:justify-between">
						<div className="max-w-3xl space-y-3">
							<div className="inline-flex items-center gap-2 rounded-full border border-teal-200 bg-white px-3 py-1 text-xs font-medium text-teal-800">
								<BookOpen className="h-3.5 w-3.5" /> Skills Catalog
							</div>
							<h1 className="text-4xl font-bold tracking-normal text-slate-950">
								技能管理
							</h1>
							<p className="max-w-3xl text-sm leading-7 text-slate-600">
								从本地上传或 GitHub 导入的 Skill
								在这进行统一管理，可以根据实际需要为不同的 Agent 绑定不同的
								Skill。
							</p>
						</div>
						<div className="flex flex-wrap gap-3">
							<Button
								variant="outline"
								className="h-11 rounded-full"
								onClick={syncSkillDirectory}
								disabled={syncing}
							>
								{syncing ? (
									<RefreshCw className="mr-2 h-4 w-4 animate-spin" />
								) : (
									<RefreshCw className="mr-2 h-4 w-4" />
								)}
								同步本地目录
							</Button>
							<Button
								variant="outline"
								className="h-11 rounded-full"
								onClick={() => setImportOpen(true)}
							>
								<Github className="mr-2 h-4 w-4" /> 导入 GitHub Skill
							</Button>
							<Button
								className="h-11 rounded-full cyber-btn-primary"
								onClick={() => setUploadOpen(true)}
							>
								<UploadCloud className="mr-2 h-4 w-4" /> 上传 Skill
							</Button>
						</div>
					</div>
				</section>

				<section className="grid gap-3 md:grid-cols-2 xl:grid-cols-[minmax(280px,0.9fr)_repeat(3,minmax(150px,0.55fr))]">
					<div className="rounded-2xl border border-slate-200 bg-white/95 p-5 shadow-[0_16px_42px_rgba(15,23,42,0.05)]">
						<div className="flex items-start justify-between gap-4">
							<div className="space-y-2">
								<div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">
									Skills 根目录
								</div>
								<div className="break-all text-sm leading-7 text-foreground">
									{rootPath || (loading ? "正在读取技能库..." : "未检测到根目录")}
								</div>
							</div>
							<FolderOpen className="h-6 w-6 text-primary" />
						</div>
					</div>

					<div className="rounded-2xl border border-slate-200 bg-white/95 p-5 shadow-[0_16px_42px_rgba(15,23,42,0.05)]">
						<div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">
							技能总数
						</div>
						<div className="mt-3 text-3xl font-black text-primary">
							{skills.length}
						</div>
					</div>
					<div className="rounded-2xl border border-slate-200 bg-white/95 p-5 shadow-[0_16px_42px_rgba(15,23,42,0.05)]">
						<div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">
							当前 Agent 已启用
						</div>
						<div className="mt-3 text-3xl font-black text-primary">
							{agentBindingCount(skills, selectedAgent)}
						</div>
					</div>
					<div className="rounded-2xl border border-slate-200 bg-white/95 p-5 shadow-[0_16px_42px_rgba(15,23,42,0.05)]">
						<div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">
							GitHub 来源
						</div>
						<div className="mt-3 text-3xl font-black text-primary">
							{skills.filter((item) => item.source_type === "github").length}
						</div>
					</div>
				</section>

				<section className="rounded-2xl border border-slate-200 bg-white/95 p-5 shadow-[0_16px_42px_rgba(15,23,42,0.05)]">
					<div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
						<div>
							<div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">
								Agent 绑定
							</div>
						</div>
						<div className="flex flex-col gap-3 md:flex-row">
							<Select value={selectedAgent} onValueChange={setSelectedAgent}>
								<SelectTrigger className="min-w-[220px]">
									<SelectValue />
								</SelectTrigger>
								<SelectContent>
									{AGENT_OPTIONS.map((agent) => (
										<SelectItem key={agent.value} value={agent.value}>
											{agent.label}
										</SelectItem>
									))}
								</SelectContent>
							</Select>
							<div className="relative min-w-[300px]">
								<Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
								<Input
									className="pl-9"
									placeholder="搜索 Skill 名称或 slug"
									value={search}
									onChange={(event) => setSearch(event.target.value)}
								/>
							</div>
						</div>
					</div>
				</section>

				<section className="grid gap-4 md:grid-cols-2">
					{showSkillSkeletons &&
						Array.from({ length: 4 }).map((_, index) => (
							<article
								key={`skill-loading-${index}`}
								className="rounded-[24px] border border-slate-200 bg-white/90 p-5 shadow-[0_18px_44px_rgba(15,23,42,0.05)]"
							>
								<div className="flex items-start gap-3">
									<div className="h-11 w-11 animate-pulse rounded-2xl bg-slate-100" />
									<div className="flex-1 space-y-3">
										<div className="h-5 w-1/2 animate-pulse rounded-full bg-slate-100" />
										<div className="h-4 w-1/3 animate-pulse rounded-full bg-slate-100" />
									</div>
								</div>
								<div className="mt-5 space-y-3">
									<div className="h-4 w-full animate-pulse rounded-full bg-slate-100" />
									<div className="h-4 w-4/5 animate-pulse rounded-full bg-slate-100" />
									<div className="h-14 w-full animate-pulse rounded-2xl bg-slate-100" />
								</div>
							</article>
						))}
					{!showSkillSkeletons &&
						filteredSkills.map((skill) => {
						const binding = bindingFor(skill, selectedAgent);
						const folderPath = skillFolderPath(skill);
						return (
							<article
								key={skill.id}
								className={`group relative overflow-hidden rounded-[24px] border p-5 shadow-[0_18px_44px_rgba(15,23,42,0.06)] transition duration-200 hover:-translate-y-1 hover:shadow-[0_28px_70px_rgba(15,23,42,0.12)] ${
									binding?.enabled
										? "border-primary/20 bg-[linear-gradient(135deg,rgba(255,255,255,0.99),rgba(239,253,246,0.92))]"
										: "border-slate-200 bg-[linear-gradient(135deg,rgba(255,255,255,0.99),rgba(248,250,252,0.95))]"
								}`}
							>
								<div
									className={`pointer-events-none absolute inset-x-0 top-0 h-1 ${
										binding?.enabled
											? "bg-gradient-to-r from-primary/80 via-emerald-300/70 to-transparent"
											: "bg-gradient-to-r from-slate-300 via-slate-200 to-transparent"
									}`}
								/>
								<div
									className={`pointer-events-none absolute -right-16 -top-20 h-48 w-48 rounded-full blur-3xl transition-opacity duration-200 group-hover:opacity-100 ${
										binding?.enabled
											? "bg-primary/15 opacity-70"
											: "bg-slate-200/80 opacity-50"
									}`}
								/>
								<div className="flex items-start justify-between gap-4">
									<div className="relative min-w-0 space-y-3">
										<div className="flex items-center gap-3">
											<div
												className={`flex h-11 w-11 items-center justify-center rounded-2xl border ${
													binding?.enabled
														? "border-primary/20 bg-primary/10 text-primary"
														: "border-slate-200 bg-slate-50 text-slate-500"
												}`}
											>
												<BookOpen className="h-5 w-5" />
											</div>
											<div className="min-w-0">
												<h3 className="text-xl font-bold text-foreground">
													{skill.name}
												</h3>
												<p className="text-sm text-muted-foreground">
													{skill.slug}
												</p>
											</div>
										</div>
										<p className="line-clamp-3 text-sm leading-7 text-muted-foreground">
											{skill.description || "暂无描述"}
										</p>
									</div>
									<Switch
										className="relative z-10"
										checked={Boolean(binding?.enabled)}
										onCheckedChange={(checked) => toggleBinding(skill, checked)}
									/>
								</div>

								<div className="relative mt-5 grid gap-3">
									<DetailRow label="Skill 目录" value={folderPath} />
								</div>

								<div className="relative mt-5 flex flex-wrap gap-3">
									<Button
										size="sm"
										className="cyber-btn-primary"
										onClick={() => openDetail(skill)}
									>
										查看详情
									</Button>
									<Button
										variant="outline"
										size="sm"
										onClick={() => openEdit(skill)}
									>
										<Pencil className="mr-2 h-4 w-4" /> 编辑
									</Button>
									{binding?.enabled && (
										<Button
											variant="outline"
											size="sm"
											onClick={() => removeBinding(skill)}
										>
											移除当前 Agent 绑定
										</Button>
									)}
									<Button
										variant="outline"
										size="sm"
										onClick={() => requestDeleteSkill(skill)}
									>
										<Trash2 className="mr-2 h-4 w-4" /> 删除
									</Button>
								</div>
							</article>
						);
					})}
					{showEmptySkills && (
						<div className="md:col-span-2 rounded-[24px] border border-dashed border-slate-200 bg-white/80 p-12 text-center shadow-[0_18px_44px_rgba(15,23,42,0.04)]">
							<div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
								<BookOpen className="h-5 w-5" />
							</div>
							<div className="mt-4 text-base font-semibold text-foreground">
								暂无 Skill
							</div>
							<p className="mt-2 text-sm text-muted-foreground">
								可以导入 GitHub Skill，或点击同步本地目录刷新技能库。
							</p>
						</div>
					)}
				</section>

				<Dialog open={detailOpen} onOpenChange={setDetailOpen}>
					<DialogContent className="max-w-6xl border-border bg-[linear-gradient(180deg,rgba(255,255,255,.99),rgba(236,253,245,.9))] sm:max-h-[88vh] overflow-hidden">
						<DialogHeader>
							<DialogTitle className="text-2xl font-black text-foreground">
								Skill 详情
							</DialogTitle>
						</DialogHeader>
						{detailSkill && (
							<div className="grid gap-5 lg:grid-cols-[0.92fr_1.08fr]">
								<div className="space-y-4 rounded-[24px] border border-border bg-white/95 p-5">
									<div>
										<h3 className="text-2xl font-bold text-foreground">
											{detailSkill.name}
										</h3>
										<p className="mt-2 text-sm leading-7 text-muted-foreground">
											{detailSkill.description || "暂无描述"}
										</p>
									</div>
									<DetailRow label="Slug" value={detailSkill.slug} />
									<DetailRow
										label="Skill 目录"
										value={skillFolderPath(detailSkill)}
									/>
								</div>
								<div className="rounded-[24px] border border-border bg-white/95 p-5">
									<div className="flex items-center justify-between">
										<h4 className="text-lg font-bold text-foreground">
											SKILL.md 内容
										</h4>
										<Button
											variant="outline"
											size="sm"
											onClick={() =>
												copyText(
													detailSkill.content || "",
													"已复制 SKILL.md 内容",
												)
											}
										>
											复制内容
										</Button>
									</div>
									<ScrollArea className="mt-4 h-[58vh] rounded-2xl border border-border bg-muted/25 p-4">
										<pre className="whitespace-pre-wrap text-sm leading-7 text-foreground">
											{detailSkill.content || "暂无内容"}
										</pre>
									</ScrollArea>
								</div>
							</div>
						)}
					</DialogContent>
				</Dialog>

				<Dialog open={editorOpen} onOpenChange={setEditorOpen}>
					<DialogContent
						className="max-w-2xl border-border bg-[linear-gradient(180deg,rgba(255,255,255,.99),rgba(236,253,245,.9))] sm:max-h-[90vh] overflow-hidden"
					>
						<DialogHeader>
							<DialogTitle className="text-2xl font-black text-foreground">
								编辑
							</DialogTitle>
						</DialogHeader>
						<div className="grid gap-5">
							<div className="space-y-4 rounded-[24px] border border-border bg-white/95 p-5">
								<div className="rounded-[22px] border border-primary/15 bg-[linear-gradient(180deg,rgba(236,253,245,.92),rgba(255,255,255,.96))] p-4">
									<div className="text-xs uppercase tracking-[0.22em] text-primary">
										配置面板
									</div>
									<div className="mt-2 text-sm leading-7 text-muted-foreground">
										调整 Skill 的名称、标识、说明和来源信息。
									</div>
								</div>
								<div className="grid gap-4 md:grid-cols-2">
									<div className="space-y-2">
										<Label>名称</Label>
										<Input
											value={skillForm.name}
											onChange={(event) =>
												setSkillForm((prev) => ({
													...prev,
													name: event.target.value,
												}))
											}
											placeholder="例如：代码授权审查"
										/>
									</div>
									<div className="space-y-2">
										<Label>Slug</Label>
										<Input
											value={skillForm.slug || ""}
											onChange={(event) =>
												setSkillForm((prev) => ({
													...prev,
													slug: event.target.value,
												}))
											}
											placeholder="留空将根据名称自动生成"
										/>
									</div>
								</div>
								<div className="space-y-2">
									<Label>描述</Label>
									<Textarea
										rows={5}
										value={skillForm.description}
										onChange={(event) =>
											setSkillForm((prev) => ({
												...prev,
												description: event.target.value,
											}))
										}
										placeholder="一句话说明这个 Skill 解决什么问题。"
									/>
								</div>
								<div className="grid gap-4 md:grid-cols-2">
									<div className="space-y-2">
										<Label>来源类型</Label>
										<Select
											value={skillForm.source_type || "manual"}
											onValueChange={(value) =>
												setSkillForm((prev) => ({
													...prev,
													source_type: value,
												}))
											}
										>
											<SelectTrigger>
												<SelectValue />
											</SelectTrigger>
											<SelectContent>
												<SelectItem value="manual">manual</SelectItem>
												<SelectItem value="github">github</SelectItem>
												<SelectItem value="local">local</SelectItem>
											</SelectContent>
										</Select>
									</div>
									<div className="space-y-2">
										<Label>来源 URL</Label>
										<Input
											value={skillForm.source_url || ""}
											onChange={(event) =>
												setSkillForm((prev) => ({
													...prev,
													source_url: event.target.value,
												}))
											}
											placeholder="GitHub 或文档链接，可选"
										/>
									</div>
								</div>
								<div className="flex justify-end gap-3 pt-2">
									<Button
										variant="outline"
										onClick={() => setEditorOpen(false)}
									>
										取消
									</Button>
									<Button className="cyber-btn-primary" onClick={saveSkill}>
										保存
									</Button>
								</div>
							</div>
						</div>
					</DialogContent>
				</Dialog>

				<Dialog open={importOpen} onOpenChange={setImportOpen}>
					<DialogContent className="max-w-[640px] overflow-hidden rounded-[28px] border border-slate-200 bg-white p-0 shadow-[0_28px_90px_rgba(15,23,42,0.22)]">
						<div className="border-b border-emerald-100 bg-[linear-gradient(135deg,rgba(240,253,244,0.96),rgba(255,255,255,0.98))] px-8 py-7">
							<DialogHeader>
								<div className="flex items-center gap-4">
									<div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-emerald-200 bg-white text-primary shadow-sm">
										<Link2 className="h-5 w-5" />
									</div>
									<DialogTitle className="text-2xl font-black text-foreground">
										导入 GitHub Skill
									</DialogTitle>
								</div>
							</DialogHeader>
						</div>
						<div className="space-y-5 px-8 py-7">
							<div className="rounded-[24px] border border-emerald-100 bg-[linear-gradient(180deg,rgba(236,253,245,0.68),rgba(255,255,255,0.96))] p-5">
								<div className="text-xs font-semibold uppercase tracking-[0.22em] text-primary">
									导入说明
								</div>
								<ul className="mt-4 grid gap-3">
									{[
										{
											text: (
												<>
													目录下至少需要存在
													<code className="mx-1 rounded bg-white px-1.5 py-0.5 text-xs text-slate-700 ring-1 ring-slate-200">
														SKILL.md
													</code>
													文件
												</>
											),
										},
										{
											text: (
												<>
													导入后会生成
													<code className="mx-1 rounded bg-white px-1.5 py-0.5 text-xs text-slate-700 ring-1 ring-slate-200">
														skill_library/&lt;skill-folder&gt;
													</code>
												</>
											),
										},
										{
											text: (
												<>
													绑定 Agent 时会同步写入
													<code className="mx-1 rounded bg-white px-1.5 py-0.5 text-xs text-slate-700 ring-1 ring-slate-200">
														agents/&lt;agent&gt;/bindings.json
													</code>
												</>
											),
										},
									].map((item, index) => (
										<li
											key={index}
											className="flex items-start gap-3 rounded-2xl bg-white/78 px-3 py-2.5 text-sm leading-7 text-slate-600 ring-1 ring-emerald-100/70"
										>
											<span className="mt-2 flex h-2.5 w-2.5 shrink-0 rounded-full bg-primary/70 shadow-[0_0_0_4px_rgba(16,185,129,0.10)]" />
											<span>{item.text}</span>
										</li>
									))}
								</ul>
							</div>
							<div className="rounded-[24px] border border-slate-200 bg-white p-5 shadow-[0_18px_52px_rgba(15,23,42,0.06)]">
								<div className="space-y-5">
									<div className="space-y-2">
										<Label>GitHub 仓库 URL</Label>
										<Input
											value={importUrl}
											onChange={(event) => setImportUrl(event.target.value)}
											placeholder="https://github.com/.../tree/main/skill-folder"
										/>
									</div>
									<div className="space-y-2">
										<Label>默认绑定 Agent</Label>
										<Select value={importAgent} onValueChange={setImportAgent}>
											<SelectTrigger>
												<SelectValue />
											</SelectTrigger>
											<SelectContent>
												{IMPORT_AGENT_OPTIONS.map((agent) => (
													<SelectItem key={agent.value} value={agent.value}>
														{agent.label}
													</SelectItem>
												))}
											</SelectContent>
										</Select>
									</div>
								</div>
							</div>
							<div className="flex justify-end gap-3 border-t border-slate-100 pt-5">
								<Button variant="outline" onClick={() => setImportOpen(false)}>
									取消
								</Button>
								<Button
									className="cyber-btn-primary"
									onClick={importSkill}
									disabled={!importUrl.trim()}
								>
									导入 Skill
								</Button>
							</div>
						</div>
					</DialogContent>
				</Dialog>

				<Dialog
					open={uploadOpen}
					onOpenChange={(open) => {
						setUploadOpen(open);
						if (!open) setUploadFile(null);
					}}
				>
					<DialogContent className="max-w-[620px] overflow-hidden rounded-[28px] border border-slate-200 bg-white p-0 shadow-[0_28px_90px_rgba(15,23,42,0.22)]">
						<div className="border-b border-emerald-100 bg-[linear-gradient(135deg,rgba(240,253,244,0.96),rgba(255,255,255,0.98))] px-8 py-7">
							<DialogHeader>
								<div className="flex items-center gap-4">
									<div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-emerald-200 bg-white text-primary shadow-sm">
										<FileArchive className="h-5 w-5" />
									</div>
									<DialogTitle className="text-2xl font-black text-foreground">
										上传 Skill
									</DialogTitle>
								</div>
							</DialogHeader>
						</div>
						<div className="space-y-5 px-8 py-7">
							<div className="rounded-[24px] border border-emerald-100 bg-[linear-gradient(180deg,rgba(236,253,245,0.68),rgba(255,255,255,0.96))] p-5">
								<div className="flex gap-4">
									<div className="mt-1 flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-primary/10 text-primary">
										<UploadCloud className="h-5 w-5" />
									</div>
									<p className="text-sm leading-7 text-slate-600">
										可上传 Skill 压缩包，或直接将 Skill 文件夹移至
										<code className="mx-1 rounded bg-white px-1.5 py-0.5 text-xs text-slate-700 ring-1 ring-slate-200">
											[项目根目录]/skill_library/
										</code>
										目录。
									</p>
								</div>
							</div>
							<label className="group flex cursor-pointer flex-col items-center justify-center rounded-[26px] border border-dashed border-emerald-200 bg-[linear-gradient(180deg,rgba(255,255,255,0.98),rgba(248,250,252,0.94))] px-6 py-10 text-center transition hover:border-primary/50 hover:bg-emerald-50/40">
								<div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 text-primary transition group-hover:scale-105">
									<FileArchive className="h-6 w-6" />
								</div>
								<div className="mt-4 text-base font-semibold text-foreground">
									{uploadFile ? uploadFile.name : "选择 ZIP 文件"}
								</div>
								<div className="mt-2 text-sm text-muted-foreground">
									仅支持 .zip 格式
								</div>
								<Input
									type="file"
									accept=".zip,application/zip,application/x-zip-compressed"
									className="sr-only"
									onChange={(event) =>
										setUploadFile(event.target.files?.[0] ?? null)
									}
								/>
							</label>
							<div className="flex justify-end gap-3 border-t border-slate-100 pt-5">
								<Button variant="outline" onClick={() => setUploadOpen(false)}>
									取消
								</Button>
								<Button
									className="cyber-btn-primary"
									onClick={uploadSkillArchive}
									disabled={!uploadFile || uploading}
								>
									{uploading ? (
										<RefreshCw className="mr-2 h-4 w-4 animate-spin" />
									) : (
										<UploadCloud className="mr-2 h-4 w-4" />
									)}
									上传 Skill
								</Button>
							</div>
						</div>
					</DialogContent>
				</Dialog>

				<AlertDialog
					open={Boolean(pendingDeleteSkill)}
					onOpenChange={(open) => {
						if (!open && !deletingSkill) setPendingDeleteSkill(null);
					}}
				>
					<AlertDialogContent className="border-border bg-white">
						<AlertDialogHeader>
							<AlertDialogTitle className="text-foreground">
								确认删除 Skill
							</AlertDialogTitle>
							<AlertDialogDescription className="font-sans leading-7 text-muted-foreground">
								此操作会删除 Skill 文件夹、SKILL.md、扩展资源以及所有 Agent
								绑定记录。确认删除
								<span className="mx-1 font-semibold text-foreground">
									{pendingDeleteSkill?.name}
								</span>
								后无法在页面内撤销。
							</AlertDialogDescription>
						</AlertDialogHeader>
						<AlertDialogFooter>
							<AlertDialogCancel disabled={deletingSkill}>
								取消
							</AlertDialogCancel>
							<AlertDialogAction
								className="cyber-btn-primary"
								disabled={deletingSkill}
								onClick={(event) => {
									event.preventDefault();
									confirmDeleteSkill();
								}}
							>
								{deletingSkill ? "删除中..." : "确认删除"}
							</AlertDialogAction>
						</AlertDialogFooter>
					</AlertDialogContent>
				</AlertDialog>
			</div>
		</div>
	);
}
