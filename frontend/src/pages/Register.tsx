import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { apiClient } from "@/shared/api/serverClient";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "sonner";
import { ArrowRight, CheckCircle2, Lock, Mail, ShieldCheck, User, Wand2 } from "lucide-react";
import { appVersionLabel } from "@/shared/config/version";

const onboardingPoints = [
  {
    title: "\u5feb\u901f\u521b\u5efa\u5de5\u4f5c\u533a",
    description: "\u6ce8\u518c\u540e\u5373\u53ef\u8fdb\u5165\u7edf\u4e00\u7684\u5ba1\u8ba1\u63a7\u5236\u53f0\uff0c\u5f00\u59cb\u7ec4\u7ec7\u9879\u76ee\u548c\u4efb\u52a1\u3002",
    icon: Wand2,
  },
  {
    title: "\u4fdd\u6301\u534f\u4f5c\u4e00\u81f4",
    description: "\u56e2\u961f\u6210\u5458\u3001\u5ba1\u8ba1\u8fc7\u7a0b\u4e0e\u7ed3\u679c\u8f93\u51fa\u90fd\u5728\u540c\u4e00\u4e2a\u4ea7\u54c1\u754c\u9762\u5185\u5b8c\u6210\u3002",
    icon: ShieldCheck,
  },
];

