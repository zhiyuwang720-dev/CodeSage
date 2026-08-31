import { Database, GitBranchPlus, SlidersHorizontal } from 'lucide-react';

import { DatabaseManager } from '@/components/database/DatabaseManager';
import { SystemConfig } from '@/components/system/SystemConfig';
import { WorkflowManager } from '@/components/system/WorkflowManager';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

export default function AdminDashboard() {
  return (
    <div className="cyber-bg-elevated relative min-h-screen overflow-x-hidden p-6">
      <div className="absolute inset-0 cyber-grid-subtle pointer-events-none" />
      <div className="relative z-10 mx-auto max-w-7xl space-y-6">
        <Tabs defaultValue="models" className="space-y-6">
          <TabsList className="grid h-auto w-full grid-cols-3 gap-2 rounded-[24px] border border-[rgba(190,209,200,.82)] bg-white/90 p-2 shadow-[0_18px_44px_rgba(61,85,75,.07)] backdrop-blur">
            <TabsTrigger
              value="models"
              className="rounded-[18px] py-3 text-sm font-semibold text-slate-600 transition data-[state=active]:bg-primary data-[state=active]:text-white data-[state=active]:shadow-[0_12px_28px_rgba(94,142,114,.28)]"
            >
              <SlidersHorizontal className="mr-2 h-4 w-4" /> 模型配置
            </TabsTrigger>
            <TabsTrigger
              value="workflow"
              className="rounded-[18px] py-3 text-sm font-semibold text-slate-600 transition data-[state=active]:bg-primary data-[state=active]:text-white data-[state=active]:shadow-[0_12px_28px_rgba(94,142,114,.28)]"
            >
              <GitBranchPlus className="mr-2 h-4 w-4" /> 工作流管理
            </TabsTrigger>
            <TabsTrigger
              value="database"
              className="rounded-[18px] py-3 text-sm font-semibold text-slate-600 transition data-[state=active]:bg-primary data-[state=active]:text-white data-[state=active]:shadow-[0_12px_28px_rgba(94,142,114,.28)]"
            >
              <Database className="mr-2 h-4 w-4" /> 数据库管理
            </TabsTrigger>
          </TabsList>
          <TabsContent value="models">
            <SystemConfig />
          </TabsContent>
          <TabsContent value="workflow">
            <WorkflowManager />
          </TabsContent>
          <TabsContent value="database">
            <DatabaseManager />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
