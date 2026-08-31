import { useEffect, useMemo, useState } from 'react';
import {
  ArrowUpRight,
  FileText,
  FolderOpen,
  Pencil,
  Plus,
  RefreshCw,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { toast } from 'sonner';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  createReportTemplate,
  deleteReportTemplate,
  getReportTemplates,
  resyncReportTemplates,
  updateReportTemplate,
  type ReportTemplate,
  type ReportTemplatePayload,
} from '@/shared/api/reportTemplates';
import { syncLocalLibraries } from '@/shared/api/modelConfig';

const HOST_PROJECT_ROOT = (import.meta.env.VITE_HOST_PROJECT_ROOT as string | undefined) || '';
const EMPTY_TEMPLATE: ReportTemplatePayload = {
  name: '',
  description: '',
  report_type: 'final_vulnerability_report',
  output_format: 'markdown',
  content: '# 执行摘要\n\n{{summary}}\n\n# 漏洞清单\n\n{{findings}}\n\n# 修复建议\n\n{{remediation}}\n', 
  variables: {},
  metadata_json: {},
  is_active: true,
  is_default: false,
  sort_order: 100,
};

function errorMessage(error: unknown, fallback: string) {
  if (typeof error === 'object' && error && 'response' in error) {
    const detail = (error as { response?: { data?: { detail?: string } } }).response?.data?.detail;
    if (detail) return detail;
  }
  return error instanceof Error ? error.message : fallback;
}

function buildAbsolutePath(relativePath: string) {
  if (!relativePath) return '';
  return HOST_PROJECT_ROOT
    ? `${HOST_PROJECT_ROOT.replace(/[\\/]+$/, '')}/${relativePath}`.replace(/\//g, '\\')
    : relativePath;
}

function toFileHref(path: string) {
  return path ? `file:///${path.replace(/\\/g, '/')}` : '#';
}

function copyText(value: string, message: string) {
  if (!value) return;
  navigator.clipboard.writeText(value).then(() => toast.success(message)).catch(() => toast.error('复制失败'));
}

function metadataString(template: ReportTemplate, key: string) {
  const value = template.metadata_json?.[key];
  return typeof value === 'string' ? value : '';
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-[rgba(228,214,192,.92)] bg-white/90 px-4 py-3">
      <div className="text-[11px] uppercase tracking-[0.18em] text-[#9a7e60]">{label}</div>
      <div className="mt-2 break-all text-sm leading-6 text-[#554434]">{value || '暂无'}</div>
    </div>
  );
}

