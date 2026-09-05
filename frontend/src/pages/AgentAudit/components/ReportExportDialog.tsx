import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { marked } from "marked";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Code2,
  Copy,
  Download,
  Eye,
  FileCode,
  FileJson,
  FileText,
  Loader2,
  Pencil,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Shield,
} from "lucide-react";
import { toast } from "sonner";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { apiClient } from "@/shared/api/serverClient";
import { syncLocalLibraries } from "@/shared/api/modelConfig";
import { downloadAgentReport, type AgentFinding, type AgentTask } from "@/shared/api/agentTasks";
import {
  getReportTemplates,
  resyncReportTemplates,
  updateReportTemplate,
  type ReportTemplate,
  type ReportTemplatePayload,
} from "@/shared/api/reportTemplates";

type ReportFormat = "markdown" | "json" | "html";
type WorkspaceMode = "report" | "template-view" | "template-edit";

interface ReportExportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  task: AgentTask | null;
  findings: AgentFinding[];
}

interface ReportPreview {
  content: string;
  format: ReportFormat;
  loading: boolean;
  error: string | null;
}

const HOST_PROJECT_ROOT = (import.meta.env.VITE_HOST_PROJECT_ROOT as string | undefined) || "";

const FORMAT_OPTIONS: Record<ReportFormat, {
  label: string;
  description: string;
  extension: string;
  icon: ReactNode;
}> = {
  markdown: {
    label: "Markdown",
    description: "可编辑、便于二次整理",
    extension: ".md",
    icon: <FileText className="h-4 w-4" />,
  },
  json: {
    label: "JSON",
    description: "结构化数据，适合自动化处理",
    extension: ".json",
    icon: <FileJson className="h-4 w-4" />,
  },
  html: {
    label: "HTML",
    description: "带样式的网页报告",
    extension: ".html",
    icon: <FileCode className="h-4 w-4" />,
  },
};

function errorMessage(error: unknown, fallback: string) {
  if (typeof error === "object" && error && "response" in error) {
    const detail = (error as { response?: { data?: { detail?: string } } }).response?.data?.detail;
    if (detail) return detail;
  }
  return error instanceof Error ? error.message : fallback;
}

function buildAbsolutePath(relativePath?: string) {
  if (!relativePath) return "";
  return HOST_PROJECT_ROOT
    ? `${HOST_PROJECT_ROOT.replace(/[\\/]+$/, "")}/${relativePath}`.replace(/\//g, "\\")
    : relativePath;
}

function metadataString(template: ReportTemplate | null, key: string) {
  const value = template?.metadata_json?.[key];
  return typeof value === "string" ? value : "";
}

function formatBytes(bytes: number) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDate(value?: string | null) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未记录";
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function copyText(value: string, successMessage: string) {
  if (!value) return;
  navigator.clipboard.writeText(value).then(
    () => toast.success(successMessage),
    () => toast.error("复制失败")
  );
}

function toTemplatePayload(template: ReportTemplate): ReportTemplatePayload {
  return {
    name: template.name,
    description: template.description || "",
    report_type: template.report_type,
    output_format: template.output_format,
    content: template.content,
    variables: template.variables || {},
    metadata_json: template.metadata_json || {},
    is_active: template.is_active,
    is_default: template.is_default,
    sort_order: template.sort_order,
  };
}

function getScoreTone(score: number) {
  if (score >= 80) return "text-emerald-700 border-emerald-200 bg-emerald-50";
  if (score >= 60) return "text-amber-700 border-amber-200 bg-amber-50";
  return "text-rose-700 border-rose-200 bg-rose-50";
}

function getSelectedTemplateId(templateId: string) {
  return templateId === "system-default" ? undefined : templateId;
}

function StatCard({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string | number;
  tone?: "default" | "danger" | "success";
}) {
  const toneClass = {
    default: "text-slate-900",
    danger: "text-rose-600",
    success: "text-emerald-700",
  }[tone];

  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className={`mt-1 font-mono text-2xl font-bold ${toneClass}`}>{value}</div>
    </div>
  );
}

