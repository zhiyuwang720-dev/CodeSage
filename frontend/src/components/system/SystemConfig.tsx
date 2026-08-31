import { useEffect, useMemo, useState } from 'react';
import {
  Check,
  CheckCircle2,
  ChevronDown,
  Eye,
  Info,
  Loader2,
  MessageSquareMore,
  PencilLine,
  Plus,
  Save,
  ServerCog,
  Settings2,
  Trash2,
  Wand2,
} from 'lucide-react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  type AgentChatMessage,
  type AgentModelConfig,
  type AgentModelTestResponse,
  type AgentType,
  type ModelProfileConfig,
  type ProviderOption,
  getModelConfig,
  getModelProviders,
  saveModelConfig,
  testAgentModel,
  testGlobalModel,
} from '@/shared/api/modelConfig';
import { cn } from '@/shared/utils/utils';

const AGENT_ORDER: AgentType[] = ['orchestrator', 'recon', 'scan', 'triage', 'finding', 'verification'];
const DEFAULT_GLOBAL_ENV = '{}';
const DEFAULT_TEST_PROMPT =
  'Please tell me which skill metadata you currently know, what your current role is, and give one concrete example of how you would use those skills.';

const DEFAULT_ENDPOINT_PROTOCOL = 'openai_chat';

const ENDPOINT_PROTOCOL_LABELS: Record<string, string> = {
  openai_chat: 'OpenAI Chat',
  openai_responses: 'OpenAI Responses',
  anthropic_messages: 'Anthropic Messages',
  gemini_native: 'Gemini Native',
  native: 'Native',
};

const TOOL_MESSAGE_FORMAT_LABELS: Record<string, string> = {
  auto: 'Auto',
  openai_tools: 'OpenAI Tools',
  anthropic_blocks: 'Anthropic Blocks',
  gemini_parts: 'Gemini Parts',
  responses_items: 'Responses Items',
  legacy_text: 'Legacy Text',
};

const AGENT_META: Record<AgentType, { label: string; description: string }> = {
  orchestrator: { label: 'Orchestrator', description: '编排审计流程和阶段分发' },
  recon: { label: 'Recon', description: '负责信息收集和入口发现' },
  scan: { label: 'Scan', description: '负责调用扫描工具' },
  triage: { label: 'Triage', description: '负责误报过滤和风险收束' },
  finding: { label: 'Finding', description: '负责漏洞深挖与报告生成' },
  verification: { label: 'Verification', description: '负责问题验证与闭环确认' },
};

const DEFAULT_AGENT_CONFIG: AgentModelConfig = {
  enabled: false,
  llmProvider: '',
  llmApiKey: '',
  llmModel: '',
  llmBaseUrl: '',
  llmTimeout: null,
  llmTemperature: null,
  llmTopP: null,
  llmMaxTokens: null,
  endpointProtocol: DEFAULT_ENDPOINT_PROTOCOL,
  toolMessageFormat: 'auto',
  maxIterations: null,
  env: {},
  alwaysThinkingEnabled: false,
};

type GlobalModelConfig = Omit<ModelProfileConfig, 'id' | 'name' | 'isDefault'>;
type ConfigScope = 'global' | AgentType;

const HERO_CLASS =
  'relative overflow-hidden rounded-[28px] border border-slate-200 bg-[linear-gradient(135deg,rgba(241,253,248,.96),rgba(255,255,255,.98)_54%,rgba(248,251,250,.96))] shadow-[0_20px_52px_rgba(15,23,42,.06)]';
const PANEL_CLASS =
  'rounded-[28px] border border-[rgba(190,209,200,.82)] bg-[linear-gradient(145deg,rgba(255,255,255,.97),rgba(246,251,249,.94))] shadow-[0_22px_60px_rgba(61,85,75,.08)]';
const SOFT_PANEL_CLASS =
  'rounded-[22px] border border-[rgba(202,216,211,.9)] bg-white/88 shadow-[0_12px_30px_rgba(15,23,42,.04)]';
const INPUT_CLASS =
  'h-11 rounded-2xl border-slate-200 bg-white/95 font-sans text-[15px] text-slate-800 shadow-none placeholder:text-slate-400 focus:border-primary';
const TEXTAREA_CLASS =
  'min-h-[140px] rounded-2xl border-slate-200 bg-white/95 font-mono text-[13px] leading-6 text-slate-800 shadow-none placeholder:text-slate-400 focus:border-primary';

function stringifyEnvPayload(value?: Record<string, string>): string {
  const payload = value && Object.keys(value).length > 0 ? value : {};
  return JSON.stringify(payload, null, 2);
}

function parseEnvPayload(raw: string, label: string): Record<string, string> {
  const trimmed = raw.trim();
  if (!trimmed) return {};

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error(`${label} 必须是合法 JSON`);
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label} 必须是 JSON 对象`);
  }

  return Object.fromEntries(
    Object.entries(parsed as Record<string, unknown>)
      .filter(([, value]) => value !== null && value !== '')
      .map(([key, value]) => [key, String(value)]),
  );
}

function cloneAgentConfig(input?: Partial<AgentModelConfig>): AgentModelConfig {
  return {
    ...DEFAULT_AGENT_CONFIG,
    ...(input || {}),
    env: input?.env || {},
    alwaysThinkingEnabled: false,
  };
}

function createDefaultAgentConfigs(): Record<AgentType, AgentModelConfig> {
  return Object.fromEntries(AGENT_ORDER.map((agent) => [agent, cloneAgentConfig()])) as Record<
    AgentType,
    AgentModelConfig
  >;
}

function createDefaultAgentEnvTexts(): Record<AgentType, string> {
  return Object.fromEntries(AGENT_ORDER.map((agent) => [agent, DEFAULT_GLOBAL_ENV])) as Record<AgentType, string>;
}

function uniqueModels(models?: string[]): string[] {
  return Array.from(new Set((models || []).filter(Boolean)));
}

function normalizeEndpointProtocol(value?: string): string {
  const aliases: Record<string, string> = {
    openai: 'openai_chat',
    openai_compatible: 'openai_chat',
    'openai-compatible': 'openai_chat',
    chat_completions: 'openai_chat',
    anthropic: 'anthropic_messages',
    claude: 'anthropic_messages',
    google: 'gemini_native',
    gemini: 'gemini_native',
  };
  const raw = (value || '').trim().toLowerCase();
  return aliases[raw] || raw || DEFAULT_ENDPOINT_PROTOCOL;
}

function normalizeToolMessageFormat(value?: string): string {
  const aliases: Record<string, string> = {
    follow_protocol: 'auto',
    xml: 'legacy_text',
    json: 'legacy_text',
    openai: 'openai_tools',
    anthropic: 'anthropic_blocks',
    gemini: 'gemini_parts',
  };
  const raw = (value || '').trim().toLowerCase();
  return aliases[raw] || raw || 'auto';
}

function protocolOptionsForProvider(provider?: ProviderOption): string[] {
  const options = provider?.supported_endpoint_protocols?.length
    ? provider.supported_endpoint_protocols
    : ['openai_chat', 'openai_responses', 'anthropic_messages', 'gemini_native'];
  return uniqueModels(options.map(normalizeEndpointProtocol));
}

function createProfileId(): string {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  return `profile-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeProfiles(profiles: ModelProfileConfig[]): ModelProfileConfig[] {
  let defaultSeen = false;
  const normalized = profiles.map((profile) => {
    const isDefault = Boolean(profile.isDefault) && !defaultSeen;
    defaultSeen = defaultSeen || isDefault;
    return { ...profile, isDefault };
  });
  if (normalized.length > 0 && !defaultSeen) {
    normalized[0] = { ...normalized[0], isDefault: true };
  }
  return normalized;
}

