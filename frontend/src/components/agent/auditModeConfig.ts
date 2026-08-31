import type { WorkflowConfig } from "@/shared/api/modelConfig";

export type AuditMode =
	| "enhanced_scan"
	| "intelligent_audit"
	| "comprehensive_audit";

export interface AuditModeOption {
	mode: AuditMode;
	label: string;
	recommended?: boolean;
	description: string;
	workflow: string;
}

export const AUDIT_MODE_OPTIONS: AuditModeOption[] = [
	{
		mode: "enhanced_scan",
		label: "增强扫描",
		description: "传统工具扫描+模型验证",
		workflow: "Orchestrator -> Recon -> Scan -> Triage",
	},
	{
		mode: "intelligent_audit",
		label: "智能审计",
		recommended: true,
		description: "Agent自主审计",
		workflow: "Orchestrator -> Recon -> Finding",
	},
	{
		mode: "comprehensive_audit",
		label: "综合审计",
		description: "增强扫描+智能审计",
		workflow: "Orchestrator -> Recon -> Scan → Triage + Finding",
	},
];

export function getAuditModeLabel(mode: AuditMode): string {
	return (
		AUDIT_MODE_OPTIONS.find((option) => option.mode === mode)?.label ??
		"智能审计"
	);
}

function buildWorkflowConfig(
	mode: AuditMode,
	enableVerification: boolean,
): WorkflowConfig {
	const scanEnabled = mode !== "intelligent_audit";
	const triageEnabled = mode !== "intelligent_audit";
	const findingEnabled = mode !== "enhanced_scan";

	return {
		agentStates: {
			orchestrator: { enabled: true, locked: true },
			recon: { enabled: true, locked: true },
			scan: { enabled: scanEnabled },
			triage: { enabled: triageEnabled },
			finding: { enabled: findingEnabled },
			verification: { enabled: enableVerification },
		},
	};
}

export function buildAgentTaskAuditScope(
	mode: AuditMode,
	enableVerification: boolean,
): Record<string, unknown> {
	return {
		mode,
		dynamic_verification_enabled: enableVerification,
		workflow: buildWorkflowConfig(mode, enableVerification),
	};
}
