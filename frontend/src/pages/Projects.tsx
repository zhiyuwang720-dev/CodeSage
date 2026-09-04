/**
 * Projects Page
 * Cyberpunk Terminal Aesthetic
 */

import { useState, useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Progress } from "@/components/ui/progress";
import {
  Plus,
  Search,
  GitBranch,
  Loader2,
  Calendar,
  Users,
  Code,
  Shield,
  Activity,
  Upload,
  FileText,
  AlertCircle,
  Trash2,
  Edit,
  CheckCircle,
  Terminal,
  Github,
  Folder,
  ArrowUpRight,
  Key,
  HardDrive
} from "lucide-react";
import { api } from "@/shared/config/database";
import { validateZipFile } from "@/features/projects/services";
import type { Project, CreateProjectForm } from "@/shared/types";
import {
  createAgentTask,
  getAgentTasks,
  type AgentTask,
} from "@/shared/api/agentTasks";
import {
  deleteProjectSourceArtifacts,
  formatFileSize,
  getZipFileInfo,
  type ZipFileMeta,
  uploadZipFile,
  waitForZipImport,
} from "@/shared/utils/zipStorage";
import { isLocalDirectoryProject, isRepositoryProject, isZipProject, isPrReviewProject, getSourceTypeBadge } from "@/shared/utils/projectUtils";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import CreateTaskDialog from "@/components/audit/CreateTaskDialog";
import { SUPPORTED_LANGUAGES, REPOSITORY_PLATFORMS } from "@/shared/constants";

