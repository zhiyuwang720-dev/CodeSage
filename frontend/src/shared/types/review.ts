// CodeSage PR 审查领域类型(与 backend ReviewComment / ReviewFinding 契约对齐)
// backend: app/services/pr_review/models.py + review_runtime/final_review_contract.py

export type ReviewSeverity = "critical" | "high" | "medium" | "low";
export type ReviewCategory =
  | "bug"
  | "security"
  | "concurrency"
  | "data"
  | "api"
  | "perf"
  | "test_gap"
  | "doc_defect";
export type ReviewPerspective = "security" | "architecture" | "quality" | "rules" | "orchestrator";

/** benchmark 注入契约: 一条行内评论 */
export interface ReviewComment {
  path: string;
  line: number;
  body: string;
  severity?: ReviewSeverity;
  category?: ReviewCategory;
}

/** 运行时富契约(分视角归因) */
export interface ReviewFinding extends ReviewComment {
  line_end?: number;
  code_snippet?: string | null;
  suggestion?: string | null;
  confidence?: number;
  needs_verification?: boolean;
  verdict?: "confirmed" | "refuted" | "uncertain";
  rule_id?: string;
  /** 视角标记(spec 03 分视角归因) */
  source?: ReviewPerspective;
}

export interface PrReviewCreatePayload {
  pr_url?: string;
  diff_text?: string;
  engine?: "rules" | "runtime";
  options?: Record<string, unknown>;
}

export interface PrReviewJob {
  review_id: string;
  pr_key: string;
  status: "running" | "completed" | "failed";
}

export interface PrReviewResult {
  status: string;
  comments: ReviewComment[];
  meta?: Record<string, unknown>;
}
