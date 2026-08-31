import { Link } from "react-router-dom";
import PageMeta from "@/components/layout/PageMeta";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Compass } from "lucide-react";

export default function NotFound() {
  return (
    <>
      <PageMeta title="页面未找到" description="CodeSage 页面未找到" />
      <div className="min-h-screen gradient-bg p-4 md:p-6">
        <div className="mx-auto flex min-h-[calc(100vh-2rem)] max-w-5xl items-center justify-center rounded-[40px] border border-white/70 bg-white/55 p-6 shadow-[0_28px_80px_rgba(88,97,110,0.12)] backdrop-blur-xl sm:p-10">
          <div className="w-full max-w-3xl rounded-[34px] border border-slate-200/70 bg-white/80 p-8 text-center shadow-[0_20px_55px_rgba(88,97,110,0.10)] sm:p-12">
            <div className="mx-auto mb-6 flex h-20 w-20 items-center justify-center rounded-[26px] bg-[rgba(223,235,225,0.78)]">
              <Compass className="h-9 w-9 text-[hsl(var(--primary))]" />
            </div>

            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">404 Error</p>
            <h1 className="mt-3 text-5xl font-semibold tracking-[-0.05em] text-slate-900 sm:text-6xl">页面未找到</h1>
            <p className="mx-auto mt-4 max-w-xl text-base text-slate-500">
              你访问的页面可能已被移动、删除，或者链接地址有误。返回 CodeSage 首页继续操作会更快。
            </p>

            <div className="mt-8 grid gap-4 sm:grid-cols-2">
              <div className="rounded-[24px] bg-slate-100/85 p-5 text-left">
                <p className="text-sm font-semibold text-slate-900">你可以尝试</p>
                <ul className="mt-3 space-y-2 text-sm text-slate-500">
                  <li>检查 URL 是否输入正确</li>
                  <li>返回首页重新进入模块</li>
                  <li>从左侧导航重新打开对应页面</li>
                </ul>
              </div>
              <div className="rounded-[24px] bg-[rgba(212,222,229,0.72)] p-5 text-left">
                <p className="text-sm font-semibold text-slate-900">当前品牌</p>
                <p className="mt-3 text-sm text-slate-600">
                  CodeSage 已启用新的浅灰工作台界面，你可以直接返回首页继续使用核心审计功能。
                </p>
              </div>
            </div>

            <Link to="/" className="mt-8 inline-flex">
              <Button className="cyber-btn-primary h-12 rounded-[18px] px-8 text-base">
                <ArrowLeft className="mr-2 h-5 w-5" />
                返回首页
              </Button>
            </Link>

            <p className="mt-8 text-sm text-slate-400">&copy; {new Date().getFullYear()} CodeSage</p>
          </div>
        </div>
      </div>
    </>
  );
}
