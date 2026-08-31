import {
	AlertTriangle,
	Bot,
	CheckCircle2,
	Shield,
	Sparkles,
	Zap,
} from "lucide-react";

import { Switch } from "@/components/ui/switch";
import { cn } from "@/shared/utils/utils";

import { AUDIT_MODE_OPTIONS, type AuditMode } from "./auditModeConfig";

interface AgentModeSelectorProps {
	value: AuditMode;
	onChange: (mode: AuditMode) => void;
	verificationEnabled: boolean;
	onVerificationChange: (enabled: boolean) => void;
	disabled?: boolean;
}

const MODE_VISUALS: Record<
	AuditMode,
	{
		card: string;
		icon: string;
		check: string;
		recommended: string;
	}
> = {
	enhanced_scan: {
		card: "border-amber-200/80 bg-[linear-gradient(135deg,rgba(255,251,235,0.95),rgba(255,255,255,0.92))] hover:border-amber-300/80 hover:bg-amber-50/80",
		icon: "border-amber-200 bg-amber-100/80 text-amber-700",
		check: "border-amber-500 bg-amber-500 text-white",
		recommended: "bg-amber-100 text-amber-700",
	},
	intelligent_audit: {
		card: "border-emerald-200/90 bg-[linear-gradient(135deg,rgba(236,253,245,0.96),rgba(255,255,255,0.92))] hover:border-emerald-300/80 hover:bg-emerald-50/80",
		icon: "border-emerald-200 bg-emerald-100/80 text-emerald-700",
		check: "border-primary bg-primary text-primary-foreground",
		recommended: "bg-primary/10 text-primary",
	},
	comprehensive_audit: {
		card: "border-teal-200/90 bg-[linear-gradient(135deg,rgba(240,253,250,0.96),rgba(255,255,255,0.92))] hover:border-teal-300/80 hover:bg-teal-50/80",
		icon: "border-teal-200 bg-teal-100/80 text-teal-700",
		check: "border-teal-600 bg-teal-600 text-white",
		recommended: "bg-teal-100 text-teal-700",
	},
};

export type { AuditMode } from "./auditModeConfig";

export default function AgentModeSelector({
	value,
	onChange,
	verificationEnabled,
	onVerificationChange,
	disabled = false,
}: AgentModeSelectorProps) {
	return (
		<section className="rounded-2xl border border-border bg-background p-4 shadow-[0_18px_50px_-42px_rgba(15,23,42,0.45)]">
			<div className="flex items-center gap-3">
				<div className="flex h-9 w-9 items-center justify-center rounded-xl border border-primary/20 bg-primary/10 shadow-sm">
					<Shield className="h-4 w-4 text-primary" />
				</div>
				<div>
					<p className="text-sm font-semibold text-foreground">审计模式</p>
					<p className="text-xs text-muted-foreground">
						选择本次审计的模式即可
					</p>
				</div>
			</div>

			<div className="mt-4 grid grid-cols-1 gap-3 lg:grid-cols-3">
				{AUDIT_MODE_OPTIONS.map((option) => {
					const isSelected = value === option.mode;
					const visual = MODE_VISUALS[option.mode];
					const Icon =
						option.mode === "enhanced_scan"
							? Zap
							: option.mode === "intelligent_audit"
								? Bot
								: Sparkles;

					return (
						<label
							key={option.mode}
							className={cn(
								"relative flex min-h-[138px] cursor-pointer flex-col justify-between overflow-hidden rounded-xl border p-4 transition-all duration-200",
								"hover:-translate-y-0.5 hover:shadow-[0_18px_40px_-30px_rgba(15,23,42,0.4)]",
								visual.card,
								isSelected &&
									"shadow-[0_22px_44px_-30px_rgba(20,83,45,0.55)] ring-1 ring-primary/20",
								disabled && "pointer-events-none opacity-55",
							)}
						>
							<div
								className={cn(
									"absolute inset-x-0 top-0 h-1 opacity-0 transition-opacity",
									isSelected && "opacity-100",
									option.mode === "enhanced_scan" && "bg-amber-400",
									option.mode === "intelligent_audit" && "bg-primary",
									option.mode === "comprehensive_audit" && "bg-teal-500",
								)}
							/>
							<input
								type="radio"
								name="auditMode"
								value={option.mode}
								checked={isSelected}
								onChange={() => onChange(option.mode)}
								disabled={disabled}
								className="sr-only"
							/>

							<div className="flex items-start justify-between gap-3">
								<div className="flex items-center gap-3">
									<div
										className={cn(
											"flex h-10 w-10 items-center justify-center rounded-xl border shadow-sm",
											visual.icon,
										)}
									>
										<Icon className="h-4 w-4" />
									</div>
									<div>
										<div className="flex items-center gap-2">
											<p className="text-sm font-semibold text-foreground">
												{option.label}
											</p>
											{option.recommended && (
												<span
													className={cn(
														"rounded-full px-2 py-0.5 text-[11px] font-medium",
														visual.recommended,
													)}
												>
													推荐
												</span>
											)}
										</div>
										<p className="mt-1 text-sm text-muted-foreground">
											{option.description}
										</p>
									</div>
								</div>

								<div
									className={cn(
										"flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
										isSelected
											? visual.check
											: "border-border bg-white text-transparent",
									)}
								>
									<CheckCircle2 className="h-3.5 w-3.5" />
								</div>
							</div>
						</label>
					);
				})}
			</div>

			<div className="mt-4 flex items-start justify-between gap-4 rounded-xl border border-amber-200/80 bg-[linear-gradient(135deg,rgba(255,251,235,0.9),rgba(255,255,255,0.9))] px-4 py-3 shadow-sm">
				<div className="flex min-w-0 gap-3">
					<div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-amber-200 bg-amber-100/80 text-amber-700">
						<AlertTriangle className="h-4 w-4" />
					</div>
					<div>
						<p className="text-sm font-medium text-foreground">动态漏洞验证</p>
						<p className="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
							开启后会执行动态验证，时间成本和token消耗都会增加，且动态验证涉及网络、环境部署、权限等多方面因素，当前版本并未做过多测试和优化，效果可能不够稳定，建议仅在需要时开启。
						</p>
					</div>
				</div>
				<div className="flex shrink-0 items-center gap-3 pt-1">
					{!verificationEnabled && (
						<span className="text-xs font-medium text-amber-700">建议关闭</span>
					)}
					<Switch
						checked={verificationEnabled}
						onCheckedChange={onVerificationChange}
						disabled={disabled}
					/>
				</div>
			</div>
		</section>
	);
}
