import { version as packageVersion } from "../../../package.json";

const normalizeVersion = (value: unknown): string | undefined => {
  if (typeof value !== "string") {
    return undefined;
  }

  const trimmed = value.trim();
  if (!trimmed) {
    return undefined;
  }

  return trimmed.startsWith("v") || trimmed.startsWith("V") ? trimmed.slice(1) : trimmed;
};

const formatVersionLabel = (value: string): string => {
  if (/^\d+\.\d+\.\d+(?:[-+].*)?$/.test(value)) {
    return `v${value}`;
  }

  return value;
};

export const appVersion = normalizeVersion(import.meta.env.VITE_APP_VERSION) ?? packageVersion;
export const appVersionLabel = formatVersionLabel(appVersion);
