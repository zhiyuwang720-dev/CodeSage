import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import { resources } from "./resources";

export type SupportedLanguage = "zh" | "en";

export const LANGUAGE_STORAGE_KEY = "codesage.language";

function isSupportedLanguage(value: string | null): value is SupportedLanguage {
  return value === "zh" || value === "en";
}

function getInitialLanguage(): SupportedLanguage {
  if (typeof window === "undefined") {
    return "zh";
  }

  const storedLanguage = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
  return isSupportedLanguage(storedLanguage) ? storedLanguage : "zh";
}

void i18n.use(initReactI18next).init({
  resources,
  lng: getInitialLanguage(),
  fallbackLng: "zh",
  supportedLngs: ["zh", "en"],
  interpolation: {
    escapeValue: false,
  },
});

export function getCurrentLanguage(): SupportedLanguage {
  return i18n.language === "en" ? "en" : "zh";
}

export async function setAppLanguage(language: SupportedLanguage) {
  if (typeof window !== "undefined") {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, language);
  }

  await i18n.changeLanguage(language);
}

export default i18n;