function PlainPreview({
  content,
  searchQuery,
}: {
  content: string;
  searchQuery: string;
}) {
  if (!searchQuery) {
    return <pre className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-700">{content}</pre>;
  }

  const regex = new RegExp(`(${searchQuery.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
  return (
    <pre className="whitespace-pre-wrap break-words text-sm leading-7 text-slate-700">
      {content.split(regex).map((part, index) =>
        regex.test(part) ? (
          <mark key={`${part}-${index}`} className="rounded bg-amber-200 px-1 text-slate-900">
            {part}
          </mark>
        ) : (
          part
        )
      )}
    </pre>
  );
}

function HtmlSourcePreview({ content }: { content: string }) {
  return (
    <iframe
      title="报告 HTML 预览"
      srcDoc={content}
      className="h-full min-h-[520px] w-full rounded-md border-0 bg-white"
    />
  );
}

function TemplateMetaRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="text-[11px] font-semibold uppercase tracking-normal text-slate-500">{label}</div>
      <div className="mt-1 break-all text-sm text-slate-700">{value || "暂无"}</div>
    </div>
  );
}

export const ReportExportDialog = memo(function ReportExportDialog({
  open,
  onOpenChange,
  task,
  findings,
}: ReportExportDialogProps) {
  const [activeFormat, setActiveFormat] = useState<ReportFormat>("markdown");
  const [preview, setPreview] = useState<ReportPreview>({
    content: "",
    format: "markdown",
    loading: false,
    error: null,
  });
  const [reportTemplates, setReportTemplates] = useState<ReportTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("system-default");
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceMode>("report");
  const [searchQuery, setSearchQuery] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [savingTemplate, setSavingTemplate] = useState(false);
  const [loadingTemplates, setLoadingTemplates] = useState(false);
  const [copied, setCopied] = useState(false);
  const [templateForm, setTemplateForm] = useState<ReportTemplatePayload | null>(null);
  const previewCache = useRef<Map<string, string>>(new Map());

  const selectedTemplate = useMemo(
    () => reportTemplates.find((template) => template.id === selectedTemplateId) || null,
    [reportTemplates, selectedTemplateId]
  );

  const taskName = task?.name || (task ? `Agent Task ${task.id.slice(0, 8)}` : "审计任务");
  const totalFindings = task?.findings_count ?? findings.length;
  const criticalAndHigh = (task?.critical_count || 0) + (task?.high_count || 0);
  const verifiedCount = task?.verified_count || findings.filter((finding) => finding.is_verified).length;
  const score = task?.security_score ?? 0;

  const searchMatchCount = useMemo(() => {
    if (!searchQuery || !preview.content) return 0;
    const regex = new RegExp(searchQuery.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    return (preview.content.match(regex) || []).length;
  }, [preview.content, searchQuery]);

  const generateHtmlReport = useCallback(async (markdown: string, currentTask: AgentTask) => {
    const contentHtml = await marked.parse(markdown);
    const generatedAt = new Date().toLocaleString("zh-CN");
    const htmlTaskName = currentTask.name || `Task ${currentTask.id.slice(0, 8)}`;
    return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CodeSage 审计报告 - ${htmlTaskName}</title>
  <style>
    body { margin: 0; background: #f8fafc; color: #0f172a; font: 15px/1.7 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .shell { max-width: 960px; margin: 0 auto; padding: 40px 24px; }
    header { border: 1px solid #dbe7e2; background: #ffffff; border-radius: 12px; padding: 28px; margin-bottom: 20px; }
    h1, h2, h3 { color: #0f172a; line-height: 1.25; }
    h1 { margin: 0 0 12px; font-size: 30px; }
    .meta { color: #64748b; font-size: 13px; }
    main { border: 1px solid #dbe7e2; background: #ffffff; border-radius: 12px; padding: 32px; }
    code { background: #ecfdf5; color: #065f46; padding: 2px 5px; border-radius: 5px; }
    pre { overflow: auto; background: #0f172a; color: #e2e8f0; padding: 16px; border-radius: 10px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e2e8f0; padding: 10px; text-align: left; }
    blockquote { margin: 16px 0; border-left: 3px solid #0d9488; padding-left: 14px; color: #475569; }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <h1>${htmlTaskName}</h1>
      <div class="meta">CodeSage 审计报告 · ${generatedAt}</div>
    </header>
    <main>${contentHtml}</main>
  </div>
</body>
</html>`;
  }, []);

  const fetchPreview = useCallback(async (format: ReportFormat, forceRefresh = false) => {
    if (!task) return;
    const cacheKey = `${format}:${selectedTemplateId}`;

    if (!forceRefresh && previewCache.current.has(cacheKey)) {
      setPreview({
        content: previewCache.current.get(cacheKey) || "",
        format,
        loading: false,
        error: null,
      });
      return;
    }

    setPreview((current) => ({ ...current, loading: true, error: null }));

    try {
      let content = "";
      const templateId = getSelectedTemplateId(selectedTemplateId);

      if (format === "json") {
        const response = await apiClient.get(`/agent-tasks/${task.id}/report`, {
          params: { format: "json", template_id: templateId },
        });
        content = JSON.stringify(response.data, null, 2);
      } else {
        const response = await apiClient.get(`/agent-tasks/${task.id}/report`, {
          params: { format: "markdown", template_id: templateId },
          responseType: "text",
        });
        content = format === "html" ? await generateHtmlReport(response.data, task) : response.data;
      }

      previewCache.current.set(cacheKey, content);
      setPreview({ content, format, loading: false, error: null });
    } catch (error) {
      setPreview((current) => ({
        ...current,
        loading: false,
        error: errorMessage(error, "报告预览加载失败"),
      }));
    }
  }, [generateHtmlReport, selectedTemplateId, task]);

  const loadTemplates = useCallback(async () => {
    setLoadingTemplates(true);
    try {
      const response = await getReportTemplates();
      setReportTemplates(response.items);
      const currentStillExists = response.items.some((template) => template.id === selectedTemplateId);
      if (!currentStillExists) {
        const defaultTemplate = response.items.find((template) => template.is_default) || response.items[0];
        setSelectedTemplateId(defaultTemplate?.id || "system-default");
      }
    } catch (error) {
      toast.error(errorMessage(error, "加载报告模板失败"));
    } finally {
      setLoadingTemplates(false);
    }
  }, [selectedTemplateId]);

  useEffect(() => {
    if (!open || !task) return;
    loadTemplates();
  }, [loadTemplates, open, task]);

  useEffect(() => {
    if (!open || !task) return;
    fetchPreview(activeFormat);
  }, [activeFormat, fetchPreview, open, task]);

  useEffect(() => {
    if (!open) {
      previewCache.current.clear();
      setSearchQuery("");
      setWorkspaceMode("report");
      setCopied(false);
      setTemplateForm(null);
    }
  }, [open]);

  useEffect(() => {
    if (workspaceMode === "template-edit" && selectedTemplate) {
      setTemplateForm(toTemplatePayload(selectedTemplate));
    }
  }, [selectedTemplate, workspaceMode]);

  const handleFormatChange = (format: ReportFormat) => {
    setActiveFormat(format);
    setWorkspaceMode("report");
  };

  const handleTemplateChange = (templateId: string) => {
    setSelectedTemplateId(templateId);
    setWorkspaceMode("report");
    previewCache.current.clear();
  };

  const handleCopy = async () => {
    if (!preview.content) return;
    try {
      await navigator.clipboard.writeText(preview.content);
      setCopied(true);
      toast.success("报告内容已复制");
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      toast.error("复制失败");
    }
  };

  const handleDownload = async () => {
    if (!task) return;
    setDownloading(true);
    try {
      await downloadAgentReport(task.id, activeFormat, getSelectedTemplateId(selectedTemplateId));
      toast.success(`已导出 ${FORMAT_OPTIONS[activeFormat].label} 报告`);
      onOpenChange(false);
    } catch (error) {
      toast.error(errorMessage(error, "导出报告失败"));
    } finally {
      setDownloading(false);
    }
  };

  const handleSaveTemplate = async () => {
    if (!selectedTemplate || !templateForm) return;
    setSavingTemplate(true);
    try {
      await updateReportTemplate(selectedTemplate.id, templateForm);
      await syncLocalLibraries();
      await resyncReportTemplates();
      await loadTemplates();
      previewCache.current.clear();
      await fetchPreview(activeFormat, true);
      setWorkspaceMode("template-view");
      toast.success("报告模板已更新");
    } catch (error) {
      toast.error(errorMessage(error, "保存报告模板失败"));
    } finally {
      setSavingTemplate(false);
    }
  };

  const handleRefreshTemplates = async () => {
    try {
      setLoadingTemplates(true);
      await syncLocalLibraries();
      await resyncReportTemplates();
      await loadTemplates();
      previewCache.current.clear();
      await fetchPreview(activeFormat, true);
      toast.success("模板目录已同步");
    } catch (error) {
      toast.error(errorMessage(error, "同步模板失败"));
    } finally {
      setLoadingTemplates(false);
    }
  };

  if (!task) return null;

  const selectedTemplatePath = buildAbsolutePath(metadataString(selectedTemplate, "workspace_relative_path"));
  const selectedTemplateFile = buildAbsolutePath(metadataString(selectedTemplate, "workspace_file_path"));
  const canEditTemplate = Boolean(selectedTemplate);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[90vh] max-w-6xl flex-col overflow-hidden border-slate-200 bg-slate-50 p-0 shadow-2xl">
        <DialogHeader className="border-b border-slate-200 bg-white px-6 py-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div className="flex items-start gap-4">
              <div className="flex h-12 w-12 items-center justify-center rounded-lg border border-teal-200 bg-teal-50 text-teal-700">
                <Download className="h-5 w-5" />
              </div>
              <div>
                <DialogTitle className="text-2xl font-bold tracking-normal text-slate-950">
                  导出审计报告
                </DialogTitle>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-500">
                  <span>{taskName}</span>
                  <span className="text-slate-300">/</span>
                  <span>完成于 {formatDate(task.completed_at || task.started_at || task.created_at)}</span>
                </div>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:flex">
              <StatCard label="安全评分" value={score.toFixed(0)} tone={score >= 70 ? "success" : "danger"} />
              <StatCard label="漏洞总数" value={totalFindings} />
              <StatCard label="高危以上" value={criticalAndHigh} tone={criticalAndHigh > 0 ? "danger" : "default"} />
              <StatCard label="已验证" value={verifiedCount} tone="success" />
            </div>
          </div>
        </DialogHeader>

        <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[340px_minmax(0,1fr)]">
          <aside className="min-h-0 border-r border-slate-200 bg-white">
            <ScrollArea className="h-full">
              <div className="space-y-5 p-5">
                <section className="space-y-3">
                  <div className="flex items-center justify-between">
                    <Label className="text-sm font-semibold text-slate-900">导出格式</Label>
                    <Badge variant="outline" className="rounded-md font-normal">
                      {FORMAT_OPTIONS[activeFormat].extension}
                    </Badge>
                  </div>
                  <div className="grid gap-2">
                    {(Object.keys(FORMAT_OPTIONS) as ReportFormat[]).map((format) => {
                      const item = FORMAT_OPTIONS[format];
                      const active = activeFormat === format;
                      return (
                        <button
                          key={format}
                          type="button"
                          onClick={() => handleFormatChange(format)}
                          className={`flex min-h-16 items-center gap-3 rounded-lg border px-3 py-3 text-left transition ${
                            active
                              ? "border-teal-300 bg-teal-50 text-teal-950 shadow-sm"
                              : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50"
                          }`}
                        >
                          <span className={`flex h-9 w-9 items-center justify-center rounded-md ${active ? "bg-white text-teal-700" : "bg-slate-100 text-slate-500"}`}>
                            {item.icon}
                          </span>
                          <span className="min-w-0 flex-1">
                            <span className="block text-sm font-semibold">{item.label}</span>
                            <span className="block text-xs leading-5 text-slate-500">{item.description}</span>
                          </span>
                          {active && <Check className="h-4 w-4 text-teal-700" />}
                        </button>
                      );
                    })}
                  </div>
                </section>

                <section className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <Label className="text-sm font-semibold text-slate-900">报告模板</Label>
                      <p className="mt-1 text-xs leading-5 text-slate-500">
                        模板选择、查看和编辑都收在导出流程里。
                      </p>
                    </div>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-9 w-9"
                      onClick={handleRefreshTemplates}
                      disabled={loadingTemplates}
                      title="同步模板目录"
                    >
                      <RefreshCw className={`h-4 w-4 ${loadingTemplates ? "animate-spin" : ""}`} />
                    </Button>
                  </div>

                  <Select value={selectedTemplateId} onValueChange={handleTemplateChange}>
                    <SelectTrigger className="h-11 bg-white">
                      <SelectValue placeholder="选择报告模板" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="system-default">系统默认模板</SelectItem>
                      {reportTemplates.map((template) => (
                        <SelectItem key={template.id} value={template.id}>
                          {template.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>

                  <div className="grid grid-cols-2 gap-2">
                    <Button
                      variant={workspaceMode === "template-view" ? "default" : "outline"}
                      size="sm"
                      onClick={() => setWorkspaceMode("template-view")}
                      disabled={!selectedTemplate}
                    >
                      <Eye className="mr-2 h-4 w-4" />
                      查看模板
                    </Button>
                    <Button
                      variant={workspaceMode === "template-edit" ? "default" : "outline"}
                      size="sm"
                      onClick={() => setWorkspaceMode("template-edit")}
                      disabled={!canEditTemplate}
                    >
                      <Pencil className="mr-2 h-4 w-4" />
                      编辑
                    </Button>
                  </div>

                  {selectedTemplate && (
                    <div className="flex flex-wrap gap-2">
                      {selectedTemplate.is_default && <Badge className="bg-emerald-100 text-emerald-700">默认模板</Badge>}
                      {selectedTemplate.is_active ? <Badge className="bg-teal-100 text-teal-700">启用中</Badge> : <Badge variant="outline">已停用</Badge>}
                      {selectedTemplate.is_system && <Badge className="bg-slate-200 text-slate-700">system</Badge>}
                    </div>
                  )}
                </section>

                <section className={`rounded-lg border p-4 ${getScoreTone(score)}`}>
                  <div className="flex items-center gap-2 text-sm font-semibold">
                    <Shield className="h-4 w-4" />
                    导出前概览
                  </div>
                  <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                    <div>
                      <div className="text-xs opacity-75">分析文件</div>
                      <div className="font-mono font-semibold">{task.analyzed_files || task.total_files || 0}</div>
                    </div>
                    <div>
                      <div className="text-xs opacity-75">工具调用</div>
                      <div className="font-mono font-semibold">{task.tool_calls_count || 0}</div>
                    </div>
                    <div>
                      <div className="text-xs opacity-75">Critical</div>
                      <div className="font-mono font-semibold">{task.critical_count || 0}</div>
                    </div>
                    <div>
                      <div className="text-xs opacity-75">High</div>
                      <div className="font-mono font-semibold">{task.high_count || 0}</div>
                    </div>
                  </div>
                </section>
              </div>
            </ScrollArea>
          </aside>

          <main className="flex min-h-0 flex-col bg-slate-50">
            <div className="flex min-h-14 items-center justify-between gap-3 border-b border-slate-200 bg-white px-5">
              <div className="flex min-w-0 items-center gap-3">
                {workspaceMode === "report" && <FileText className="h-4 w-4 text-slate-500" />}
                {workspaceMode === "template-view" && <Eye className="h-4 w-4 text-slate-500" />}
                {workspaceMode === "template-edit" && <Pencil className="h-4 w-4 text-slate-500" />}
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-slate-900">
                    {workspaceMode === "report" && "报告预览"}
                    {workspaceMode === "template-view" && (selectedTemplate?.name || "模板详情")}
                    {workspaceMode === "template-edit" && `编辑模板：${selectedTemplate?.name || ""}`}
                  </div>
                  <div className="text-xs text-slate-500">
                    {workspaceMode === "report"
                      ? `${FORMAT_OPTIONS[activeFormat].label} · ${formatBytes(preview.content.length)}`
                      : selectedTemplate?.report_type || "audit_report"}
                  </div>
                </div>
              </div>

              <div className="flex items-center gap-2">
                {workspaceMode === "report" && (
                  <>
                    <div className="hidden items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 sm:flex">
                      <Search className="h-4 w-4 text-slate-400" />
                      <Input
                        value={searchQuery}
                        onChange={(event) => setSearchQuery(event.target.value)}
                        placeholder="搜索预览"
                        className="h-5 w-28 border-0 bg-transparent p-0 text-xs shadow-none focus-visible:shadow-none"
                      />
                      {searchQuery && <span className="font-mono text-xs text-slate-400">{searchMatchCount}</span>}
                    </div>
                    <Button variant="outline" size="sm" onClick={handleCopy} disabled={preview.loading || !preview.content}>
                      {copied ? <Check className="mr-2 h-4 w-4" /> : <Copy className="mr-2 h-4 w-4" />}
                      {copied ? "已复制" : "复制"}
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => fetchPreview(activeFormat, true)} disabled={preview.loading}>
                      <RotateCcw className={`mr-2 h-4 w-4 ${preview.loading ? "animate-spin" : ""}`} />
                      刷新
                    </Button>
                  </>
                )}
                {workspaceMode !== "report" && (
                  <Button variant="outline" size="sm" onClick={() => setWorkspaceMode("report")}>
                    返回预览
                  </Button>
                )}
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-hidden">
              {workspaceMode === "report" && (
                <ScrollArea className="h-full">
                  <div className="p-5">
                    {preview.loading ? (
                      <div className="flex h-[520px] items-center justify-center rounded-lg border border-slate-200 bg-white">
                        <div className="text-center text-slate-500">
                          <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin" />
                          正在生成报告预览
                        </div>
                      </div>
                    ) : preview.error ? (
                      <div className="flex h-[520px] items-center justify-center rounded-lg border border-amber-200 bg-amber-50">
                        <div className="max-w-sm text-center">
                          <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-amber-600" />
                          <div className="font-semibold text-amber-900">预览加载失败</div>
                          <p className="mt-2 text-sm leading-6 text-amber-800">{preview.error}</p>
                          <Button className="mt-4" variant="outline" onClick={() => fetchPreview(activeFormat, true)}>
                            重试
                          </Button>
                        </div>
                      </div>
                    ) : activeFormat === "html" ? (
                      <div className="h-full overflow-hidden rounded-lg border border-slate-200 bg-white p-2 shadow-sm">
                        <HtmlSourcePreview content={preview.content} />
                      </div>
                    ) : (
                      <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
                        <PlainPreview content={preview.content} searchQuery={searchQuery} />
                      </div>
                    )}
                  </div>
                </ScrollArea>
              )}

              {workspaceMode === "template-view" && (
                <ScrollArea className="h-full">
                  <div className="space-y-4 p-5">
                    {selectedTemplate ? (
                      <>
                        <section className="rounded-lg border border-slate-200 bg-white p-5">
                          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                            <div>
                              <h3 className="text-xl font-bold text-slate-950">{selectedTemplate.name}</h3>
                              <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">
                                {selectedTemplate.description || "暂无描述"}
                              </p>
                            </div>
                            <div className="flex flex-wrap gap-2">
                              <Button variant="outline" size="sm" onClick={() => copyText(selectedTemplate.content, "已复制模板内容")}>
                                <Copy className="mr-2 h-4 w-4" />
                                复制内容
                              </Button>
                              <Button variant="outline" size="sm" onClick={() => setWorkspaceMode("template-edit")}>
                                <Pencil className="mr-2 h-4 w-4" />
                                编辑模板
                              </Button>
                            </div>
                          </div>
                          <div className="mt-5 grid gap-3 lg:grid-cols-2">
                            <TemplateMetaRow label="模板目录" value={selectedTemplatePath} />
                            <TemplateMetaRow label="模板文件" value={selectedTemplateFile} />
                            <TemplateMetaRow label="输出格式" value={selectedTemplate.output_format} />
                            <TemplateMetaRow label="排序" value={String(selectedTemplate.sort_order ?? "暂无")} />
                          </div>
                        </section>

                        <section className="rounded-lg border border-slate-200 bg-white p-5">
                          <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-slate-900">
                            <Code2 className="h-4 w-4 text-teal-700" />
                            模板内容
                          </div>
                          <pre className="max-h-[56vh] overflow-auto whitespace-pre-wrap rounded-md bg-slate-950 p-5 text-sm leading-7 text-slate-100">
                            {selectedTemplate.content}
                          </pre>
                        </section>
                      </>
                    ) : (
                      <div className="rounded-lg border border-slate-200 bg-white p-8 text-center text-slate-500">
                        当前使用系统默认模板，无可查看的本地模板记录。
                      </div>
                    )}
                  </div>
                </ScrollArea>
              )}

              {workspaceMode === "template-edit" && (
                <ScrollArea className="h-full">
                  <div className="space-y-4 p-5">
                    {selectedTemplate && templateForm ? (
                      <>
                        <section className="grid gap-4 rounded-lg border border-slate-200 bg-white p-5 lg:grid-cols-2">
                          <div className="space-y-2">
                            <Label>模板名称</Label>
                            <Input
                              value={templateForm.name}
                              onChange={(event) => setTemplateForm((current) => current ? { ...current, name: event.target.value } : current)}
                            />
                          </div>
                          <div className="space-y-2">
                            <Label>报告类型</Label>
                            <Input
                              value={templateForm.report_type || ""}
                              onChange={(event) => setTemplateForm((current) => current ? { ...current, report_type: event.target.value } : current)}
                            />
                          </div>
                          <div className="space-y-2 lg:col-span-2">
                            <Label>描述</Label>
                            <Textarea
                              className="min-h-24 font-sans"
                              value={templateForm.description || ""}
                              onChange={(event) => setTemplateForm((current) => current ? { ...current, description: event.target.value } : current)}
                            />
                          </div>
                          <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
                            <div>
                              <div className="text-sm font-semibold text-slate-900">启用模板</div>
                              <div className="text-xs text-slate-500">关闭后不会出现在常规导出选择里</div>
                            </div>
                            <Switch
                              checked={Boolean(templateForm.is_active)}
                              onCheckedChange={(checked) => setTemplateForm((current) => current ? { ...current, is_active: checked } : current)}
                            />
                          </div>
                          <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
                            <div>
                              <div className="text-sm font-semibold text-slate-900">设为默认模板</div>
                              <div className="text-xs text-slate-500">会作为导出报告的默认选择</div>
                            </div>
                            <Switch
                              checked={Boolean(templateForm.is_default)}
                              onCheckedChange={(checked) => setTemplateForm((current) => current ? { ...current, is_default: checked } : current)}
                            />
                          </div>
                        </section>

                        <section className="rounded-lg border border-slate-200 bg-white p-5">
                          <div className="mb-3 flex items-center justify-between gap-3">
                            <div>
                              <Label className="text-sm font-semibold text-slate-900">模板内容</Label>
                              <p className="mt-1 text-xs text-slate-500">
                                支持变量占位符，例如 {"{{summary}}"}、{"{{findings}}"}、{"{{remediation}}"}。
                              </p>
                            </div>
                            <Button variant="outline" size="sm" onClick={() => setTemplateForm(toTemplatePayload(selectedTemplate))}>
                              重置
                            </Button>
                          </div>
                          <Textarea
                            className="min-h-[46vh] resize-y rounded-md border-slate-200 bg-slate-950 font-mono text-sm leading-7 text-slate-100"
                            value={templateForm.content}
                            onChange={(event) => setTemplateForm((current) => current ? { ...current, content: event.target.value } : current)}
                          />
                          <div className="mt-4 flex justify-end gap-3">
                            <Button variant="outline" onClick={() => setWorkspaceMode("template-view")}>
                              取消
                            </Button>
                            <Button onClick={handleSaveTemplate} disabled={savingTemplate || !templateForm.name.trim() || !templateForm.content.trim()}>
                              {savingTemplate ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
                              保存模板
                            </Button>
                          </div>
                        </section>
                      </>
                    ) : (
                      <div className="rounded-lg border border-slate-200 bg-white p-8 text-center text-slate-500">
                        当前使用系统默认模板，无法直接编辑。
                      </div>
                    )}
                  </div>
                </ScrollArea>
              )}
            </div>

            <div className="flex items-center justify-between border-t border-slate-200 bg-white px-5 py-4">
              <div className="flex min-w-0 items-center gap-2 text-sm text-slate-500">
                <CheckCircle2 className="h-4 w-4 text-teal-700" />
                <span className="truncate">
                  将导出 {FORMAT_OPTIONS[activeFormat].label}，模板：{selectedTemplate?.name || "系统默认模板"}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <Button variant="ghost" onClick={() => onOpenChange(false)}>
                  取消
                </Button>
                <Button onClick={handleDownload} disabled={downloading || preview.loading || !preview.content}>
                  {downloading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Download className="mr-2 h-4 w-4" />}
                  下载 {FORMAT_OPTIONS[activeFormat].label}
                </Button>
              </div>
            </div>
          </main>
        </div>
      </DialogContent>
    </Dialog>
  );
});

export default ReportExportDialog;
