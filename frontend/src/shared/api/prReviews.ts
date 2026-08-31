/**
 * PR Reviews API — CodeSage 审查管线(阶段 01/02 后端契约)
 * POST /api/v1/pr-reviews 创建审查任务(rules 默认离线 / runtime 三视角编排)
 * GET  /api/v1/pr-reviews/{review_id} 轮询结果
 */

import { apiClient } from "./serverClient";
import type { PrReviewCreatePayload, PrReviewJob, PrReviewResult } from "@/shared/types/review";

export async function createPrReview(payload: PrReviewCreatePayload): Promise<PrReviewJob> {
  const { data } = await apiClient.post<PrReviewJob>("/pr-reviews", payload);
  return data;
}

export async function getPrReview(reviewId: string): Promise<PrReviewResult> {
  const { data } = await apiClient.get<PrReviewResult>(`/pr-reviews/${reviewId}`);
  return data;
}

/** 按视角筛选评论(body [Label] 前缀或 source 字段) */
export function filterByPerspective<T extends { body?: string; source?: string }>(
  comments: T[],
  perspective: string | null,
): T[] {
  if (!perspective) return comments;
  const label = perspective.charAt(0).toUpperCase() + perspective.slice(1);
  return comments.filter(
    (c) => c.source === perspective || (c.body ?? "").startsWith(`[${label}]`),
  );
}