export default function ReportTemplatesPage() {
  const [templates, setTemplates] = useState<ReportTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [editorOpen, setEditorOpen] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const [editingTemplate, setEditingTemplate] = useState<ReportTemplate | null>(null);
  const [detailTemplate, setDetailTemplate] = useState<ReportTemplate | null>(null);
  const [form, setForm] = useState<ReportTemplatePayload>(EMPTY_TEMPLATE);
  const [syncing, setSyncing] = useState(false);

  const rootPath = useMemo(() => buildAbsolutePath('report_template_library'), []);

  const loadTemplatesPage = async (autoSyncIfEmpty = false) => {
    try {
      setLoading(true);
      const response = await getReportTemplates();
      setTemplates(response.items);
      if (autoSyncIfEmpty && response.items.length === 0) {
        await syncLocalLibraries();
        await resyncReportTemplates();
        const refreshed = await getReportTemplates();
        setTemplates(refreshed.items);
      }
    } catch (error) {
      toast.error(errorMessage(error, '加载报告模板失败'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTemplatesPage(true);
  }, []);

  const stats = useMemo(() => ({
    total: templates.length,
    active: templates.filter((template) => template.is_active).length,
    defaults: templates.filter((template) => template.is_default).length,
  }), [templates]);

  const openCreate = () => {
    setEditingTemplate(null);
    setForm(EMPTY_TEMPLATE);
    setEditorOpen(true);
  };

  const openEdit = (template: ReportTemplate) => {
    setEditingTemplate(template);
    setForm({
      name: template.name,
      description: template.description,
      report_type: template.report_type,
      output_format: template.output_format,
      content: template.content,
      variables: template.variables,
      metadata_json: template.metadata_json,
      is_active: template.is_active,
      is_default: template.is_default,
      sort_order: template.sort_order,
    });
    setEditorOpen(true);
  };

  const saveTemplate = async () => {
    try {
      if (editingTemplate) {
        await updateReportTemplate(editingTemplate.id, form);
        toast.success('报告模板已更新');
      } else {
        await createReportTemplate(form);
        toast.success('报告模板已创建');
      }
      await syncLocalLibraries();
      await resyncReportTemplates();
      setEditorOpen(false);
      await loadTemplatesPage();
    } catch (error) {
      toast.error(errorMessage(error, '保存报告模板失败'));
    }
  };

  const removeTemplate = async (template: ReportTemplate) => {
    try {
      await deleteReportTemplate(template.id);
      await syncLocalLibraries();
      await resyncReportTemplates();
      toast.success('报告模板已删除');
      await loadTemplatesPage();
    } catch (error) {
      toast.error(errorMessage(error, '删除报告模板失败'));
    }
  };

  const syncTemplateDirectory = async () => {
    try {
      setSyncing(true);
      await syncLocalLibraries();
      await resyncReportTemplates();
      await loadTemplatesPage();
      toast.success('报告模板目录已同步到本地文件夹');
    } catch (error) {
      toast.error(errorMessage(error, '同步报告模板目录失败'));
    } finally {
      setSyncing(false);
    }
  };

  if (loading) {
    return <div className="gradient-bg min-h-screen flex items-center justify-center text-muted-foreground">正在加载报告模板...</div>;
  }

  return (
    <div className="gradient-bg min-h-screen px-6 py-8">
      <div className="mx-auto max-w-7xl space-y-6">
        <section className="rounded-[34px] border border-[rgba(214,193,160,.78)] bg-[linear-gradient(135deg,rgba(255,255,255,.98),rgba(249,243,233,.95))] p-8 shadow-[0_28px_70px_rgba(120,96,57,.11)]">
          <div className="flex flex-col gap-6 xl:flex-row xl:items-start xl:justify-between">
            <div className="max-w-3xl space-y-3">
              <div className="inline-flex items-center gap-2 rounded-full border border-[rgba(198,167,122,.45)] bg-white/80 px-4 py-1 text-xs uppercase tracking-[0.22em] text-[#8c6540]">
                <Sparkles className="h-3.5 w-3.5" /> Report Templates
              </div>
              <h1 className="text-4xl font-black tracking-tight text-[#2d241a]">报告模板</h1>
              <p className="text-sm leading-7 text-[#705d4b]">
                最终漏洞报告模板已经独立成单独菜单。每个模板都会在 `report_template_library` 下生成独立文件夹，你可以直接查看路径、打开目录并编辑模板内容。
              </p>
            </div>
            <div className="flex flex-wrap gap-3">
              <Button variant="outline" className="h-11 rounded-full" onClick={syncTemplateDirectory} disabled={syncing}>
                {syncing ? <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}同步本地目录
              </Button>
              <Button className="h-11 rounded-full bg-[#d97745] text-white hover:bg-[#c96532]" onClick={openCreate}>
                <Plus className="mr-2 h-4 w-4" /> 新建模板
              </Button>
            </div>
          </div>
        </section>

        <section className="grid gap-4 xl:grid-cols-[1.05fr_.95fr]">
          <div className="rounded-[26px] border border-[rgba(222,208,184,.9)] bg-white/90 p-5 shadow-[0_18px_36px_rgba(95,76,48,.08)]">
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-2">
                <div className="text-xs uppercase tracking-[0.22em] text-[#9a7e60]">模板根目录</div>
                <div className="break-all text-sm leading-7 text-[#5b4838]">{rootPath || '未检测到根目录'}</div>
              </div>
              <FolderOpen className="h-6 w-6 text-[#d97745]" />
            </div>
            <div className="mt-4 flex flex-wrap gap-3">
              <Button variant="outline" size="sm" onClick={() => copyText(rootPath, '已复制模板根目录')}>复制路径</Button>
              <Button asChild variant="outline" size="sm">
                <a href={toFileHref(rootPath)} target="_blank" rel="noreferrer">打开文件夹<ArrowUpRight className="ml-2 h-4 w-4" /></a>
              </Button>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <div className="rounded-[26px] border border-[rgba(222,208,184,.9)] bg-white/90 p-5 shadow-[0_18px_36px_rgba(95,76,48,.08)]">
              <div className="text-xs uppercase tracking-[0.22em] text-[#9a7e60]">模板总数</div>
              <div className="mt-3 text-3xl font-black text-[#d97745]">{stats.total}</div>
            </div>
            <div className="rounded-[26px] border border-[rgba(222,208,184,.9)] bg-white/90 p-5 shadow-[0_18px_36px_rgba(95,76,48,.08)]">
              <div className="text-xs uppercase tracking-[0.22em] text-[#9a7e60]">已启用</div>
              <div className="mt-3 text-3xl font-black text-[#d97745]">{stats.active}</div>
            </div>
            <div className="rounded-[26px] border border-[rgba(222,208,184,.9)] bg-white/90 p-5 shadow-[0_18px_36px_rgba(95,76,48,.08)]">
              <div className="text-xs uppercase tracking-[0.22em] text-[#9a7e60]">默认模板</div>
              <div className="mt-3 text-3xl font-black text-[#d97745]">{stats.defaults}</div>
            </div>
          </div>
        </section>

        <section className="grid gap-4 md:grid-cols-2">
          {templates.map((template) => {
            const relativePath = metadataString(template, 'workspace_relative_path');
            const filePath = metadataString(template, 'workspace_file_path');
            const displayPath = buildAbsolutePath(relativePath);
            const displayFile = buildAbsolutePath(filePath);
            return (
              <article key={template.id} className="rounded-[28px] border border-[rgba(223,210,188,.92)] bg-[#fffdf9] p-5 shadow-[0_16px_30px_rgba(94,76,52,.06)] transition hover:-translate-y-1 hover:shadow-[0_20px_40px_rgba(94,76,52,.12)]">
                <div className="flex items-start justify-between gap-4">
                  <div className="space-y-3">
                    <div className="flex items-center gap-3">
                      <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-[#f6efe6] text-[#d97745]"><FileText className="h-5 w-5" /></div>
                      <div>
                        <h3 className="text-xl font-bold text-[#2f2318]">{template.name}</h3>
                        <p className="text-sm text-[#7f6956]">{template.report_type}</p>
                      </div>
                    </div>
                    <p className="line-clamp-3 text-sm leading-7 text-[#6b5846]">{template.description || '暂无描述'}</p>
                    <div className="flex flex-wrap gap-2">
                      {template.is_default && <Badge className="bg-[#e7f6ed] text-[#2c8c59]">默认模板</Badge>}
                      {template.is_active ? <Badge className="bg-[#f6efe6] text-[#8c6540]">启用中</Badge> : <Badge variant="outline">已停用</Badge>}
                      {template.is_system && <Badge className="bg-[#e8f0ff] text-[#355c9a]">system</Badge>}
                    </div>
                  </div>
                  <Switch checked={template.is_active} disabled />
                </div>

                <div className="mt-4 grid gap-3">
                  <DetailRow label="模板目录" value={displayPath} />
                  <DetailRow label="模板文件" value={displayFile} />
                </div>

                <pre className="mt-4 line-clamp-6 overflow-hidden rounded-2xl bg-[#f9f3eb] p-4 text-xs leading-6 text-[#5e4b39]">{template.content}</pre>

                <div className="mt-5 flex flex-wrap gap-3">
                  <Button size="sm" className="bg-[#d97745] text-white hover:bg-[#c96532]" onClick={() => { setDetailTemplate(template); setDetailOpen(true); }}>查看模板</Button>
                  <Button asChild variant="outline" size="sm">
                    <a href={toFileHref(displayPath)} target="_blank" rel="noreferrer">打开文件夹<ArrowUpRight className="ml-2 h-4 w-4" /></a>
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => copyText(displayFile || displayPath, '已复制模板路径')}>复制路径</Button>
                  <Button variant="outline" size="sm" onClick={() => openEdit(template)}><Pencil className="mr-2 h-4 w-4" /> 编辑</Button>
                  {!template.is_system && <Button variant="outline" size="sm" onClick={() => removeTemplate(template)}><Trash2 className="mr-2 h-4 w-4" /> 删除</Button>}
                </div>
              </article>
            );
          })}
        </section>

        <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
          <DialogContent className="max-w-6xl border-[rgba(215,194,161,.8)] bg-[linear-gradient(180deg,rgba(255,255,255,.99),rgba(249,243,233,.98))] sm:max-h-[88vh] overflow-hidden">
            <DialogHeader>
              <DialogTitle className="text-2xl font-black text-[#2f2418]">模板详情</DialogTitle>
            </DialogHeader>
            {detailTemplate && (
              <div className="grid gap-5 lg:grid-cols-[0.92fr_1.08fr]">
                <div className="space-y-4 rounded-[24px] border border-[rgba(223,210,188,.92)] bg-[#fffdfa] p-5">
                  <div>
                    <h3 className="text-2xl font-bold text-[#2f2318]">{detailTemplate.name}</h3>
                    <p className="mt-2 text-sm leading-7 text-[#6d5847]">{detailTemplate.description || '暂无描述'}</p>
                  </div>
                  <DetailRow label="模板目录" value={buildAbsolutePath(metadataString(detailTemplate, 'workspace_relative_path'))} />
                  <DetailRow label="模板文件" value={buildAbsolutePath(metadataString(detailTemplate, 'workspace_file_path'))} />
                  <DetailRow label="输出格式" value={detailTemplate.output_format} />
                </div>
                <div className="rounded-[24px] border border-[rgba(223,210,188,.92)] bg-[#fffdfa] p-5">
                  <div className="flex items-center justify-between gap-3">
                    <h4 className="text-lg font-bold text-[#2f2318]">模板内容</h4>
                    <Button variant="outline" size="sm" onClick={() => copyText(detailTemplate.content, '已复制模板内容')}>复制内容</Button>
                  </div>
                  <ScrollArea className="mt-4 h-[58vh] rounded-2xl border border-[rgba(232,220,201,.9)] bg-white p-4">
                    <pre className="whitespace-pre-wrap text-sm leading-7 text-[#584736]">{detailTemplate.content}</pre>
                  </ScrollArea>
                </div>
              </div>
            )}
          </DialogContent>
        </Dialog>

        <Dialog open={editorOpen} onOpenChange={setEditorOpen}>
          <DialogContent className="max-w-6xl border-[rgba(215,194,161,.8)] bg-[linear-gradient(180deg,rgba(255,255,255,.99),rgba(249,243,233,.98))] sm:max-h-[90vh] overflow-hidden">
            <DialogHeader>
              <DialogTitle className="text-2xl font-black text-[#2f2418]">{editingTemplate ? '编辑报告模板' : '新建报告模板'}</DialogTitle>
            </DialogHeader>
            <div className="grid gap-5 lg:grid-cols-[0.88fr_1.12fr]">
              <div className="space-y-4 rounded-[24px] border border-[rgba(223,210,188,.92)] bg-[#fffdfa] p-5">
                <div className="rounded-[22px] border border-[rgba(228,214,192,.92)] bg-[linear-gradient(180deg,#fffdfa,#fbf3e7)] p-4">
                  <div className="text-xs uppercase tracking-[0.22em] text-[#9a7e60]">配置面板</div>
                  <div className="mt-2 text-sm leading-7 text-[#6d5847]">先定义模板名称、说明和默认状态，再在右侧编写最终输出格式。</div>
                </div>
                <div className="space-y-2"><Label>名称</Label><Input value={form.name} onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))} placeholder="例如：默认代码审计报告" /></div>
                <div className="space-y-2"><Label>描述</Label><Textarea rows={5} value={form.description || ''} onChange={(event) => setForm((prev) => ({ ...prev, description: event.target.value }))} placeholder="一句话说明模板适用场景。" /></div>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="flex items-center justify-between rounded-2xl border border-[rgba(222,210,187,.9)] bg-[#fffdfa] px-4 py-3">
                    <div>
                      <p className="font-semibold text-[#5d4a36]">启用</p>
                      <p className="text-xs text-[#856c54]">关闭后不参与导出</p>
                    </div>
                    <Switch checked={Boolean(form.is_active)} onCheckedChange={(checked) => setForm((prev) => ({ ...prev, is_active: checked }))} />
                  </div>
                  <div className="flex items-center justify-between rounded-2xl border border-[rgba(222,210,187,.9)] bg-[#fffdfa] px-4 py-3">
                    <div>
                      <p className="font-semibold text-[#5d4a36]">设为默认模板</p>
                      <p className="text-xs text-[#856c54]">会替换现有默认模板</p>
                    </div>
                    <Switch checked={Boolean(form.is_default)} onCheckedChange={(checked) => setForm((prev) => ({ ...prev, is_default: checked }))} />
                  </div>
                </div>
              </div>
              <div className="rounded-[24px] border border-[rgba(223,210,188,.92)] bg-[#fffdfa] p-5">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h4 className="text-lg font-bold text-[#2f2318]">模板内容</h4>
                    <p className="mt-1 text-sm text-[#6d5847]">支持 <code>{"{{summary}}"}</code>、<code>{"{{findings}}"}</code>、<code>{"{{remediation}}"}</code> 等变量。</p>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => setForm((prev) => ({
                    ...prev,
                    content: '# 执行摘要\n\n{{summary}}\n\n# 漏洞清单\n\n{{findings}}\n\n# 修复建议\n\n{{remediation}}\n\n# 附录\n\n{{appendix}}\n',
                  }))}>
                    <Sparkles className="mr-2 h-4 w-4" /> 填充示例
                  </Button>
                </div>
                <Textarea className="mt-4 min-h-[56vh]" value={form.content} onChange={(event) => setForm((prev) => ({ ...prev, content: event.target.value }))} placeholder="# 执行摘要\n\n{{summary}}" />
                <div className="mt-4 flex justify-end gap-3"><Button variant="outline" onClick={() => setEditorOpen(false)}>取消</Button><Button className="bg-[#d97745] text-white hover:bg-[#c96532]" onClick={saveTemplate}>保存模板</Button></div>
              </div>
            </div>
          </DialogContent>
        </Dialog>
      </div>
    </div>
  );
}