function cloneModelProfile(input?: Partial<ModelProfileConfig>): ModelProfileConfig {
  return {
    id: input?.id || createProfileId(),
    name: input?.name?.trim() || '未命名方案',
    isDefault: Boolean(input?.isDefault),
    llmProvider: input?.llmProvider || '',
    llmApiKey: input?.llmApiKey || '',
    llmModel: input?.llmModel || '',
    llmBaseUrl: input?.llmBaseUrl || '',
    llmTimeout: input?.llmTimeout ?? null,
    llmTemperature: input?.llmTemperature ?? null,
    llmTopP: input?.llmTopP ?? null,
    llmMaxTokens: input?.llmMaxTokens ?? null,
    endpointProtocol: normalizeEndpointProtocol(input?.endpointProtocol),
    toolMessageFormat: normalizeToolMessageFormat(input?.toolMessageFormat),
    env: input?.env || {},
  };
}

function buildProfileFromGlobal(
  id: string,
  name: string,
  isDefault: boolean,
  config: GlobalModelConfig,
  env: Record<string, string>,
) {
  return cloneModelProfile({
    id,
    name,
    isDefault,
    llmProvider: config.llmProvider,
    llmApiKey: config.llmApiKey,
    llmModel: config.llmModel,
    llmBaseUrl: config.llmBaseUrl,
    llmTimeout: config.llmTimeout,
    llmTemperature: config.llmTemperature,
    llmTopP: config.llmTopP,
    llmMaxTokens: config.llmMaxTokens,
    endpointProtocol: config.endpointProtocol,
    toolMessageFormat: config.toolMessageFormat,
    env,
  });
}

interface ModelSearchSelectProps {
  value: string;
  models: string[];
  onChange: (value: string) => void;
  placeholder?: string;
}

