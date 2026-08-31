export const SERVICE_STARTING_LOGIN_MESSAGE = "系统正在启动，数据库连接可能尚未就绪，请稍后再试";

function detailToMessage(detail: unknown): string | null {
  if (Array.isArray(detail)) {
    const message = detail
      .map((err) => {
        if (err && typeof err === "object") {
          const item = err as { msg?: unknown; message?: unknown };
          return item.msg || item.message || JSON.stringify(err);
        }
        return String(err);
      })
      .filter(Boolean)
      .join("; ");
    return message || null;
  }

  if (detail && typeof detail === "object") {
    const objectDetail = detail as { msg?: unknown; message?: unknown };
    return String(objectDetail.msg || objectDetail.message || JSON.stringify(detail));
  }

  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }

  return null;
}

export function getLoginErrorMessage(error: unknown): string {
  const candidate = error as {
    code?: string;
    message?: string;
    response?: {
      status?: number;
      data?: {
        detail?: unknown;
      };
    };
  };

  const status = candidate?.response?.status;
  const hasResponse = Boolean(candidate?.response);

  if (!hasResponse || candidate?.code === "ERR_NETWORK") {
    return SERVICE_STARTING_LOGIN_MESSAGE;
  }

  if (status && [502, 503, 504].includes(status)) {
    return SERVICE_STARTING_LOGIN_MESSAGE;
  }

  if (status && status >= 500) {
    return SERVICE_STARTING_LOGIN_MESSAGE;
  }

  return detailToMessage(candidate.response?.data?.detail) || "登录失败，请检查邮箱和密码";
}