export default function Register() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await apiClient.post("/auth/register", {
        email,
        password,
        full_name: fullName,
      });
      toast.success("\u6ce8\u518c\u6210\u529f\uff0c\u8bf7\u767b\u5f55");
      navigate("/login");
    } catch (error: any) {
      const detail = error.response?.data?.detail;
      if (Array.isArray(detail)) {
        const messages = detail.map((err: any) => err.msg || err.message || JSON.stringify(err)).join("; ");
        toast.error(messages || "\u6ce8\u518c\u5931\u8d25");
      } else if (typeof detail === "object") {
        toast.error(detail.msg || detail.message || JSON.stringify(detail));
      } else {
        toast.error(detail || "\u6ce8\u518c\u5931\u8d25");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen gradient-bg p-4 md:p-6">
      <div className="mx-auto grid min-h-[calc(100vh-2rem)] max-w-6xl overflow-hidden rounded-[40px] border border-white/70 bg-white/55 lg:grid-cols-[1.04fr_0.96fr] shadow-[0_28px_80px_rgba(88,97,110,0.12)] backdrop-blur-xl">
        <section className="relative hidden border-r border-slate-200/70 bg-[linear-gradient(180deg,rgba(249,250,249,0.92),rgba(242,245,244,0.92))] p-10 lg:flex lg:flex-col xl:p-12">
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(255,255,255,0.68),transparent_32%),radial-gradient(circle_at_bottom_right,rgba(197,214,205,0.22),transparent_28%)]" />
          <div className="relative flex h-full flex-col justify-between">
            <div className="space-y-10">
              <div className="flex items-center gap-4">
                <div className="flex h-16 w-16 items-center justify-center rounded-[22px] border border-slate-200/80 bg-white/85 shadow-[0_14px_36px_rgba(92,104,120,0.08)]">
                  <img src="/codesage_icon.svg" alt="CodeSage" className="h-11 w-11 object-contain" />
                </div>
                <div>
                  <div className="text-3xl font-semibold tracking-[-0.05em] text-slate-900">CodeSage</div>
                  <p className="mt-1 text-sm text-slate-500">Create Your Workspace</p>
                </div>
              </div>

              <div className="max-w-xl space-y-5">
                <p className="text-xs font-semibold uppercase tracking-[0.22em] text-slate-400">Create Account</p>
                <h1 className="text-5xl leading-[1.06] tracking-[-0.06em] text-slate-900">
                  {"\u4e3a\u4f60\u7684\u56e2\u961f\u521b\u5efa"}
                  <br />
                  {"\u65b0\u7684\u5ba1\u8ba1\u5165\u53e3"}
                </h1>
                <p className="max-w-lg text-base text-slate-500">
                  {"\u5efa\u7acb\u4e00\u4e2a\u66f4\u8f7b\u76c8\u4f46\u8db3\u591f\u4e13\u4e1a\u7684\u5b89\u5168\u5ba1\u8ba1\u5de5\u4f5c\u533a\uff0c\u8ba9\u9879\u76ee\u3001\u4efb\u52a1\u548c\u7ed3\u679c\u5f52\u6863\u4ece\u4e00\u5f00\u59cb\u5c31\u4fdd\u6301\u4e00\u81f4\u3002"}
                </p>
              </div>

              <div className="grid gap-4 xl:grid-cols-2">
                {onboardingPoints.map(({ title, description, icon: Icon }) => (
                  <div key={title} className="auth-showcase-card p-6">
                    <div className="relative flex items-start gap-4">
                      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-white/90 text-[hsl(var(--primary))] shadow-[0_10px_22px_rgba(111,167,132,0.12)]">
                        <Icon className="h-5 w-5" />
                      </div>
                      <div>
                        <div className="text-base font-semibold text-slate-900">{title}</div>
                        <p className="mt-2 text-sm leading-7 text-slate-500">{description}</p>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="relative flex items-center justify-between text-sm text-slate-400">
              <span>{appVersionLabel}</span>
              <span>{new Date().toLocaleDateString("zh-CN")}</span>
            </div>
          </div>
        </section>

        <section className="flex items-center justify-center p-6 sm:p-10 xl:p-12">
          <div className="w-full max-w-[520px]">
            <div className="mb-8 text-center lg:hidden">
              <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-[24px] border border-slate-200/80 bg-white/90 shadow-[0_14px_36px_rgba(92,104,120,0.08)]">
                <img src="/codesage_icon.svg" alt="CodeSage" className="h-11 w-11 object-contain" />
              </div>
              <h1 className="text-3xl font-semibold tracking-[-0.05em] text-slate-900">{"\u521b\u5efa CodeSage \u8d26\u53f7"}</h1>
              <p className="mt-2 text-sm text-slate-500">{"\u521b\u5efa\u65b0\u7684\u5ba1\u8ba1\u5de5\u4f5c\u533a"}</p>
            </div>

            <div className="cyber-card p-7 sm:p-8">
              <div className="mb-8 space-y-3">
                <p className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-400">Start Here</p>
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <h2 className="text-[2rem] font-semibold tracking-[-0.05em] text-slate-900">{"\u6ce8\u518c CodeSage"}</h2>
                    <p className="mt-2 text-sm text-slate-500">{"\u586b\u5199\u57fa\u7840\u4fe1\u606f\u540e\u5373\u53ef\u521b\u5efa\u65b0\u7684\u5ba1\u8ba1\u5de5\u4f5c\u533a\u3002"}</p>
                  </div>
                  <div className="hidden h-12 w-12 items-center justify-center rounded-2xl bg-[rgba(223,235,225,0.8)] text-[hsl(var(--primary))] sm:flex">
                    <CheckCircle2 className="h-5 w-5" />
                  </div>
                </div>
              </div>

              <form onSubmit={handleSubmit} className="space-y-5">
                <div className="space-y-2.5">
                  <Label htmlFor="fullName" className="cyber-label">{"\u59d3\u540d"}</Label>
                  <div className="auth-input-shell">
                    <span className="auth-input-icon">
                      <User className="h-4 w-4" />
                    </span>
                    <Input
                      id="fullName"
                      placeholder={"\u8bf7\u8f93\u5165\u59d3\u540d"}
                      value={fullName}
                      onChange={(e) => setFullName(e.target.value)}
                      required
                      className="auth-input-field"
                    />
                  </div>
                </div>

                <div className="space-y-2.5">
                  <Label htmlFor="email" className="cyber-label">{"\u90ae\u7bb1\u5730\u5740"}</Label>
                  <div className="auth-input-shell">
                    <span className="auth-input-icon">
                      <Mail className="h-4 w-4" />
                    </span>
                    <Input
                      id="email"
                      type="email"
                      placeholder="your@email.com"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      required
                      className="auth-input-field"
                    />
                  </div>
                </div>

                <div className="space-y-2.5">
                  <Label htmlFor="password" className="cyber-label">{"\u5bc6\u7801"}</Label>
                  <div className="auth-input-shell">
                    <span className="auth-input-icon">
                      <Lock className="h-4 w-4" />
                    </span>
                    <Input
                      id="password"
                      type="password"
                      placeholder={"\u521b\u5efa\u767b\u5f55\u5bc6\u7801"}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                      className="auth-input-field"
                    />
                  </div>
                </div>

                <Button type="submit" disabled={loading} className="cyber-btn-primary h-14 w-full rounded-[20px] text-base">
                  {loading ? "\u6ce8\u518c\u4e2d..." : "\u521b\u5efa\u8d26\u53f7"}
                </Button>
              </form>

              <div className="mt-7 flex items-center justify-between rounded-[22px] bg-slate-100/80 px-5 py-4 text-sm">
                <span className="text-slate-500">{"\u5df2\u7ecf\u6709\u8d26\u53f7\uff1f"}</span>
                <Link to="/login" className="inline-flex items-center gap-1.5 font-semibold text-[hsl(var(--primary))]">
                  {"\u8fd4\u56de\u767b\u5f55"}
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