export default function Projects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState("");
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [showCreateTaskDialog, setShowCreateTaskDialog] = useState(false);
  const [selectedProjectForTask, setSelectedProjectForTask] = useState<string>("");
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [creatingProject, setCreatingProject] = useState(false);
  const [projectImportMessage, setProjectImportMessage] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [projectToDelete, setProjectToDelete] = useState<Project | null>(null);
  const [showEditDialog, setShowEditDialog] = useState(false);
  const [projectToEdit, setProjectToEdit] = useState<Project | null>(null);
  const [editForm, setEditForm] = useState<CreateProjectForm>({
    name: "",
    description: "",
    source_type: "repository",
    repository_url: "",
    repository_type: "github",
    default_branch: "main",
    programming_languages: []
  });
  const [createForm, setCreateForm] = useState<CreateProjectForm>({
    name: "",
    description: "",
    source_type: "pr_review",
    repository_url: "",
    repository_type: "github",
    default_branch: "main",
    programming_languages: []
  });
  // pr_review 项目自动创建的待运行任务(项目管理卡片显示其状态)
  const [projectTasks, setProjectTasks] = useState<Record<string, AgentTask | undefined>>({});
  const [createBranches, setCreateBranches] = useState<string[]>([]);
  const [loadingCreateBranches, setLoadingCreateBranches] = useState(false);
  const [createBranchError, setCreateBranchError] = useState<string | null>(null);

  const [selectedFile, setSelectedFile] = useState<File | null>(null);

  // 编辑对话框中的ZIP文件状态
  const [editZipInfo, setEditZipInfo] = useState<ZipFileMeta | null>(null);
  const [editZipFile, setEditZipFile] = useState<File | null>(null);
  const [loadingEditZipInfo, setLoadingEditZipInfo] = useState(false);
  const [editKeepZipArchive, setEditKeepZipArchive] = useState(true);
  const [deletingZipArtifacts, setDeletingZipArtifacts] = useState(false);
  const editZipInputRef = useRef<HTMLInputElement>(null);

  // 将小写语言名转换为显示格式
  const formatLanguageName = (lang: string): string => {
    const nameMap: Record<string, string> = {
      'javascript': 'JavaScript',
      'typescript': 'TypeScript',
      'python': 'Python',
      'java': 'Java',
      'go': 'Go',
      'rust': 'Rust',
      'cpp': 'C++',
      'csharp': 'C#',
      'php': 'PHP',
      'ruby': 'Ruby',
      'swift': 'Swift',
      'kotlin': 'Kotlin'
    };
    return nameMap[lang] || lang.charAt(0).toUpperCase() + lang.slice(1);
  };

  const supportedLanguages = SUPPORTED_LANGUAGES.map(formatLanguageName);

  useEffect(() => {
    loadProjects();
  }, []);

  useEffect(() => {
    if (!showCreateDialog || createForm.source_type !== "repository") {
      setCreateBranches([]);
      setCreateBranchError(null);
      setLoadingCreateBranches(false);
      return;
    }

    const repositoryUrl = createForm.repository_url?.trim();
    const repositoryType = createForm.repository_type || "github";
    if (!repositoryUrl || repositoryType !== "github") {
      setCreateBranches([]);
      setCreateBranchError(null);
      setLoadingCreateBranches(false);
      return;
    }

    let cancelled = false;
    setLoadingCreateBranches(true);
    setCreateBranchError(null);

    const timer = window.setTimeout(async () => {
      const result = await api.lookupRepositoryBranches({
        repository_url: repositoryUrl,
        repository_type: repositoryType,
      });
      if (cancelled) {
        return;
      }

      if (result.error) {
        setCreateBranches([]);
        setCreateBranchError(result.error);
      } else {
        setCreateBranches(result.branches);
        if (result.default_branch) {
          setCreateForm((previous) => ({
            ...previous,
            default_branch: result.default_branch,
          }));
        }
      }
      setLoadingCreateBranches(false);
    }, 500);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [showCreateDialog, createForm.source_type, createForm.repository_type, createForm.repository_url]);

  const loadProjects = async () => {
    try {
      setLoading(true);
      const data = await api.getProjects();
      setProjects(data);

      // 为 pr_review 项目拉取自动创建的待运行任务(项目管理卡片展示其状态)
      const prProjects = data.filter((p) => isPrReviewProject(p));
      const tasksMap: Record<string, AgentTask | undefined> = {};
      await Promise.all(
        prProjects.map(async (p) => {
          try {
            const tasks = await getAgentTasks({ project_id: p.id, limit: 1 });
            tasksMap[p.id] = tasks[0];
          } catch (e) {
            console.error(`[Projects] 加载项目 ${p.id} 的 PR 审查任务失败:`, e);
          }
        }),
      );
      setProjectTasks(tasksMap);
    } catch (error) {
      console.error('Failed to load projects:', error);
      toast.error("加载项目失败");
    } finally {
      setLoading(false);
    }
  };

  const handleCreateProject = async () => {
    if (!createForm.name.trim()) {
      toast.error("请输入项目名称");
      return;
    }

    const prUrl = createForm.repository_url?.trim();
    if (!prUrl) {
      toast.error("请输入 PR 链接");
      return;
    }

    try {
      setCreatingProject(true);
      setProjectImportMessage("正在保存 PR 项目并创建待运行的审查任务...");
      const project = await api.createProject({
        ...createForm,
        source_type: "pr_review",
        repository_url: prUrl,
        repository_type: createForm.repository_type || "github",
      } as any);

      // 执行创建 = 保存任务(不自动运行); 点审计 -> 智能审计 -> 启动
      await createAgentTask({
        project_id: project.id,
        name: `PR Review - ${createForm.name}`,
        version_label: "pr-review",
        finding_runtime_stack: "runtime",
        audit_scope: {
          pr_review: {
            pr_url: prUrl,
          },
        },
      });

      import('@/shared/utils/logger').then(({ logger }) => {
        logger.logUserAction('创建 PR 审查项目', {
          projectName: createForm.name,
          prUrl,
          repositoryType: createForm.repository_type,
          languages: createForm.programming_languages,
        });
      });

      toast.success("项目创建成功", {
        description: "已创建待运行的 PR 审查任务，点击「审计」→「智能审计」后启动",
        duration: 5000
      });
      setShowCreateDialog(false);
      resetCreateForm();
      loadProjects();
    } catch (error) {
      console.error('Failed to create project:', error);
      import('@/shared/utils/errorHandler').then(({ handleError }) => {
        handleError(error, '创建项目失败');
      });
      const errorMessage = error instanceof Error ? error.message : '未知错误';
      toast.error(`创建项目失败: ${errorMessage}`);
    } finally {
      setCreatingProject(false);
      setProjectImportMessage("");
    }
  };

  const resetCreateForm = () => {
    setCreateForm({
      name: "",
      description: "",
      source_type: "pr_review",
      repository_url: "",
      repository_type: "github",
      local_path: "",
      workspace_mode: "",
      default_branch: "main",
      programming_languages: []
    });
    setCreateBranches([]);
    setCreateBranchError(null);
    setLoadingCreateBranches(false);
    setProjectImportMessage("");
    setSelectedFile(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  // 校验上传的 diff 文件(.diff/.patch/.txt, ≤50MB)
  const validateDiffFile = (file: File): { valid: boolean; error?: string } => {
    const lower = file.name.toLowerCase();
    const isDiff = lower.endsWith('.diff') || lower.endsWith('.patch') || lower.endsWith('.txt');
    if (!isDiff) {
      return { valid: false, error: '仅支持 .diff / .patch / .txt 格式的补丁文件' };
    }
    const maxSize = 50 * 1024 * 1024;
    if (file.size > maxSize) {
      return { valid: false, error: 'diff 文件不能超过 50MB' };
    }
    return { valid: true };
  };

  const handleFileSelect = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    const validation = validateDiffFile(file);
    if (!validation.valid) {
      toast.error(validation.error);
      event.target.value = '';
      return;
    }

    setSelectedFile(file);
    event.target.value = '';
  };

  const handleUploadAndCreate = async () => {
    if (!selectedFile) {
      toast.error("请先选择 diff 文件");
      return;
    }

    if (!createForm.name.trim()) {
      toast.error("请先输入项目名称");
      return;
    }

    try {
      setUploading(true);
      setProjectImportMessage("正在创建 PR 项目记录...");
      setUploadProgress(10);

      const project = await api.createProject({
        ...createForm,
        source_type: "pr_review",
        repository_type: "other",
        repository_url: undefined
      } as any);

      try {
        setProjectImportMessage("正在上传 diff 源码...");
        setUploadProgress(40);
        const uploadResult = await api.uploadProjectDiff(project.id, selectedFile);
        setUploadProgress(80);

        // 执行创建 = 保存任务(不自动运行); 点审计 -> 智能审计 -> 启动
        setProjectImportMessage("正在创建待运行的审查任务...");
        await createAgentTask({
          project_id: project.id,
          name: `PR Review - ${createForm.name}`,
          version_label: "pr-review",
          finding_runtime_stack: "runtime",
          audit_scope: {
            pr_review: {
              diff_file_path: uploadResult.diff_file_path,
            },
          },
        });
      } catch (error) {
        console.error('保存 diff 文件失败:', error);
        throw error;
      }

      setUploadProgress(100);

      import('@/shared/utils/logger').then(({ logger }) => {
        logger.logUserAction('上传 diff 创建项目', {
          projectName: project.name,
          fileName: selectedFile.name,
          fileSize: selectedFile.size,
        });
      });

      setShowCreateDialog(false);
      resetCreateForm();
      loadProjects();

      toast.success(`项目 "${project.name}" 已创建`, {
        description: 'diff 已保存，已创建待运行的 PR 审查任务，点击「审计」→「智能审计」后启动',
        duration: 5000
      });

    } catch (error: any) {
      console.error('Upload failed:', error);
      import('@/shared/utils/errorHandler').then(({ handleError }) => {
        handleError(error, '上传 diff 文件失败');
      });
      const errorMessage = error?.message || '未知错误';
      toast.error(`上传失败: ${errorMessage}`);
    } finally {
      setUploading(false);
      setProjectImportMessage("");
      setUploadProgress(0);
    }
  };

  const filteredProjects = projects.filter(project =>
    project.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
    project.description?.toLowerCase().includes(searchTerm.toLowerCase())
  );

  const getRepositoryIcon = (type?: string) => {
    switch (type) {
      case 'github': return <Github className="w-5 h-5" />;
      case 'gitlab': return <GitBranch className="w-5 h-5 text-orange-500" />;
      case 'gitea': return <GitBranch className="w-5 h-5 text-green-600" />;
      case 'other': return <Key className="w-5 h-5 text-cyan-500" />;
      default: return <Folder className="w-5 h-5 text-muted-foreground" />;
    }
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString('zh-CN');
  };

  const handleCreateTask = (projectId: string) => {
    setSelectedProjectForTask(projectId);
    setShowCreateTaskDialog(true);
  };

  const handleEditClick = async (project: Project) => {
    setProjectToEdit(project);
    setEditForm({
      name: project.name,
      description: project.description || "",
      source_type: project.source_type || "repository",
      repository_url: project.repository_url || "",
      repository_type: project.repository_type || "github",
      local_path: project.local_path || "",
      workspace_mode: project.workspace_mode || "",
      default_branch: project.default_branch || "main",
      programming_languages: project.programming_languages ? JSON.parse(project.programming_languages) : []
    });
    setEditZipFile(null);
    setEditZipInfo(null);
    setShowEditDialog(true);

    if (project.source_type === 'zip') {
      setLoadingEditZipInfo(true);
      try {
        const zipInfo = await getZipFileInfo(project.id);
        setEditZipInfo(zipInfo);
        setEditKeepZipArchive(Boolean(zipInfo.has_file));
      } catch (error) {
        console.error('加载ZIP文件信息失败:', error);
      } finally {
        setLoadingEditZipInfo(false);
      }
    }
  };

  const refreshEditZipInfo = async (projectId: string) => {
    setLoadingEditZipInfo(true);
    try {
      const zipInfo = await getZipFileInfo(projectId);
      setEditZipInfo(zipInfo);
      setEditKeepZipArchive(Boolean(zipInfo.has_file));
    } catch (error) {
      console.error("Failed to load ZIP file info:", error);
      toast.error("加载源码归档信息失败");
    } finally {
      setLoadingEditZipInfo(false);
    }
  };

  const handleDeleteZipArtifacts = async (
    deleteZip: boolean,
    deletePersistentSource: boolean,
  ) => {
    if (!projectToEdit) {
      return;
    }

    const targetLabel = deleteZip && deletePersistentSource
      ? "the original ZIP archive and persistent source directory"
      : deleteZip
        ? "the original ZIP archive"
        : "the persistent source directory";

    if (!window.confirm(`Delete ${targetLabel}? This cannot be undone.`)) {
      return;
    }

    try {
      setDeletingZipArtifacts(true);
      const result = await deleteProjectSourceArtifacts(projectToEdit.id, {
        deleteZip,
        deletePersistentSource,
      });

      await refreshEditZipInfo(projectToEdit.id);
      await loadProjects();

      toast.success("Project source artifacts deleted", {
        description: [
          result.deleted_zip ? "ZIP archive removed" : null,
          result.deleted_persistent_source ? "Persistent source directory removed" : null,
        ].filter(Boolean).join(", "),
      });

      if (result.deleted_persistent_source) {
        setEditForm((current) => ({
          ...current,
          local_path: "",
        }));
      }
    } catch (error) {
      console.error("Failed to delete project source artifacts:", error);
      toast.error("Failed to delete project source artifacts");
    } finally {
      setDeletingZipArtifacts(false);
    }
  };

  const handleSaveEdit = async () => {
    if (!projectToEdit) return;

    if (!editForm.name.trim()) {
      toast.error("Project name cannot be empty");
      return;
    }

    try {
      await api.updateProject(projectToEdit.id, editForm);

      if (editZipFile && editForm.source_type === "zip") {
        const result = await uploadZipFile(projectToEdit.id, editZipFile, {
          keepArchive: editKeepZipArchive,
        });
        if (result.success) {
          await waitForZipImport(projectToEdit.id);
          toast.success(`ZIP file updated: ${result.original_filename}`);
          setEditZipFile(null);
          await refreshEditZipInfo(projectToEdit.id);
        } else {
          toast.error(`ZIP upload failed: ${result.message}`);
          return;
        }
      }

      toast.success(`Project "${editForm.name}" updated`);
      setShowEditDialog(false);
      setProjectToEdit(null);
      setEditZipFile(null);
      setEditZipInfo(null);
      loadProjects();
    } catch (error) {
      console.error("Failed to update project:", error);
      toast.error("Failed to update project");
    }
  };

  const handleToggleLanguage = (lang: string) => {
    const currentLanguages = editForm.programming_languages || [];
    const newLanguages = currentLanguages.includes(lang)
      ? currentLanguages.filter(l => l !== lang)
      : [...currentLanguages, lang];

    setEditForm({ ...editForm, programming_languages: newLanguages });
  };

  const handleDeleteClick = (project: Project) => {
    setProjectToDelete(project);
    setShowDeleteDialog(true);
  };

  const handleConfirmDelete = async () => {
    if (!projectToDelete) return;

    try {
      await api.deleteProject(projectToDelete.id);

      import('@/shared/utils/logger').then(({ logger }) => {
        logger.logUserAction('删除项目', {
          projectId: projectToDelete.id,
          projectName: projectToDelete.name,
        });
      });

      toast.success(`项目 "${projectToDelete.name}" 已永久删除`, {
        description: '项目记录和关联审计数据已删除，无法从回收站恢复',
        duration: 4000
      });
      setShowDeleteDialog(false);
      setProjectToDelete(null);
      loadProjects();
    } catch (error) {
      console.error('Failed to delete project:', error);
      import('@/shared/utils/errorHandler').then(({ handleError }) => {
        handleError(error, '删除项目失败');
      });
      const errorMessage = error instanceof Error ? error.message : '未知错误';
      toast.error(`删除项目失败: ${errorMessage}`);
    }
  };

  const handleTaskCreated = () => {
    toast.success("PR 审查任务已创建", {
      description: '因为网络和代码文件大小等因素，审查时长通常至少需要1分钟，请耐心等待...',
      duration: 5000
    });
  };

  // pr_review 自动任务的状态徽标(项目管理卡片展示)
  const renderPrTaskStatus = (task: AgentTask) => {
    const map: Record<string, { label: string; className: string }> = {
      pending: { label: '待运行', className: 'cyber-badge-muted' },
      running: { label: '运行中', className: 'cyber-badge-success' },
      completed: { label: '已完成', className: 'cyber-badge-info' },
      failed: { label: '失败', className: 'cyber-badge-warning' },
      cancelled: { label: '已取消', className: 'cyber-badge-muted' },
    };
    const item = map[task.status] || { label: task.status, className: 'cyber-badge-muted' };
    return <Badge className={`cyber-badge ${item.className}`}>{item.label}</Badge>;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="text-center space-y-4">
          <div className="loading-spinner mx-auto" />
          <p className="text-muted-foreground text-sm">正在加载项目工作台...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative min-h-screen space-y-6 overflow-hidden rounded-[34px] border border-white/65 bg-white/45 p-6 shadow-[0_24px_70px_rgba(88,97,110,0.10)] backdrop-blur-xl">
      {/* Grid background */}
      <div className="absolute inset-0 cyber-grid-subtle pointer-events-none" />

      {/* 创建项目对话框 */}
      <Dialog open={showCreateDialog} onOpenChange={setShowCreateDialog}>
        <DialogTrigger asChild className="hidden">
          <Button className="cyber-btn-primary">
            <Plus className="w-5 h-5 mr-2" />
            初始化项目
          </Button>
        </DialogTrigger>
        <DialogContent className="!w-[min(92vw,760px)] !max-w-none max-h-[88vh] overflow-hidden flex flex-col gap-0 rounded-[28px] border border-slate-200/80 bg-white/95 p-0 shadow-[0_30px_90px_rgba(15,23,42,0.22)] backdrop-blur-xl">
          <DialogHeader className="flex-shrink-0 border-b border-slate-200/70 bg-[radial-gradient(circle_at_12%_12%,rgba(52,211,153,0.16),transparent_32%),linear-gradient(135deg,rgba(240,253,244,0.98),rgba(255,255,255,0.96)_58%,rgba(248,250,252,0.98))] px-7 py-6">
            <DialogTitle className="flex items-center gap-3 text-2xl font-black tracking-tight text-slate-950">
              <span className="flex h-11 w-11 items-center justify-center rounded-2xl border border-emerald-200 bg-emerald-50 text-emerald-700 shadow-sm">
                <Terminal className="h-5 w-5" />
              </span>
              <span>初始化新项目</span>
            </DialogTitle>
          </DialogHeader>

          <div className="flex-1 overflow-y-auto bg-slate-50/70 p-6">
            <Tabs
              defaultValue="pr_url"
              className="w-full space-y-5"
              onValueChange={(value) => {
                // 两个 tab 都产出 pr_review 项目; 差异在输入通道(PR URL / 上传 diff)
                if (value === "pr_url" || value === "diff") {
                  setCreateForm((previous) => ({ ...previous, source_type: "pr_review" }));
                }
              }}
            >
              <TabsList className="grid h-auto w-full grid-cols-2 gap-2 rounded-2xl border border-slate-200 bg-white p-1.5 shadow-sm">
                <TabsTrigger
                  value="pr_url"
                  className="h-11 rounded-xl text-sm font-bold text-slate-500 transition-all data-[state=active]:bg-emerald-700 data-[state=active]:text-white data-[state=active]:shadow-[0_12px_24px_rgba(5,150,105,0.22)]"
                >
                  <GitBranch className="w-4 h-4 mr-2" />
                  PR URL
                </TabsTrigger>
                <TabsTrigger
                  value="diff"
                  className="h-11 rounded-xl text-sm font-bold text-slate-500 transition-all data-[state=active]:bg-emerald-700 data-[state=active]:text-white data-[state=active]:shadow-[0_12px_24px_rgba(5,150,105,0.22)]"
                >
                  <Upload className="w-4 h-4 mr-2" />
                  上传 diff
                </TabsTrigger>
              </TabsList>

              <TabsContent value="pr_url" className="m-0 space-y-5 rounded-[24px] border border-slate-200 bg-white p-5 shadow-sm data-[state=inactive]:hidden">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="name" className="text-xs font-bold text-slate-600">项目名称 *</Label>
                    <Input
                      id="name"
                      value={createForm.name}
                      onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
                      placeholder="输入项目名称"
                      className="h-11 rounded-2xl border-slate-200 bg-slate-50/80 shadow-inner focus-visible:ring-emerald-600"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="repository_type" className="text-xs font-bold text-slate-600">认证类型</Label>
                    <Select
                      value={createForm.repository_type}
                      onValueChange={(value: any) => setCreateForm({ ...createForm, repository_type: value })}
                    >
                      <SelectTrigger className="h-11 rounded-2xl border-slate-200 bg-slate-50/80 shadow-inner focus:ring-emerald-600">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="rounded-2xl border-slate-200 bg-white">
                        {REPOSITORY_PLATFORMS.map((platform) => (
                          <SelectItem key={platform.value} value={platform.value}>
                            {platform.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="description" className="text-xs font-bold text-slate-600">描述</Label>
                  <Textarea
                    id="description"
                    value={createForm.description}
                    onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
                    placeholder="// 项目描述..."
                    rows={3}
                    className="min-h-[92px] rounded-2xl border-slate-200 bg-slate-50/80 shadow-inner focus-visible:ring-emerald-600"
                  />
                </div>

                <div className="space-y-1.5">
                  <Label htmlFor="repository_url" className="text-xs font-bold text-slate-600">PR 链接 *</Label>
                  <Input
                    id="repository_url"
                    value={createForm.repository_url}
                    onChange={(e) => setCreateForm({ ...createForm, repository_url: e.target.value })}
                    placeholder="https://github.com/user/repo/pull/123"
                    className="h-11 rounded-2xl border-slate-200 bg-slate-50/80 shadow-inner focus-visible:ring-emerald-600"
                  />
                  <p className="text-xs text-slate-500">
                    填入待审查的 PR 页面地址，PR 3-Agent 会克隆该 PR 分支并分析 diff
                  </p>
                </div>

                <div className="space-y-2">
                  <Label className="text-xs font-bold text-slate-600">技术栈</Label>
                  <div className="flex flex-wrap gap-2">
                    {supportedLanguages.map((lang) => (
                      <label key={lang} className={`flex cursor-pointer items-center gap-2 rounded-xl border px-3 py-2 text-xs font-bold transition-all ${createForm.programming_languages.includes(lang)
                        ? 'border-emerald-300 bg-emerald-50 text-emerald-800 shadow-sm'
                        : 'border-slate-200 bg-white text-slate-500 hover:border-emerald-200 hover:bg-emerald-50/40'
                        }`}>
                        <input
                          type="checkbox"
                          checked={createForm.programming_languages.includes(lang)}
                          onChange={(e) => {
                            if (e.target.checked) {
                              setCreateForm({
                                ...createForm,
                                programming_languages: [...createForm.programming_languages, lang]
                              });
                            } else {
                              setCreateForm({
                                ...createForm,
                                programming_languages: createForm.programming_languages.filter(l => l !== lang)
                              });
                            }
                          }}
                          className="h-3.5 w-3.5 rounded border-slate-300 text-emerald-700 focus:ring-emerald-600"
                        />
                        <span>{lang}</span>
                      </label>
                    ))}
                  </div>
                </div>

                {creatingProject && projectImportMessage && (
                  <div className="flex items-center gap-2 rounded-2xl border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs font-medium text-emerald-700">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>{projectImportMessage}</span>
                  </div>
                )}

                <div className="flex justify-end gap-3 border-t border-slate-200 pt-4">
                  <Button variant="outline" onClick={() => setShowCreateDialog(false)} className="rounded-2xl border-slate-200 bg-white px-6 shadow-sm" disabled={creatingProject}>
                    取消
                  </Button>
                  <Button onClick={handleCreateProject} className="rounded-2xl bg-emerald-700 px-7 font-bold text-white shadow-[0_14px_30px_rgba(5,150,105,0.22)] hover:bg-emerald-800" disabled={creatingProject}>
                    执行创建
                  </Button>
                </div>
              </TabsContent>

              <TabsContent value="diff" className="m-0 space-y-5 rounded-[24px] border border-slate-200 bg-white p-5 shadow-sm data-[state=inactive]:hidden">
                <div className="flex flex-col gap-4">
                  <div className="space-y-1.5">
                    <Label htmlFor="upload-name" className="text-xs font-bold text-slate-600">项目名称 *</Label>
                    <Input
                      id="upload-name"
                      value={createForm.name}
                      onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
                      placeholder="输入项目名称"
                      className="h-11 rounded-2xl border-slate-200 bg-slate-50/80 shadow-inner focus-visible:ring-emerald-600"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="upload-description" className="text-xs font-bold text-slate-600">描述</Label>
                    <Textarea
                      id="upload-description"
                      value={createForm.description}
                      onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
                      placeholder="// 项目描述..."
                      rows={3}
                      className="min-h-[82px] rounded-2xl border-slate-200 bg-slate-50/80 shadow-inner focus-visible:ring-emerald-600"
                    />
                  </div>
                </div>

                <div className="space-y-2">
                  <Label className="text-xs font-bold text-slate-600">技术栈</Label>
                  <div className="flex flex-wrap gap-2">
                    {supportedLanguages.map((lang) => (
                      <label key={lang} className={`flex cursor-pointer items-center gap-2 rounded-xl border px-3 py-2 text-xs font-bold transition-all ${createForm.programming_languages.includes(lang)
                        ? 'border-emerald-300 bg-emerald-50 text-emerald-800 shadow-sm'
                        : 'border-slate-200 bg-white text-slate-500 hover:border-emerald-200 hover:bg-emerald-50/40'
                        }`}>
                        <input
                          type="checkbox"
                          checked={createForm.programming_languages.includes(lang)}
                          onChange={(e) => {
                            if (e.target.checked) {
                              setCreateForm({
                                ...createForm,
                                programming_languages: [...createForm.programming_languages, lang]
                              });
                            } else {
                              setCreateForm({
                                ...createForm,
                                programming_languages: createForm.programming_languages.filter(l => l !== lang)
                              });
                            }
                          }}
                          className="h-3.5 w-3.5 rounded border-slate-300 text-emerald-700 focus:ring-emerald-600"
                        />
                        <span>{lang}</span>
                      </label>
                    ))}
                  </div>
                </div>

                <div className="flex flex-col gap-4">
                  <div className="space-y-3">
                    <Label className="text-xs font-bold text-slate-600">源代码</Label>

                  {!selectedFile ? (
                    <div
                      className="group cursor-pointer rounded-[22px] border border-dashed border-emerald-300 bg-[linear-gradient(135deg,rgba(236,253,245,0.78),rgba(248,250,252,0.88))] p-6 text-center transition-all hover:border-emerald-500 hover:bg-emerald-50"
                      onClick={() => fileInputRef.current?.click()}
                    >
                      <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl border border-emerald-200 bg-white text-emerald-700 shadow-sm">
                        <Upload className="h-6 w-6" />
                      </div>
                      <h3 className="mb-1 text-base font-black text-slate-950">上传 diff 文件</h3>
                      <p className="mb-4 text-xs font-medium text-slate-500">
                        最大 50MB / 格式 .diff .patch
                      </p>
                      <input
                        ref={fileInputRef}
                        type="file"
                        accept=".diff,.patch,.txt"
                        onChange={handleFileSelect}
                        className="hidden"
                        disabled={uploading}
                      />
                      <Button
                        type="button"
                        variant="outline"
                        className="h-9 rounded-2xl border-slate-200 bg-white px-5 text-xs font-bold shadow-sm"
                        disabled={uploading || !createForm.name.trim()}
                        onClick={(e) => {
                          e.stopPropagation();
                          fileInputRef.current?.click();
                        }}
                      >
                        <FileText className="w-3 h-3 mr-2" />
                        选择文件
                      </Button>
                    </div>
                  ) : (
                    <div className="flex items-center justify-between rounded-[22px] border border-emerald-200 bg-emerald-50/70 p-4">
                      <div className="flex items-center space-x-3 overflow-hidden">
                        <div className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-2xl border border-emerald-200 bg-white">
                          <FileText className="h-5 w-5 text-emerald-700" />
                        </div>
                        <div className="min-w-0">
                          <p className="truncate text-sm font-bold text-slate-950">{selectedFile.name}</p>
                          <p className="text-xs text-slate-500">{(selectedFile.size / 1024 / 1024).toFixed(2)} MB</p>
                        </div>
                      </div>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => setSelectedFile(null)}
                        disabled={uploading}
                        className="hover:bg-rose-500/10 hover:text-rose-400"
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  )}

                  {uploading && (
                    <div className="space-y-1.5">
                      <div className="flex items-center justify-between text-xs font-medium text-slate-500">
                        <span>{projectImportMessage || "上传并分析中..."}</span>
                        <span className="text-emerald-700">{uploadProgress}%</span>
                      </div>
                      <Progress value={uploadProgress} className="h-2 bg-slate-100 [&>div]:bg-emerald-700" />
                    </div>
                  )}
                  </div>

                  <div className="rounded-[22px] border border-slate-200 bg-slate-50/85 p-4">
                    <div className="flex items-start gap-3">
                      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-2xl border border-emerald-200 bg-white text-emerald-700">
                        <AlertCircle className="h-4 w-4" />
                      </span>
                      <div className="text-xs leading-6 text-slate-600">
                        <p className="mb-2 font-bold text-slate-800">diff 审查说明</p>
                        <ul className="space-y-2">
                          <li className="flex gap-2">
                            <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />
                            <span>上传 Git 导出的 diff / patch 文件（git diff 或 PR 的 .diff 下载）</span>
                          </li>
                          <li className="flex gap-2">
                            <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-sky-500" />
                            <span>PR 3-Agent 将直接基于 diff 内容做安全/架构/质量三视角审查</span>
                          </li>
                          <li className="flex gap-2">
                            <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400" />
                            <span>审查评论会写入该任务的 findings，可在任务详情查看</span>
                          </li>
                        </ul>
                      </div>
                    </div>
                  </div>
                </div>

                <div className="mt-auto flex justify-end gap-3 border-t border-slate-200 pt-4">
                  <Button variant="outline" onClick={() => setShowCreateDialog(false)} disabled={uploading} className="rounded-2xl border-slate-200 bg-white px-6 shadow-sm">
                    取消
                  </Button>
                  <Button
                    onClick={handleUploadAndCreate}
                    className="rounded-2xl bg-emerald-700 px-7 font-bold text-white shadow-[0_14px_30px_rgba(5,150,105,0.22)] hover:bg-emerald-800"
                    disabled={!selectedFile || uploading}
                  >
                    {uploading ? '上传中...' : '执行创建'}
                  </Button>
                </div>
              </TabsContent>

            </Tabs>
          </div>
        </DialogContent>
      </Dialog>

      {/* Stats Section */}
      {projects.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-4 relative z-10">
          <div className="cyber-card p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="stat-label">项目总数</p>
                <p className="stat-value">{projects.length}</p>
              </div>
              <div className="stat-icon text-primary">
                <Code className="w-6 h-6" />
              </div>
            </div>
          </div>

          <div className="cyber-card p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="stat-label">活跃项目</p>
                <p className="stat-value">{projects.filter(p => p.is_active).length}</p>
              </div>
              <div className="stat-icon text-emerald-400">
                <Activity className="w-6 h-6" />
              </div>
            </div>
          </div>

          <div className="cyber-card p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="stat-label">远程仓库</p>
                <p className="stat-value">{projects.filter(p => isRepositoryProject(p)).length}</p>
              </div>
              <div className="stat-icon text-sky-400">
                <GitBranch className="w-6 h-6" />
              </div>
            </div>
          </div>

          <div className="cyber-card p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="stat-label">ZIP上传</p>
                <p className="stat-value">{projects.filter(p => isZipProject(p)).length}</p>
              </div>
              <div className="stat-icon text-amber-400">
                <Upload className="w-6 h-6" />
              </div>
            </div>
          </div>

          <div className="cyber-card p-4">
            <div className="flex items-center justify-between">
              <div>
                <p className="stat-label">本地目录</p>
                <p className="stat-value">{projects.filter(p => isLocalDirectoryProject(p)).length}</p>
              </div>
              <div className="stat-icon text-violet-400">
                <HardDrive className="w-6 h-6" />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Search and Filter */}
      <div className="cyber-card relative z-10 flex flex-col gap-4 p-5 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 text-muted-foreground w-4 h-4 z-10" />
          <Input
            placeholder="搜索项目..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="cyber-input !pl-10"
          />
        </div>
        <Button className="cyber-btn-primary h-10" onClick={() => setShowCreateDialog(true)}>
          <Plus className="w-4 h-4 mr-2" />
          新建项目
        </Button>
      </div>

      {/* Project List */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 relative z-10">
        {filteredProjects.length > 0 ? (
          filteredProjects.map((project) => (
            <div key={project.id} className="cyber-card flex flex-col h-full group">
              {/* Card Header */}
              <div className="p-4 border-b border-border bg-muted/50 flex justify-between items-start">
                <div className="flex items-center space-x-3">
                  <div className="w-10 h-10 border border-border bg-muted rounded flex items-center justify-center text-muted-foreground">
                    {isLocalDirectoryProject(project) ? <HardDrive className="w-5 h-5 text-violet-500" /> : getRepositoryIcon(project.repository_type)}
                  </div>
                  <div>
                    <h3 className="font-bold text-base text-foreground group-hover:text-primary transition-colors">
                      <Link to={`/projects/${project.id}`}>
                        {project.name}
                      </Link>
                    </h3>
                    <div className="flex items-center mt-1 space-x-2">
                      <Badge className={`cyber-badge ${project.is_active ? 'cyber-badge-success' : 'cyber-badge-muted'}`}>
                        {project.is_active ? '活跃' : '暂停'}
                      </Badge>
                      <Badge className={`cyber-badge ${isRepositoryProject(project) ? 'cyber-badge-info' : isLocalDirectoryProject(project) ? 'cyber-badge-success' : 'cyber-badge-warning'}`}>
                        {getSourceTypeBadge(project.source_type)}
                      </Badge>
                    </div>
                  </div>
                </div>
              </div>

              {/* Card Body */}
              <div className="p-4 flex-1 space-y-3">
                {project.description && (
                  <p className="text-sm text-muted-foreground font-mono line-clamp-2 border-l-2 border-border pl-2">
                    {project.description}
                  </p>
                )}

                <div className="space-y-2">
                  {project.repository_url && (
                    <div className="flex items-center text-xs font-mono text-muted-foreground bg-muted p-2 border border-border rounded">
                      <GitBranch className="w-3 h-3 mr-2 flex-shrink-0 text-muted-foreground" />
                      <a
                        href={project.repository_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="hover:text-primary transition-colors truncate"
                      >
                        {project.repository_url.replace('https://', '')}
                      </a>
                    </div>
                  )}

                  {project.local_path && (
                    <div className="flex items-center text-xs font-mono text-muted-foreground bg-muted p-2 border border-border rounded">
                      <HardDrive className="w-3 h-3 mr-2 flex-shrink-0 text-violet-500" />
                      <span className="truncate">{project.local_path}</span>
                    </div>
                  )}

                  {isPrReviewProject(project) && projectTasks[project.id] && (
                    <div className="flex items-center justify-between text-xs font-mono bg-muted p-2 border border-border rounded">
                      <span className="flex items-center text-muted-foreground">
                        <Shield className="w-3 h-3 mr-2 flex-shrink-0 text-emerald-500" />
                        PR审查任务
                      </span>
                      {renderPrTaskStatus(projectTasks[project.id]!)}
                    </div>
                  )}

                  <div className="flex justify-between items-center text-xs font-mono text-muted-foreground">
                    <span className="flex items-center"><Calendar className="w-3 h-3 mr-1" /> {formatDate(project.created_at)}</span>
                    <span className="flex items-center"><Users className="w-3 h-3 mr-1" /> {project.owner?.full_name || '未知'}</span>
                  </div>
                </div>

                {project.programming_languages && (
                  <div className="flex flex-wrap gap-1">
                    {JSON.parse(project.programming_languages).slice(0, 4).map((lang: string) => (
                      <span key={lang} className="text-xs font-mono font-bold border border-primary/30 px-1.5 py-0.5 bg-primary/10 text-primary rounded">
                        {lang.toUpperCase()}
                      </span>
                    ))}
                    {JSON.parse(project.programming_languages).length > 4 && (
                      <span className="text-xs font-mono font-bold border border-border px-1.5 py-0.5 bg-muted text-muted-foreground rounded">
                        +{JSON.parse(project.programming_languages).length - 4}
                      </span>
                    )}
                  </div>
                )}
              </div>

              {/* Card Footer */}
              <div className="p-4 border-t border-border bg-muted/50 grid grid-cols-2 gap-2">
                <Link to={`/projects/${project.id}`} className="col-span-2">
                  <Button variant="outline" className="w-full cyber-btn-outline h-8 text-xs">
                    <Code className="w-3 h-3 mr-2" />
                    查看详情
                    <ArrowUpRight className="w-3 h-3 ml-auto" />
                  </Button>
                </Link>
                <Button size="sm" className="cyber-btn-primary h-8 text-xs" onClick={() => handleCreateTask(project.id)}>
                  <Shield className="w-3 h-3 mr-2" />
                  审计
                </Button>
                <div className="grid grid-cols-2 gap-2">
                  <Button size="sm" variant="outline" className="cyber-btn-ghost h-8 px-0" onClick={() => handleEditClick(project)}>
                    <Edit className="w-3 h-3" />
                  </Button>
                  <Button size="sm" variant="outline" className="cyber-btn-ghost h-8 px-0 hover:bg-rose-500/10 hover:text-rose-400 hover:border-rose-500/30" onClick={() => handleDeleteClick(project)}>
                    <Trash2 className="w-3 h-3" />
                  </Button>
                </div>
              </div>
            </div>
          ))
        ) : (
          <div className="col-span-full">
            <div className="cyber-card p-16 text-center border-dashed">
              <Code className="w-16 h-16 text-muted-foreground mx-auto mb-4" />
              <h3 className="text-xl font-bold text-foreground mb-2">
                {searchTerm ? '未找到匹配项' : '未初始化项目'}
              </h3>
              <p className="text-muted-foreground font-mono mb-6">
                {searchTerm ? '调整搜索参数' : '初始化第一个项目以开始'}
              </p>
              {!searchTerm && (
                <Button onClick={() => setShowCreateDialog(true)} className="cyber-btn-primary">
                  <Plus className="w-4 h-4 mr-2" />
                  初始化项目
                </Button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Create Task Dialog */}
      <CreateTaskDialog
        open={showCreateTaskDialog}
        onOpenChange={setShowCreateTaskDialog}
        onTaskCreated={handleTaskCreated}
        preselectedProjectId={selectedProjectForTask}
      />

      {/* Edit Dialog */}
      <Dialog open={showEditDialog} onOpenChange={setShowEditDialog}>
        <DialogContent className="!w-[min(90vw,700px)] !max-w-none max-h-[85vh] flex flex-col p-0 gap-0 cyber-dialog border border-border rounded-lg">
          {/* Terminal Header */}
          <div className="flex items-center gap-2 px-4 py-3 cyber-bg-elevated border-b border-border flex-shrink-0">
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-full bg-red-500/80" />
              <div className="w-3 h-3 rounded-full bg-yellow-500/80" />
              <div className="w-3 h-3 rounded-full bg-green-500/80" />
            </div>
            <span className="ml-2 text-xs text-muted-foreground">
              edit@codesage
            </span>
          </div>

          <DialogHeader className="px-6 pt-4 flex-shrink-0">
            <DialogTitle className="font-mono text-lg uppercase tracking-wider flex items-center gap-2 text-foreground">
              <Edit className="w-5 h-5 text-primary" />
              编辑项目配置
              {projectToEdit && (
                <Badge className={`ml-2 ${editForm.source_type === 'repository' ? 'cyber-badge-info' : editForm.source_type === 'local_directory' ? 'cyber-badge-success' : 'cyber-badge-warning'}`}>
                  {editForm.source_type === 'repository' ? '远程仓库' : editForm.source_type === 'local_directory' ? '本地目录' : 'ZIP上传'}
                </Badge>
              )}
            </DialogTitle>
          </DialogHeader>

          <div className="flex-1 overflow-y-auto p-6 space-y-6">
            {/* 基本信息 */}
            <div className="space-y-4">
              <h3 className="font-mono font-bold uppercase text-sm text-muted-foreground border-b border-border pb-2">基本信息</h3>
              <div>
                <Label htmlFor="edit-name" className="font-mono font-bold uppercase text-xs text-muted-foreground">项目名称 *</Label>
                <Input
                  id="edit-name"
                  value={editForm.name}
                  onChange={(e) => setEditForm({ ...editForm, name: e.target.value })}
                  className="cyber-input mt-1"
                />
              </div>
              <div>
                <Label htmlFor="edit-description" className="font-mono font-bold uppercase text-xs text-muted-foreground">描述</Label>
                <Textarea
                  id="edit-description"
                  value={editForm.description}
                  onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                  rows={3}
                  className="cyber-input mt-1"
                />
              </div>
            </div>

            {/* 仓库信息 - 仅远程仓库类型显示 */}
            {editForm.source_type === 'local_directory' && (
              <div className="space-y-4">
                <h3 className="font-mono font-bold uppercase text-sm text-muted-foreground border-b border-border pb-2 flex items-center gap-2">
                  <HardDrive className="w-4 h-4" />
                  本地目录
                </h3>
                <div>
                  <Label htmlFor="edit-local-path" className="font-mono font-bold uppercase text-xs text-muted-foreground">目录路径</Label>
                  <Input
                    id="edit-local-path"
                    value={editForm.local_path || ""}
                    onChange={(e) => setEditForm({ ...editForm, local_path: e.target.value, workspace_mode: "in_place" })}
                    className="cyber-input mt-1"
                  />
                </div>
              </div>
            )}

            {editForm.source_type === 'repository' && (
              <div className="space-y-4">
                <h3 className="font-mono font-bold uppercase text-sm text-muted-foreground border-b border-border pb-2 flex items-center gap-2">
                  <GitBranch className="w-4 h-4" />
                  仓库信息
                </h3>

                <div>
                  <Label htmlFor="edit-repo-url" className="font-mono font-bold uppercase text-xs text-muted-foreground">仓库地址</Label>
                  <Input
                    id="edit-repo-url"
                    value={editForm.repository_url}
                    onChange={(e) => setEditForm({ ...editForm, repository_url: e.target.value })}
                    placeholder={
                      editForm.repository_type === 'other'
                        ? "git@github.com:user/repo.git"
                        : "https://github.com/user/repo"
                    }
                    className="cyber-input mt-1"
                  />
                  {editForm.repository_type === 'other' && (
                    <p className="text-xs text-muted-foreground font-mono mt-1">
                      💡 SSH Key认证请使用 git@ 格式的SSH URL
                    </p>
                  )}
                  {editForm.repository_type !== 'other' && (
                    <p className="text-xs text-muted-foreground font-mono mt-1">
                      💡 Token认证请使用 https:// 格式的URL
                    </p>
                  )}
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label htmlFor="edit-repo-type" className="font-mono font-bold uppercase text-xs text-muted-foreground">认证类型</Label>
                    <Select
                      value={editForm.repository_type}
                      onValueChange={(value: any) => setEditForm({ ...editForm, repository_type: value })}
                    >
                      <SelectTrigger id="edit-repo-type" className="cyber-input mt-1">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="cyber-dialog border-border">
                        {REPOSITORY_PLATFORMS.map((platform) => (
                          <SelectItem key={platform.value} value={platform.value}>
                            {platform.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div>
                    <Label htmlFor="edit-default-branch" className="font-mono font-bold uppercase text-xs text-muted-foreground">默认分支</Label>
                    <Input
                      id="edit-default-branch"
                      value={editForm.default_branch}
                      onChange={(e) => setEditForm({ ...editForm, default_branch: e.target.value })}
                      placeholder="main"
                      className="cyber-input mt-1"
                    />
                  </div>
                </div>
              </div>
            )}

            {/* ZIP项目文件管理 */}
            {editForm.source_type === "zip" && (
              <div className="space-y-4">
                <h3 className="font-mono font-bold uppercase text-sm text-muted-foreground border-b border-border pb-2 flex items-center gap-2">
                  <Upload className="w-4 h-4" />
                  ZIP文件管理
                </h3>

                {loadingEditZipInfo ? (
                  <div className="flex items-center space-x-3 p-4 bg-sky-500/10 border border-sky-500/30 rounded">
                    <div className="loading-spinner w-5 h-5"></div>
                    <p className="text-sm text-sky-400 font-bold font-mono">正在加载源码工件信息...</p>
                  </div>
                ) : (
                  <div className="grid gap-3 md:grid-cols-2">
                    <div className={`rounded border p-4 ${editZipInfo?.has_file ? "border-emerald-500/30 bg-emerald-500/10" : "border-amber-500/30 bg-amber-500/10"}`}>
                      <div className="flex items-start gap-3">
                        <FileText className={`mt-0.5 h-5 w-5 ${editZipInfo?.has_file ? "text-emerald-400" : "text-amber-400"}`} />
                        <div className="space-y-1 text-sm font-mono">
                          <p className={`font-bold uppercase ${editZipInfo?.has_file ? "text-emerald-300" : "text-amber-300"}`}>
                            原始 ZIP 归档
                          </p>
                          {editZipInfo?.has_file ? (
                            <>
                              <p className="text-xs text-muted-foreground">
                                文件: {editZipInfo.original_filename}
                                {typeof editZipInfo.file_size === "number" ? ` (${formatFileSize(editZipInfo.file_size)})` : ""}
                              </p>
                              {editZipInfo.uploaded_at && (
                                <p className="text-xs text-muted-foreground">
                                  上传时间: {new Date(editZipInfo.uploaded_at).toLocaleString("zh-CN")}
                                </p>
                              )}
                            </>
                          ) : (
                            <p className="text-xs text-muted-foreground">
                              当前未保留原始 ZIP 归档。
                            </p>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className={`rounded border p-4 ${editZipInfo?.has_persistent_source ? "border-emerald-500/30 bg-emerald-500/10" : "border-amber-500/30 bg-amber-500/10"}`}>
                      <div className="flex items-start gap-3">
                        <Folder className={`mt-0.5 h-5 w-5 ${editZipInfo?.has_persistent_source ? "text-emerald-400" : "text-amber-400"}`} />
                        <div className="space-y-1 text-sm font-mono">
                          <p className={`font-bold uppercase ${editZipInfo?.has_persistent_source ? "text-emerald-300" : "text-amber-300"}`}>
                            持久源码目录
                          </p>
                          {editZipInfo?.has_persistent_source ? (
                            <>
                              <p className="break-all text-xs text-muted-foreground">
                                路径: {editZipInfo.persistent_source_path}
                              </p>
                              {editZipInfo.persistent_source_updated_at && (
                                <p className="text-xs text-muted-foreground">
                                  更新时间: {new Date(editZipInfo.persistent_source_updated_at).toLocaleString("zh-CN")}
                                </p>
                              )}
                              <p className="text-xs text-emerald-300/80">
                                Agent 直审默认直接使用这份目录，workflow 审计会先复制临时工作副本。
                              </p>
                            </>
                          ) : (
                            <p className="text-xs text-muted-foreground">
                              当前没有可用的持久源码目录。重新上传 ZIP 后会自动生成。
                            </p>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                <div className="space-y-3 rounded border border-border bg-muted/20 p-4">
                  <Label className="font-mono font-bold uppercase text-xs text-muted-foreground">
                    {editZipInfo?.has_persistent_source ? "替换源码 ZIP" : "上传源码 ZIP"}
                  </Label>
                  <input
                    ref={editZipInputRef}
                    type="file"
                    accept=".zip"
                    className="hidden"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) {
                        const validation = validateZipFile(file);
                        if (!validation.valid) {
                          toast.error(validation.error || "文件无效");
                          e.target.value = "";
                          return;
                        }
                        setEditZipFile(file);
                        toast.success(`已选择文件: ${file.name}`);
                      }
                    }}
                  />

                  {editZipFile ? (
                    <div className="flex items-center justify-between p-3 bg-sky-500/10 border border-sky-500/30 rounded">
                      <div className="flex items-center space-x-2">
                        <FileText className="w-4 h-4 text-sky-400" />
                        <span className="text-sm font-mono font-bold text-sky-300">{editZipFile.name}</span>
                        <span className="text-xs text-muted-foreground">
                          ({formatFileSize(editZipFile.size)})
                        </span>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setEditZipFile(null)}
                        className="cyber-btn-ghost h-7 text-xs"
                      >
                        取消
                      </Button>
                    </div>
                  ) : (
                    <Button
                      variant="outline"
                      onClick={() => editZipInputRef.current?.click()}
                      className="cyber-btn-outline w-full"
                    >
                      <Upload className="w-4 h-4 mr-2" />
                      {editZipInfo?.has_persistent_source ? "选择新 ZIP 替换" : "选择 ZIP 文件"}
                    </Button>
                  )}

                  <label className="flex items-start gap-3 rounded border border-border bg-background/40 p-3">
                    <input
                      type="checkbox"
                      checked={editKeepZipArchive}
                      onChange={(event) => setEditKeepZipArchive(event.target.checked)}
                      className="mt-0.5 h-4 w-4 rounded border border-border bg-transparent"
                    />
                    <div className="space-y-1">
                      <p className="text-sm font-mono font-bold text-foreground">保留原始 ZIP 归档</p>
                      <p className="text-xs font-mono text-muted-foreground">
                        上传替换包时，无论是否保留归档，系统都会重建持久源码目录。
                      </p>
                    </div>
                  </label>
                </div>

                <div className="space-y-3 rounded border border-rose-500/25 bg-rose-500/5 p-4">
                  <div className="space-y-1">
                    <p className="text-sm font-mono font-bold uppercase text-rose-300">手动删除源码工件</p>
                    <p className="text-xs font-mono text-muted-foreground">
                      删除能力已移动到项目管理中。项目本身不会自动清理 ZIP 归档或持久源码目录。
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      className="cyber-btn-outline"
                      disabled={deletingZipArtifacts || !editZipInfo?.has_file}
                      onClick={() => void handleDeleteZipArtifacts(true, false)}
                    >
                      删除原始 ZIP
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      className="cyber-btn-outline"
                      disabled={deletingZipArtifacts || !editZipInfo?.has_persistent_source}
                      onClick={() => void handleDeleteZipArtifacts(false, true)}
                    >
                      删除持久源码目录
                    </Button>
                    <Button
                      type="button"
                      variant="destructive"
                      disabled={deletingZipArtifacts || (!editZipInfo?.has_file && !editZipInfo?.has_persistent_source)}
                      onClick={() => void handleDeleteZipArtifacts(true, true)}
                    >
                      全部删除
                    </Button>
                  </div>
                </div>
              </div>
            )}

            {/* 技术栈 */}
            <div className="space-y-4">
              <h3 className="font-mono font-bold uppercase text-sm text-muted-foreground border-b border-border pb-2">技术栈</h3>
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                {supportedLanguages.map((lang) => (
                  <div
                    key={lang}
                    className={`flex items-center space-x-2 p-2 border cursor-pointer transition-all rounded ${editForm.programming_languages?.includes(lang)
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border hover:border-border text-muted-foreground'
                      }`}
                    onClick={() => handleToggleLanguage(lang)}
                  >
                    <div
                      className={`w-4 h-4 border-2 rounded-sm flex items-center justify-center ${editForm.programming_languages?.includes(lang)
                        ? 'bg-primary border-primary'
                        : 'border-border'
                        }`}
                    >
                      {editForm.programming_languages?.includes(lang) && (
                        <CheckCircle className="w-3 h-3 text-foreground" />
                      )}
                    </div>
                    <span className="text-sm font-mono font-bold uppercase">{lang}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div className="flex-shrink-0 flex justify-end gap-3 px-6 py-4 bg-muted border-t border-border">
            <Button variant="outline" onClick={() => setShowEditDialog(false)} className="cyber-btn-outline">
              取消
            </Button>
            <Button onClick={handleSaveEdit} className="cyber-btn-primary">
              保存更改
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* Delete Dialog */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent className="cyber-card border-border cyber-dialog p-0 !fixed">
          {/* Terminal Header */}
          <div className="flex items-center gap-2 px-4 py-3 bg-rose-500/10 border-b border-rose-500/30">
            <div className="flex items-center gap-1.5">
              <div className="w-3 h-3 rounded-full bg-red-500/80" />
              <div className="w-3 h-3 rounded-full bg-yellow-500/80" />
              <div className="w-3 h-3 rounded-full bg-green-500/80" />
            </div>
            <span className="ml-2 text-xs text-rose-400">
              delete@codesage
            </span>
          </div>

          <AlertDialogHeader className="p-6">
            <AlertDialogTitle className="font-mono text-lg uppercase tracking-wider flex items-center gap-2 text-foreground">
              <Trash2 className="w-5 h-5 text-rose-400" />
              确认删除
            </AlertDialogTitle>
            <AlertDialogDescription className="text-muted-foreground font-mono">
              您确定要永久删除 <span className="font-bold text-rose-400">"{projectToDelete?.name}"</span> 吗？此操作无法恢复。
            </AlertDialogDescription>
          </AlertDialogHeader>

          <div className="px-6 pb-6">
            <div className="bg-sky-500/10 border border-sky-500/30 p-4 rounded">
              <p className="text-sky-300 font-bold mb-2 font-mono uppercase text-sm">系统通知:</p>
              <ul className="list-none text-sky-400/80 space-y-1 text-xs font-mono">
                <li className="flex items-center gap-2"><span className="text-sky-400">&gt;</span> 项目将被永久删除</li>
                <li className="flex items-center gap-2"><span className="text-sky-400">&gt;</span> 无法通过回收站恢复</li>
                <li className="flex items-center gap-2"><span className="text-sky-400">&gt;</span> 关联审计数据会一并删除</li>
                <li className="flex items-center gap-2"><span className="text-sky-400">&gt;</span> 请确认已不再需要此项目</li>
              </ul>
            </div>
          </div>

          <AlertDialogFooter className="p-4 border-t border-border bg-muted/50">
            <AlertDialogCancel className="cyber-btn-outline">取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleConfirmDelete}
              className="cyber-btn bg-rose-500/90 border-rose-500/50 text-foreground hover:bg-rose-500"
            >
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