function ModelSearchSelect({ value, models, onChange, placeholder = '搜索推荐模型' }: ModelSearchSelectProps) {
  const [open, setOpen] = useState(false);
  const options = useMemo(() => uniqueModels(models), [models]);
  const selectedRecommended = options.includes(value) ? value : '';

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          role="combobox"
          className={cn(
            'h-11 w-full justify-between rounded-2xl border-slate-200 bg-white/95 px-4 font-sans text-[15px] font-medium text-slate-800 shadow-none hover:bg-emerald-50/50',
            !selectedRecommended && 'text-slate-500',
          )}
        >
          <span className="truncate">{selectedRecommended || placeholder}</span>
          <ChevronDown className="h-4 w-4 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[--radix-popover-trigger-width] rounded-2xl border-slate-200 bg-white p-0 font-sans shadow-[0_16px_36px_rgba(15,23,42,.12)]"
      >
        <Command className="rounded-2xl bg-transparent">
          <CommandInput className="h-11 text-sm" placeholder="搜索当前 Provider 的推荐模型..." />
          <CommandList className="max-h-64">
            <CommandEmpty className="py-6 text-center text-sm text-slate-500">没有匹配的推荐模型</CommandEmpty>
            <CommandGroup heading="推荐模型">
              {options.map((model) => (
                <CommandItem
                  key={model}
                  value={model}
                  className="rounded-xl px-3 py-2.5 text-[14px]"
                  onSelect={() => {
                    onChange(model);
                    setOpen(false);
                  }}
                >
                  <Check className={cn('h-4 w-4 text-primary', value === model ? 'opacity-100' : 'opacity-0')} />
                  <span className="truncate">{model}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

export function SystemConfig() {
  const [providers, setProviders] = useState<ProviderOption[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testingGlobal, setTestingGlobal] = useState(false);
  const [testingAgent, setTestingAgent] = useState(false);

  const [globalConfig, setGlobalConfig] = useState<GlobalModelConfig>({
    llmProvider: 'openai',
    llmApiKey: '',
    llmModel: '',
    llmBaseUrl: '',
    llmTimeout: 150000,
    llmTemperature: null,
    llmTopP: null,
    llmMaxTokens: 4096,
    endpointProtocol: DEFAULT_ENDPOINT_PROTOCOL,
    toolMessageFormat: 'auto',
    env: {},
  });
  const [globalEnvText, setGlobalEnvText] = useState(DEFAULT_GLOBAL_ENV);
  const [agentConfigs, setAgentConfigs] = useState<Record<AgentType, AgentModelConfig>>(createDefaultAgentConfigs);
  const [agentEnvTexts, setAgentEnvTexts] = useState<Record<AgentType, string>>(createDefaultAgentEnvTexts);
  const [activeConfigScope, setActiveConfigScope] = useState<ConfigScope>('global');

  const [modelProfiles, setModelProfiles] = useState<ModelProfileConfig[]>([]);
  const [selectedProfileId, setSelectedProfileId] = useState('');
  const [profileDialogOpen, setProfileDialogOpen] = useState(false);
  const [profileManagerOpen, setProfileManagerOpen] = useState(false);
  const [profileDetailOpen, setProfileDetailOpen] = useState(false);
  const [profileEditOpen, setProfileEditOpen] = useState(false);
  const [activeProfileId, setActiveProfileId] = useState('');
  const [editProfileName, setEditProfileName] = useState('');
  const [editProfileIsDefault, setEditProfileIsDefault] = useState(false);
  const [editProfileDraft, setEditProfileDraft] = useState<ModelProfileConfig | null>(null);
  const [editProfileEnvText, setEditProfileEnvText] = useState(DEFAULT_GLOBAL_ENV);
  const [profileName, setProfileName] = useState('');
  const [profileIsDefault, setProfileIsDefault] = useState(false);

  const [testDialogOpen, setTestDialogOpen] = useState(false);
  const [testAgent, setTestAgent] = useState<AgentType>('finding');
  const [testPrompt, setTestPrompt] = useState(DEFAULT_TEST_PROMPT);
  const [testMessages, setTestMessages] = useState<AgentChatMessage[]>([]);
  const [testResult, setTestResult] = useState<AgentModelTestResponse | null>(null);

  const providerMap = useMemo(
    () =>
      providers.reduce<Record<string, ProviderOption>>((acc, provider) => {
        acc[provider.value] = provider;
        return acc;
      }, {}),
    [providers],
  );
  const defaultProfile = modelProfiles.find((profile) => profile.isDefault) || modelProfiles[0] || null;
  const activeManagedProfile = useMemo(
    () => modelProfiles.find((profile) => profile.id === activeProfileId) || null,
    [activeProfileId, modelProfiles],
  );

  const isGlobalScope = activeConfigScope === 'global';
  const activeAgent = isGlobalScope ? null : activeConfigScope;
  const activeAgentConfig = activeAgent ? agentConfigs[activeAgent] : null;
  const activeProviderValue = isGlobalScope
    ? globalConfig.llmProvider || ''
    : activeAgentConfig?.llmProvider || globalConfig.llmProvider || '';
  const activeModelValue = isGlobalScope ? globalConfig.llmModel || '' : activeAgentConfig?.llmModel || '';
  const activeProvider = providerMap[activeProviderValue];
  const activeProtocolOptions = protocolOptionsForProvider(activeProvider);
  const activeEnvText = isGlobalScope ? globalEnvText : agentEnvTexts[activeAgent as AgentType] || DEFAULT_GLOBAL_ENV;

  useEffect(() => {
    void loadPage();
  }, []);

  async function loadPage() {
    setLoading(true);
    try {
      const [providerResponse, configResponse] = await Promise.all([getModelProviders(), getModelConfig()]);
      setProviders(providerResponse.providers || []);

      const llmConfig = configResponse.llmConfig || {};
      const nextGlobal: GlobalModelConfig = {
        llmProvider: llmConfig.llmProvider || 'openai',
        llmApiKey: llmConfig.llmApiKey || '',
        llmModel: llmConfig.llmModel || '',
        llmBaseUrl: llmConfig.llmBaseUrl || '',
        llmTimeout: llmConfig.llmTimeout ?? 150000,
        llmTemperature: llmConfig.llmTemperature ?? null,
        llmTopP: llmConfig.llmTopP ?? null,
        llmMaxTokens: llmConfig.llmMaxTokens ?? 4096,
        endpointProtocol: normalizeEndpointProtocol(llmConfig.endpointProtocol),
        toolMessageFormat: normalizeToolMessageFormat(llmConfig.toolMessageFormat),
        env: llmConfig.env || {},
      };
      setGlobalConfig(nextGlobal);
      setGlobalEnvText(stringifyEnvPayload(nextGlobal.env));

      const loadedAgentConfigs = llmConfig.agentConfigs || {};
      const nextAgentConfigs = createDefaultAgentConfigs();
      const nextAgentEnvTexts = createDefaultAgentEnvTexts();
      AGENT_ORDER.forEach((agent) => {
        const config = cloneAgentConfig(loadedAgentConfigs[agent]);
        nextAgentConfigs[agent] = config;
        nextAgentEnvTexts[agent] = stringifyEnvPayload(config.env);
      });
      setAgentConfigs(nextAgentConfigs);
      setAgentEnvTexts(nextAgentEnvTexts);

      setModelProfiles(
        normalizeProfiles(Array.isArray(llmConfig.modelProfiles) ? llmConfig.modelProfiles.map(cloneModelProfile) : []),
      );
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '模型配置加载失败');
    } finally {
      setLoading(false);
    }
  }

  function updateAgentConfig(agent: AgentType, patch: Partial<AgentModelConfig>) {
    setAgentConfigs((prev) => ({
      ...prev,
      [agent]: cloneAgentConfig({ ...prev[agent], ...patch }),
    }));
  }

  function updateAgentEnvText(agent: AgentType, value: string) {
    setAgentEnvTexts((prev) => ({ ...prev, [agent]: value }));
  }

  function updateActiveConfig(patch: Partial<AgentModelConfig & GlobalModelConfig>) {
    if (isGlobalScope) {
      setGlobalConfig((prev) => ({ ...prev, ...patch }));
      return;
    }
    if (activeAgent) {
      updateAgentConfig(activeAgent, { ...patch, enabled: true });
    }
  }

  function updateActiveProvider(value: string) {
    const provider = providerMap[value];
    updateActiveConfig({
      llmProvider: value,
      llmModel: provider?.default_model || '',
      endpointProtocol: normalizeEndpointProtocol(provider?.default_endpoint_protocol),
      toolMessageFormat: 'auto',
    });
  }

  function updateActiveEnvText(value: string) {
    if (isGlobalScope) {
      setGlobalEnvText(value);
      return;
    }
    if (activeAgent) {
      updateAgentEnvText(activeAgent, value);
    }
  }

  function applyProfileToGlobal(profile: ModelProfileConfig, showToast = true) {
    setGlobalConfig({
      llmProvider: profile.llmProvider || '',
      llmApiKey: profile.llmApiKey || '',
      llmModel: profile.llmModel || '',
      llmBaseUrl: profile.llmBaseUrl || '',
      llmTimeout: profile.llmTimeout ?? null,
      llmTemperature: profile.llmTemperature ?? null,
      llmTopP: profile.llmTopP ?? null,
      llmMaxTokens: profile.llmMaxTokens ?? null,
      endpointProtocol: normalizeEndpointProtocol(profile.endpointProtocol),
      toolMessageFormat: normalizeToolMessageFormat(profile.toolMessageFormat),
      env: profile.env || {},
    });
    setGlobalEnvText(stringifyEnvPayload(profile.env));
    setActiveConfigScope('global');
    if (showToast) {
      toast.success('已应用方案到全局配置，保存后生效');
    }
  }

  function handleProfileSelect(value: string) {
    if (value === 'none') {
      setSelectedProfileId('');
      return;
    }
    const profile = modelProfiles.find((item) => item.id === value);
    if (!profile) return;
    setSelectedProfileId(value);
    applyProfileToGlobal(profile);
  }

  async function saveAll() {
    setSaving(true);
    try {
      const parsedGlobalEnv = parseEnvPayload(globalEnvText, '全局 Env');
      const nextAgentConfigs = { ...agentConfigs };
      AGENT_ORDER.forEach((agent) => {
        nextAgentConfigs[agent] = {
          ...nextAgentConfigs[agent],
          env: parseEnvPayload(agentEnvTexts[agent], `${AGENT_META[agent].label} Env`),
          alwaysThinkingEnabled: false,
        };
      });
      const nextProfiles = normalizeProfiles(modelProfiles).map(cloneModelProfile);

      await saveModelConfig({
        llmConfig: {
          ...globalConfig,
          env: parsedGlobalEnv,
          alwaysThinkingEnabled: false,
          modelProfiles: nextProfiles,
          agentConfigs: nextAgentConfigs,
        },
      });
      setGlobalConfig((prev) => ({ ...prev, env: parsedGlobalEnv }));
      setAgentConfigs(nextAgentConfigs);
      setModelProfiles(nextProfiles);
      toast.success('模型配置已保存');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '保存模型配置失败');
    } finally {
      setSaving(false);
    }
  }

  function handleRestoreDefaultProfile() {
    if (!defaultProfile) {
      toast.error('暂无默认方案，请先保存一个模型方案');
      return;
    }
    setSelectedProfileId(defaultProfile.id);
    applyProfileToGlobal(defaultProfile, false);
    toast.success('已恢复默认方案，请保存模型配置后生效');
  }

  async function handleGlobalTest() {
    setTestingGlobal(true);
    try {
      const response = await testGlobalModel({
        provider: globalConfig.llmProvider || '',
        apiKey: globalConfig.llmApiKey,
        model: globalConfig.llmModel,
        baseUrl: globalConfig.llmBaseUrl,
        temperature: globalConfig.llmTemperature,
        topP: globalConfig.llmTopP,
        endpointProtocol: globalConfig.endpointProtocol,
        toolMessageFormat: globalConfig.toolMessageFormat,
        prompt: DEFAULT_TEST_PROMPT,
      });
      toast.success(response?.message || '全局模型连接成功');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '全局模型连接失败');
    } finally {
      setTestingGlobal(false);
    }
  }

  function openAgentTest(agent: AgentType) {
    setTestAgent(agent);
    setTestPrompt(DEFAULT_TEST_PROMPT);
    setTestMessages([]);
    setTestResult(null);
    setTestDialogOpen(true);
  }

  async function runAgentTest() {
    setTestingAgent(true);
    try {
      const messages = testMessages.length > 0 ? testMessages : undefined;
      const response = await testAgentModel({
        agent_type: testAgent,
        prompt: testPrompt,
        include_skills: true,
        agent_model_config: agentConfigs[testAgent],
        messages,
      });
      setTestResult(response);
      setTestMessages((prev) => [...prev, { role: 'user', content: testPrompt }, { role: 'assistant', content: response.response || '' }]);
      toast.success(response.success ? 'Agent 模型测试完成' : response.message || 'Agent 模型测试返回异常');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Agent 模型测试失败');
    } finally {
      setTestingAgent(false);
    }
  }

  function runActiveConnectionTest() {
    if (isGlobalScope) {
      void handleGlobalTest();
      return;
    }
    if (activeAgent) {
      openAgentTest(activeAgent);
    }
  }

  function handleOpenSaveProfile() {
    const selectedProfile = selectedProfileId ? modelProfiles.find((item) => item.id === selectedProfileId) : null;
    setProfileName(selectedProfile?.name || '');
    setProfileIsDefault(Boolean(selectedProfile?.isDefault) || modelProfiles.length === 0);
    setProfileDialogOpen(true);
  }

  function handleSaveProfile() {
    const name = profileName.trim();
    if (!name) {
      toast.error('请填写方案名称');
      return;
    }

    try {
      const parsedGlobalEnv = parseEnvPayload(globalEnvText, '全局 Env');
      const nextId = selectedProfileId || createProfileId();
      const shouldBeDefault = profileIsDefault || modelProfiles.length === 0;
      const nextProfile = buildProfileFromGlobal(nextId, name, shouldBeDefault, globalConfig, parsedGlobalEnv);

      setModelProfiles((prev) => {
        const exists = prev.some((item) => item.id === nextId);
        const nextProfiles = exists ? prev.map((item) => (item.id === nextId ? nextProfile : item)) : [...prev, nextProfile];
        return normalizeProfiles(
          shouldBeDefault ? nextProfiles.map((item) => ({ ...item, isDefault: item.id === nextId })) : nextProfiles,
        );
      });
      setSelectedProfileId(nextId);
      setProfileDialogOpen(false);
      toast.success('方案已暂存，请保存模型配置后生效');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '保存方案失败');
    }
  }

  function deleteProfile(profileId: string) {
    setModelProfiles((prev) => normalizeProfiles(prev.filter((profile) => profile.id !== profileId)));
    if (selectedProfileId === profileId) {
      setSelectedProfileId('');
    }
    if (activeProfileId === profileId) {
      setActiveProfileId('');
      setProfileDetailOpen(false);
      setProfileEditOpen(false);
    }
    toast.success('方案已删除，请保存模型配置后生效');
  }

  function openProfileDetail(profile: ModelProfileConfig) {
    setActiveProfileId(profile.id);
    setProfileDetailOpen(true);
  }

  function openProfileEdit(profile: ModelProfileConfig) {
    setActiveProfileId(profile.id);
    setEditProfileName(profile.name);
    setEditProfileIsDefault(Boolean(profile.isDefault));
    setEditProfileDraft(cloneModelProfile(profile));
    setEditProfileEnvText(stringifyEnvPayload(profile.env));
    setProfileEditOpen(true);
  }

  function updateEditProfileDraft(patch: Partial<ModelProfileConfig>) {
    setEditProfileDraft((prev) => (prev ? { ...prev, ...patch } : prev));
  }

  function handleSaveProfileEdit() {
    if (!activeManagedProfile || !editProfileDraft) return;

    const nextName = editProfileName.trim();
    if (!nextName) {
      toast.error('请填写方案名称');
      return;
    }

    try {
      const parsedEnv = parseEnvPayload(editProfileEnvText, '方案 Env');
      setModelProfiles((prev) =>
        normalizeProfiles(
          prev.map((profile) => {
            if (profile.id !== activeManagedProfile.id) {
              return editProfileIsDefault ? { ...profile, isDefault: false } : profile;
            }
            return cloneModelProfile({
              ...editProfileDraft,
              id: activeManagedProfile.id,
              name: nextName,
              isDefault: editProfileIsDefault || prev.length === 1,
              env: parsedEnv,
            });
          }),
        ),
      );
      setProfileEditOpen(false);
      toast.success('方案已更新，请保存模型配置后生效');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : '保存方案失败');
    }
  }

  if (loading) {
    return (
      <div className={cn(PANEL_CLASS, 'flex min-h-[360px] items-center justify-center')}>
        <div className="flex items-center gap-3 text-sm font-medium text-slate-600">
          <Loader2 className="h-4 w-4 animate-spin text-primary" />
          正在加载模型配置...
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <section className={cn(HERO_CLASS, 'p-6 md:p-7')}>
        <div className="pointer-events-none absolute inset-0 cyber-grid-subtle opacity-35" />
        <div className="relative max-w-3xl">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full border border-cyan-200 bg-white px-3 py-1 text-xs font-medium uppercase tracking-[0.24em] text-primary">
            <ServerCog className="h-3.5 w-3.5" />
            Model Console
          </div>
          <h1 className="text-4xl font-black tracking-normal text-slate-950">模型配置</h1>
          <p className="mt-4 max-w-2xl text-sm leading-7 text-slate-600">
            可以在这里配置统一的全局模型，也可以给不同的 Agent 独立配置模型。
          </p>
        </div>
      </section>

      <section className={cn(PANEL_CLASS, 'overflow-hidden bg-white/96')}>
        <div className="border-b border-slate-200/75 bg-[linear-gradient(135deg,rgba(255,255,255,.98),rgba(248,251,250,.96)_58%,rgba(242,248,246,.9))] p-5 md:p-6">
          <div className="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
            <div>
              <div className="text-xs font-medium uppercase tracking-[0.24em] text-primary">Model Scope</div>
              <h2 className="mt-2 text-2xl font-black text-slate-950">配置对象</h2>
              <p className="mt-2 text-sm leading-6 text-slate-600">通过下拉框切换全局配置或各 Agent 的独立配置。</p>
            </div>
            <div className="grid gap-3 lg:grid-cols-[220px_220px_auto] lg:items-center">
              <Select value={activeConfigScope} onValueChange={(value) => setActiveConfigScope(value as ConfigScope)}>
                <SelectTrigger className="h-11 rounded-full border-slate-200 bg-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="global">全局配置</SelectItem>
                  {AGENT_ORDER.map((agent) => (
                    <SelectItem key={agent} value={agent}>
                      {AGENT_META[agent].label} Agent
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Select value={selectedProfileId || 'none'} onValueChange={handleProfileSelect}>
                <SelectTrigger className="h-11 rounded-full border-slate-200 bg-white">
                  <SelectValue placeholder="选择方案" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">未选择方案</SelectItem>
                  {modelProfiles.map((profile) => (
                    <SelectItem key={profile.id} value={profile.id}>
                      {profile.isDefault ? '默认 · ' : ''}
                      {profile.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                variant="outline"
                className="h-11 rounded-full border-slate-200 bg-white px-5"
                onClick={runActiveConnectionTest}
                disabled={isGlobalScope && testingGlobal}
              >
                {isGlobalScope && testingGlobal ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Wand2 className="mr-2 h-4 w-4" />}
                连接测试
              </Button>
            </div>
          </div>

          <div className="mt-5 grid gap-3 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-center">
            <div className="rounded-2xl border border-slate-200 bg-white/88 px-4 py-3 text-sm text-slate-600 shadow-[inset_0_1px_0_rgba(255,255,255,.8)]">
              {isGlobalScope ? (
                <>
                  <span className="font-semibold text-slate-900">全局配置：</span>
                  未启用独立模型的 Agent 会继承这里的模型配置。
                </>
              ) : (
                activeAgent && (
                  <div className="flex flex-wrap items-center gap-3">
                    <span>
                      <span className="font-semibold text-slate-900">{AGENT_META[activeAgent].label}：</span>
                      {AGENT_META[activeAgent].description}
                      {!activeAgentConfig?.enabled && <span className="ml-2 text-slate-500">当前继承全局模型配置。</span>}
                    </span>
                    <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 shadow-sm">
                      <Switch
                        checked={Boolean(activeAgentConfig?.enabled)}
                        onCheckedChange={(checked) => updateAgentConfig(activeAgent, { enabled: checked })}
                      />
                      <span className="text-xs font-semibold text-slate-700">启用独立模型</span>
                    </span>
                  </div>
                )
              )}
            </div>
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="outline" className="rounded-full border-slate-200 bg-white" onClick={handleOpenSaveProfile}>
                <Plus className="mr-2 h-4 w-4" />
                保存方案
              </Button>
              <Button variant="outline" className="rounded-full border-slate-200 bg-white" onClick={() => setProfileManagerOpen(true)}>
                <Settings2 className="mr-2 h-4 w-4" />
                方案管理
              </Button>
              <Button variant="outline" className="rounded-full border-slate-200 bg-white" onClick={handleRestoreDefaultProfile}>
                恢复默认
              </Button>
              <Button className="rounded-full px-6 shadow-[0_16px_34px_rgba(94,142,114,0.26)]" onClick={saveAll} disabled={saving}>
                {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
                保存模型配置
              </Button>
            </div>
          </div>
        </div>

        <div className="grid gap-4 p-5 md:p-6 lg:grid-cols-2">
          <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
            <Label className="text-sm font-bold text-slate-800">Provider</Label>
            <Select value={activeProviderValue} onValueChange={updateActiveProvider}>
              <SelectTrigger className={cn(INPUT_CLASS, 'mt-2')}>
                <SelectValue placeholder="选择 Provider" />
              </SelectTrigger>
              <SelectContent>
                {providers.map((provider) => (
                  <SelectItem key={provider.value} value={provider.value}>
                    {provider.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
            <Label className="text-sm font-bold text-slate-800">Model</Label>
            <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_180px]">
              <ModelSearchSelect
                value={activeModelValue}
                models={activeProvider?.models || []}
                onChange={(value) => updateActiveConfig({ llmModel: value })}
              />
              <Input
                className={INPUT_CLASS}
                value={activeModelValue}
                onChange={(event) => updateActiveConfig({ llmModel: event.target.value })}
                placeholder="手动输入模型"
              />
            </div>
          </div>
          <div className={cn(SOFT_PANEL_CLASS, 'p-4 lg:col-span-2')}>
            <Label className="text-sm font-bold text-slate-800">API Key</Label>
            <Input
              type="password"
              className={cn(INPUT_CLASS, 'mt-2')}
              value={(isGlobalScope ? globalConfig.llmApiKey : activeAgentConfig?.llmApiKey) || ''}
              onChange={(event) => updateActiveConfig({ llmApiKey: event.target.value })}
              placeholder="sk-..."
            />
          </div>
          <div className={cn(SOFT_PANEL_CLASS, 'p-4 lg:col-span-2')}>
            <Label className="text-sm font-bold text-slate-800">Base URL</Label>
            <Input
              className={cn(INPUT_CLASS, 'mt-2')}
              value={(isGlobalScope ? globalConfig.llmBaseUrl : activeAgentConfig?.llmBaseUrl) || ''}
              onChange={(event) => updateActiveConfig({ llmBaseUrl: event.target.value })}
              placeholder="https://api.example.com"
            />
          </div>
          <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
            <Label className="text-sm font-bold text-slate-800">Temperature</Label>
            <Input
              type="number"
              min={0}
              max={2}
              step="0.1"
              className={cn(INPUT_CLASS, 'mt-2')}
              value={(isGlobalScope ? globalConfig.llmTemperature : activeAgentConfig?.llmTemperature) ?? ''}
              onChange={(event) =>
                updateActiveConfig({
                  llmTemperature: event.target.value === '' ? null : Number(event.target.value),
                })
              }
              placeholder="自动（留空）"
            />
          </div>
          <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
            <Label className="text-sm font-bold text-slate-800">Top P</Label>
            <Input
              type="number"
              min={0}
              max={1}
              step="0.01"
              className={cn(INPUT_CLASS, 'mt-2')}
              value={(isGlobalScope ? globalConfig.llmTopP : activeAgentConfig?.llmTopP) ?? ''}
              onChange={(event) =>
                updateActiveConfig({
                  llmTopP: event.target.value === '' ? null : Number(event.target.value),
                })
              }
              placeholder="自动（留空）"
            />
          </div>
          <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
            <Label className="text-sm font-bold text-slate-800">Endpoint Protocol</Label>
            <Select
              value={normalizeEndpointProtocol(isGlobalScope ? globalConfig.endpointProtocol : activeAgentConfig?.endpointProtocol)}
              onValueChange={(value) => updateActiveConfig({ endpointProtocol: normalizeEndpointProtocol(value) })}
            >
              <SelectTrigger className={cn(INPUT_CLASS, 'mt-2')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {activeProtocolOptions.map((protocol) => (
                  <SelectItem key={protocol} value={protocol}>
                    {ENDPOINT_PROTOCOL_LABELS[protocol] || protocol}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
            <Label className="text-sm font-bold text-slate-800">Tool Message Format</Label>
            <Select
              value={normalizeToolMessageFormat(isGlobalScope ? globalConfig.toolMessageFormat : activeAgentConfig?.toolMessageFormat)}
              onValueChange={(value) => updateActiveConfig({ toolMessageFormat: normalizeToolMessageFormat(value) })}
            >
              <SelectTrigger className={cn(INPUT_CLASS, 'mt-2')}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(TOOL_MESSAGE_FORMAT_LABELS).map(([value, label]) => (
                  <SelectItem key={value} value={value}>
                    {label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {!isGlobalScope && (
            <div className={cn(SOFT_PANEL_CLASS, 'p-4 lg:col-span-2')}>
              <Label className="text-sm font-bold text-slate-800">Max Iterations</Label>
              <Input
                type="number"
                className={cn(INPUT_CLASS, 'mt-2')}
                value={activeAgentConfig?.maxIterations ?? ''}
                onChange={(event) =>
                  updateActiveConfig({
                    maxIterations: event.target.value === '' ? null : Number(event.target.value),
                  })
                }
                placeholder="留空继承默认值"
              />
            </div>
          )}
          <div className={cn(SOFT_PANEL_CLASS, 'p-4 lg:col-span-2')}>
            <Label className="text-sm font-bold text-slate-800">Env JSON</Label>
            <Textarea className={cn(TEXTAREA_CLASS, 'mt-2')} value={activeEnvText} onChange={(event) => updateActiveEnvText(event.target.value)} />
          </div>

          <div className="rounded-[24px] border border-sky-100 bg-[linear-gradient(135deg,rgba(240,249,255,.96),rgba(255,255,255,.94))] p-4 shadow-[0_18px_44px_rgba(14,116,144,.06)] lg:col-span-2">
            <div className="flex gap-3">
              <div className="mt-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-2xl bg-sky-100 text-sky-700">
                <Info className="h-4 w-4" />
              </div>
              <div className="space-y-2 text-sm leading-7 text-slate-600">
                <div className="font-bold text-slate-900">Tips：中转站协议选择</div>
                <p>
                  如果使用中转站，Provider 应按中转站对外暴露的接口协议选择，而不是只按模型名称选择。例如中转站虽然背后接的是 GPT
                  模型，但如果它对外提供的是 Anthropic/Claude 兼容接口，则 Provider 需要选择 CLAUDE，Model 填写实际模型名，如
                  gpt-5.x；如果中转站对外提供的是 OpenAI Chat Completions 兼容接口，则 Provider 选择 OPENAI。
                </p>
                <p>
                  两类协议的工具调用格式不同：Claude/Anthropic 使用 tool_use / tool_result 消息块，OpenAI 使用 tool_calls / tool
                  消息结构。请确保 Provider 与中转站协议一致，否则普通文本对话可能可用，但 Agent 工具调用、审计循环或结果提交可能失败。
                </p>
              </div>
            </div>
          </div>
        </div>
      </section>

      <Dialog open={profileDialogOpen} onOpenChange={setProfileDialogOpen}>
        <DialogContent className="overflow-hidden rounded-[28px] border-slate-200 bg-white p-0 sm:max-w-lg">
          <DialogHeader className="border-b border-slate-200 bg-[linear-gradient(135deg,rgba(248,252,250,.98),rgba(255,255,255,.98))] px-6 py-5">
            <DialogTitle className="text-2xl font-black text-slate-950">保存模型方案</DialogTitle>
            <DialogDescription className="text-sm text-slate-500">
              保存当前全局模型配置，后续可从方案下拉框直接应用。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 px-6 py-5">
            <div className="rounded-3xl border border-slate-200 bg-slate-50/60 p-4">
              <Label className="text-sm font-bold text-slate-800">方案名称</Label>
              <Input
                className={cn(INPUT_CLASS, 'mt-2 bg-white')}
                value={profileName}
                onChange={(event) => setProfileName(event.target.value)}
              />
            </div>
            <label className="flex cursor-pointer items-center gap-3 rounded-3xl border border-slate-200 bg-white p-4 shadow-[0_12px_30px_rgba(15,23,42,.04)]">
              <Checkbox
                checked={profileIsDefault || modelProfiles.length === 0}
                disabled={modelProfiles.length === 0}
                onCheckedChange={(checked) => setProfileIsDefault(Boolean(checked))}
              />
              <span className="min-w-0">
                <span className="block text-sm font-bold text-slate-900">设为默认方案</span>
                <span className="mt-1 block text-xs leading-5 text-slate-500">
                  {modelProfiles.length === 0 ? '第一个保存的方案会自动设为默认方案。' : '恢复默认时会应用这个方案。'}
                </span>
              </span>
            </label>
          </div>
          <DialogFooter className="border-t border-slate-200 bg-slate-50/80 px-6 py-4">
            <Button variant="outline" className="rounded-full bg-white" onClick={() => setProfileDialogOpen(false)}>
              取消
            </Button>
            <Button className="rounded-full px-6 shadow-[0_14px_30px_rgba(94,142,114,.24)]" onClick={handleSaveProfile}>
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={profileManagerOpen} onOpenChange={setProfileManagerOpen}>
        <DialogContent className="max-h-[84vh] overflow-hidden rounded-[30px] border-slate-200 bg-white p-0 sm:max-w-3xl">
          <DialogHeader className="border-b border-slate-200 bg-[linear-gradient(135deg,rgba(249,252,251,.98),rgba(255,255,255,.98))] px-6 py-5">
            <DialogTitle className="text-2xl font-black text-slate-950">方案管理</DialogTitle>
            <DialogDescription className="text-sm text-slate-500">
              查看、编辑或删除方案。修改后需要点击保存模型配置。
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[58vh] space-y-2.5 overflow-y-auto bg-[linear-gradient(180deg,rgba(248,250,252,.72),rgba(255,255,255,.96))] p-4">
            {modelProfiles.length === 0 ? (
              <div className="rounded-3xl border border-dashed border-slate-200 bg-white p-10 text-center text-sm text-slate-500">
                暂无模型方案
              </div>
            ) : (
              modelProfiles.map((profile) => (
                <div
                  key={profile.id}
                  className="grid gap-3 rounded-3xl border border-slate-200 bg-white/95 px-4 py-3 shadow-[0_12px_30px_rgba(15,23,42,.045)] transition-colors hover:border-emerald-200 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
                >
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <h3 className="truncate text-base font-black text-slate-950">{profile.name}</h3>
                      {profile.isDefault && (
                        <span className="shrink-0 rounded-full bg-emerald-50 px-2.5 py-1 text-[11px] font-bold text-primary">默认</span>
                      )}
                    </div>
                    <div className="mt-2 flex flex-wrap gap-1.5 text-xs text-slate-500">
                      <span className="rounded-full bg-slate-100 px-2.5 py-1">{profile.llmProvider || 'provider 未配置'}</span>
                      <span className="rounded-full bg-slate-100 px-2.5 py-1">{profile.llmModel || 'model 未配置'}</span>
                      <span className="rounded-full bg-slate-100 px-2.5 py-1">{profile.endpointProtocol || 'protocol 未配置'}</span>
                    </div>
                  </div>
                  <div className="flex shrink-0 gap-2 md:justify-end">
                    <Button
                      variant="outline"
                      size="icon"
                      className="h-10 w-10 rounded-full bg-white text-slate-700"
                      aria-label="查看详情"
                      title="查看详情"
                      onClick={() => openProfileDetail(profile)}
                    >
                      <Eye className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="outline"
                      size="icon"
                      className="h-10 w-10 rounded-full bg-white text-slate-700"
                      aria-label="编辑"
                      title="编辑"
                      onClick={() => openProfileEdit(profile)}
                    >
                      <PencilLine className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="outline"
                      size="icon"
                      className="h-10 w-10 rounded-full border-rose-200 bg-white text-rose-600 hover:bg-rose-50"
                      aria-label="删除"
                      title="删除"
                      onClick={() => deleteProfile(profile.id)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
              ))
            )}
          </div>
          <DialogFooter className="border-t border-slate-200 bg-white px-6 py-4">
            <Button className="rounded-full px-6" onClick={() => setProfileManagerOpen(false)}>
              完成
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={profileDetailOpen} onOpenChange={setProfileDetailOpen}>
        <DialogContent className="overflow-hidden rounded-[28px] border-slate-200 bg-white p-0 sm:max-w-2xl">
          <DialogHeader className="border-b border-slate-200 bg-[linear-gradient(135deg,rgba(248,252,250,.98),rgba(255,255,255,.98))] px-6 py-5">
            <DialogTitle className="text-2xl font-black text-slate-950">方案详情</DialogTitle>
            <DialogDescription>{activeManagedProfile?.name || '模型方案'}</DialogDescription>
          </DialogHeader>
          {activeManagedProfile && (
            <div className="grid gap-3 p-5 sm:grid-cols-2">
              {[
                ['Provider', activeManagedProfile.llmProvider || '-'],
                ['Model', activeManagedProfile.llmModel || '-'],
                ['Base URL', activeManagedProfile.llmBaseUrl || '-'],
                ['Endpoint Protocol', activeManagedProfile.endpointProtocol || '-'],
                ['Tool Message Format', activeManagedProfile.toolMessageFormat || '-'],
                ['Timeout', activeManagedProfile.llmTimeout ?? '-'],
                ['Temperature', activeManagedProfile.llmTemperature ?? '-'],
                ['Top P', activeManagedProfile.llmTopP ?? '-'],
                ['Max Tokens', activeManagedProfile.llmMaxTokens ?? '-'],
              ].map(([label, value]) => (
                <div key={label} className="rounded-2xl border border-slate-200 bg-slate-50/70 p-4">
                  <div className="text-xs font-medium uppercase tracking-[0.16em] text-slate-500">{label}</div>
                  <div className="mt-2 break-all text-sm font-semibold text-slate-900">{String(value)}</div>
                </div>
              ))}
            </div>
          )}
          <DialogFooter className="border-t border-slate-200 bg-white px-6 py-4">
            <Button className="rounded-full px-6" onClick={() => setProfileDetailOpen(false)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={profileEditOpen} onOpenChange={setProfileEditOpen}>
        <DialogContent className="max-h-[88vh] overflow-hidden rounded-[30px] border-slate-200 bg-white p-0 sm:max-w-5xl">
          <DialogHeader className="border-b border-slate-200 bg-[linear-gradient(135deg,rgba(248,252,250,.98),rgba(255,255,255,.98))] px-6 py-5">
            <DialogTitle className="text-2xl font-black text-slate-950">编辑方案</DialogTitle>
            <DialogDescription>编辑方案名称、模型参数和默认状态。</DialogDescription>
          </DialogHeader>
          {editProfileDraft && (
            <div className="max-h-[64vh] overflow-y-auto bg-[linear-gradient(180deg,rgba(248,250,252,.72),rgba(255,255,255,.96))] p-5">
              <div className="grid gap-4 lg:grid-cols-2">
                <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                  <Label className="text-sm font-bold text-slate-800">方案名称</Label>
                  <Input
                    className={cn(INPUT_CLASS, 'mt-2 bg-white')}
                    value={editProfileName}
                    onChange={(event) => setEditProfileName(event.target.value)}
                  />
                </div>
                <label className={cn(SOFT_PANEL_CLASS, 'flex cursor-pointer items-center justify-between gap-4 p-4')}>
                  <span>
                    <span className="block text-sm font-bold text-slate-900">默认方案</span>
                    <span className="mt-1 block text-xs text-slate-500">恢复默认时会应用该方案。</span>
                  </span>
                  <Switch checked={editProfileIsDefault} onCheckedChange={(checked) => setEditProfileIsDefault(Boolean(checked))} />
                </label>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                  <Label className="text-sm font-bold text-slate-800">Provider</Label>
                  <Select
                    value={editProfileDraft.llmProvider || ''}
                    onValueChange={(value) =>
                      updateEditProfileDraft({
                        llmProvider: value,
                        llmModel: providerMap[value]?.default_model || editProfileDraft.llmModel,
                        endpointProtocol: normalizeEndpointProtocol(providerMap[value]?.default_endpoint_protocol),
                        toolMessageFormat: 'auto',
                      })
                    }
                  >
                    <SelectTrigger className={cn(INPUT_CLASS, 'mt-2')}>
                      <SelectValue placeholder="选择 Provider" />
                    </SelectTrigger>
                    <SelectContent>
                      {providers.map((provider) => (
                        <SelectItem key={provider.value} value={provider.value}>
                          {provider.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                  <Label className="text-sm font-bold text-slate-800">Model</Label>
                  <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_160px]">
                    <ModelSearchSelect
                      value={editProfileDraft.llmModel || ''}
                      models={providerMap[editProfileDraft.llmProvider || '']?.models || []}
                      onChange={(value) => updateEditProfileDraft({ llmModel: value })}
                    />
                    <Input
                      className={INPUT_CLASS}
                      value={editProfileDraft.llmModel || ''}
                      onChange={(event) => updateEditProfileDraft({ llmModel: event.target.value })}
                      placeholder="手动输入模型"
                    />
                  </div>
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4 lg:col-span-2')}>
                  <Label className="text-sm font-bold text-slate-800">API Key</Label>
                  <Input
                    type="password"
                    className={cn(INPUT_CLASS, 'mt-2')}
                    value={editProfileDraft.llmApiKey || ''}
                    onChange={(event) => updateEditProfileDraft({ llmApiKey: event.target.value })}
                    placeholder="sk-..."
                  />
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4 lg:col-span-2')}>
                  <Label className="text-sm font-bold text-slate-800">Base URL</Label>
                  <Input
                    className={cn(INPUT_CLASS, 'mt-2')}
                    value={editProfileDraft.llmBaseUrl || ''}
                    onChange={(event) => updateEditProfileDraft({ llmBaseUrl: event.target.value })}
                    placeholder="https://api.example.com"
                  />
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                  <Label className="text-sm font-bold text-slate-800">Temperature</Label>
                  <Input
                    type="number"
                    min={0}
                    max={2}
                    step="0.1"
                    className={cn(INPUT_CLASS, 'mt-2')}
                    value={editProfileDraft.llmTemperature ?? ''}
                    onChange={(event) =>
                      updateEditProfileDraft({
                        llmTemperature: event.target.value === '' ? null : Number(event.target.value),
                      })
                    }
                    placeholder="自动（留空）"
                  />
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                  <Label className="text-sm font-bold text-slate-800">Top P</Label>
                  <Input
                    type="number"
                    min={0}
                    max={1}
                    step="0.01"
                    className={cn(INPUT_CLASS, 'mt-2')}
                    value={editProfileDraft.llmTopP ?? ''}
                    onChange={(event) =>
                      updateEditProfileDraft({
                        llmTopP: event.target.value === '' ? null : Number(event.target.value),
                      })
                    }
                    placeholder="自动（留空）"
                  />
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                  <Label className="text-sm font-bold text-slate-800">Endpoint Protocol</Label>
                  <Select
                    value={normalizeEndpointProtocol(editProfileDraft.endpointProtocol)}
                    onValueChange={(value) => updateEditProfileDraft({ endpointProtocol: normalizeEndpointProtocol(value) })}
                  >
                    <SelectTrigger className={cn(INPUT_CLASS, 'mt-2')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {protocolOptionsForProvider(providerMap[editProfileDraft.llmProvider || '']).map((protocol) => (
                        <SelectItem key={protocol} value={protocol}>
                          {ENDPOINT_PROTOCOL_LABELS[protocol] || protocol}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                  <Label className="text-sm font-bold text-slate-800">Tool Message Format</Label>
                  <Select
                    value={normalizeToolMessageFormat(editProfileDraft.toolMessageFormat)}
                    onValueChange={(value) => updateEditProfileDraft({ toolMessageFormat: normalizeToolMessageFormat(value) })}
                  >
                    <SelectTrigger className={cn(INPUT_CLASS, 'mt-2')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {Object.entries(TOOL_MESSAGE_FORMAT_LABELS).map(([value, label]) => (
                        <SelectItem key={value} value={value}>
                          {label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div className={cn(SOFT_PANEL_CLASS, 'p-4 lg:col-span-2')}>
                  <Label className="text-sm font-bold text-slate-800">Env JSON</Label>
                  <Textarea
                    className={cn(TEXTAREA_CLASS, 'mt-2 min-h-[170px]')}
                    value={editProfileEnvText}
                    onChange={(event) => setEditProfileEnvText(event.target.value)}
                  />
                </div>
              </div>
            </div>
          )}
          <DialogFooter className="border-t border-slate-200 bg-slate-50/80 px-6 py-4">
            <Button variant="outline" className="rounded-full bg-white" onClick={() => setProfileEditOpen(false)}>
              取消
            </Button>
            <Button className="rounded-full px-6" onClick={handleSaveProfileEdit}>
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={testDialogOpen} onOpenChange={setTestDialogOpen}>
        <DialogContent className="max-h-[86vh] overflow-y-auto rounded-3xl border-slate-200 sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <MessageSquareMore className="h-5 w-5 text-primary" />
              {AGENT_META[testAgent].label} Agent 连接测试
            </DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 md:grid-cols-[1fr_1.1fr]">
            <div className="space-y-3">
              <div className={cn(SOFT_PANEL_CLASS, 'p-4')}>
                <div className="text-xs font-medium uppercase tracking-[0.2em] text-primary">Prompt</div>
                <Textarea
                  className={cn(TEXTAREA_CLASS, 'mt-3 min-h-[220px]')}
                  value={testPrompt}
                  onChange={(event) => setTestPrompt(event.target.value)}
                />
              </div>
              <Button className="w-full rounded-full" onClick={runAgentTest} disabled={testingAgent || !testPrompt.trim()}>
                {testingAgent ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <CheckCircle2 className="mr-2 h-4 w-4" />}
                运行测试
              </Button>
            </div>
            <div className={cn(SOFT_PANEL_CLASS, 'min-h-[300px] p-4')}>
              <div className="text-xs font-medium uppercase tracking-[0.2em] text-primary">Response</div>
              {testResult ? (
                <div className="mt-3 space-y-4 text-sm leading-6 text-slate-700">
                  <div className="rounded-2xl border border-slate-200 bg-white p-3">
                    <div className="font-semibold text-slate-900">
                      {testResult.provider || '-'} / {testResult.model || '-'}
                    </div>
                    <div className="mt-1 text-xs text-slate-500">会话轮次：{testResult.conversation_count ?? 0}</div>
                  </div>
                  <div className="whitespace-pre-wrap rounded-2xl border border-slate-200 bg-white p-3">{testResult.response || testResult.message}</div>
                  {testResult.loaded_skills && testResult.loaded_skills.length > 0 && (
                    <div className="flex flex-wrap gap-2">
                      {testResult.loaded_skills.map((skill) => (
                        <span key={skill.slug} className="rounded-full bg-emerald-50 px-3 py-1 text-xs font-semibold text-primary">
                          {skill.name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="mt-20 text-center text-sm text-slate-500">运行测试后会在这里显示结果</div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" className="rounded-full" onClick={() => setTestDialogOpen(false)}>
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
