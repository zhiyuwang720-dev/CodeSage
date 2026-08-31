import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { autoAttributeTranslations, autoTemplateTranslations, autoTextTranslations } from "./resources";

const SKIP_SELECTOR = [
  "script",
  "style",
  "textarea",
  "select",
  "option",
  "pre",
  "code",
  "[contenteditable='true']",
  "[data-i18n-ignore='true']",
].join(",");

const TRANSLATED_ATTRIBUTES = ["placeholder", "title", "aria-label"] as const;
const CHINESE_TEXT_PATTERN = /[\u3400-\u9fff]/u;

type TranslatedAttribute = (typeof TRANSLATED_ATTRIBUTES)[number];

const originalTextByNode = new WeakMap<Text, string>();
const originalAttributesByElement = new WeakMap<Element, Partial<Record<TranslatedAttribute, string>>>();

function isEnglishLanguage(language: string) {
  return language.toLowerCase().startsWith("en");
}

function shouldSkipNode(node: Node) {
  const element = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
  return Boolean(element?.closest(SKIP_SELECTOR));
}

function hasTranslatableContent(node: Node) {
  if (node.nodeType === Node.TEXT_NODE) {
    return originalTextByNode.has(node as Text) || CHINESE_TEXT_PATTERN.test(node.nodeValue ?? "");
  }

  if (node.nodeType !== Node.ELEMENT_NODE) {
    return false;
  }

  const element = node as Element;
  if (element.closest(SKIP_SELECTOR)) {
    return false;
  }

  const originals = originalAttributesByElement.get(element);
  if (originals && Object.keys(originals).length > 0) {
    return true;
  }

  for (const attribute of TRANSLATED_ATTRIBUTES) {
    const value = element.getAttribute(attribute);
    if (value && CHINESE_TEXT_PATTERN.test(value)) {
      return true;
    }
  }

  return CHINESE_TEXT_PATTERN.test(element.textContent ?? "");
}

function syncTextNode(node: Text, language: string) {
  if (shouldSkipNode(node)) return;

  const originalText = originalTextByNode.get(node) ?? node.nodeValue ?? "";
  if (!originalTextByNode.has(node)) {
    originalTextByNode.set(node, originalText);
  }

  const trimmed = originalText.trim();
  const translated = autoTextTranslations[trimmed] ?? translateTemplateText(trimmed);
  if (!translated) return;

  const leading = originalText.match(/^\s*/)?.[0] ?? "";
  const trailing = originalText.match(/\s*$/)?.[0] ?? "";
  const nextText = isEnglishLanguage(language) ? `${leading}${translated}${trailing}` : originalText;

  if (node.nodeValue !== nextText) {
    node.nodeValue = nextText;
  }
}

function translateTemplateText(text: string) {
  for (const template of autoTemplateTranslations) {
    const match = text.match(template.pattern);
    if (match) {
      return template.translate(...match);
    }
  }

  return undefined;
}

function syncElementAttributes(element: Element, language: string) {
  if (element.closest(SKIP_SELECTOR)) return;

  for (const attribute of TRANSLATED_ATTRIBUTES) {
    const currentValue = element.getAttribute(attribute);
    if (!currentValue) continue;

    const originals = originalAttributesByElement.get(element) ?? {};
    const originalValue = originals[attribute] ?? currentValue;
    originals[attribute] = originalValue;
    originalAttributesByElement.set(element, originals);

    const translated = autoAttributeTranslations[originalValue];
    if (!translated) continue;

    const nextValue = isEnglishLanguage(language) ? translated : originalValue;
    if (currentValue !== nextValue) {
      element.setAttribute(attribute, nextValue);
    }
  }
}

function walkAndSync(root: Node, language: string) {
  if (root.nodeType === Node.TEXT_NODE) {
    syncTextNode(root as Text, language);
    return;
  }

  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) {
    return;
  }

  if (root.nodeType === Node.ELEMENT_NODE) {
    syncElementAttributes(root as Element, language);
  }

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  let current = walker.nextNode();
  while (current) {
    if (current.nodeType === Node.TEXT_NODE) {
      syncTextNode(current as Text, language);
    } else if (current.nodeType === Node.ELEMENT_NODE) {
      syncElementAttributes(current as Element, language);
    }
    current = walker.nextNode();
  }
}

export function useAutoTranslateDom() {
  const { i18n } = useTranslation();

  useEffect(() => {
    const root = document.body;
    if (!root) return;

    const sync = () => walkAndSync(root, i18n.language);
    const frame = window.requestAnimationFrame(sync);
    const pendingNodes = new Set<Node>();
    let pendingFrame: number | null = null;

    const flushPendingNodes = () => {
      pendingFrame = null;
      const nodes = Array.from(pendingNodes);
      pendingNodes.clear();

      for (const node of nodes) {
        if (node === root || node.isConnected) {
          walkAndSync(node, i18n.language);
        }
      }
    };

    const queueNode = (node: Node) => {
      if (!hasTranslatableContent(node)) return;

      pendingNodes.add(node);
      if (pendingFrame === null) {
        pendingFrame = window.requestAnimationFrame(flushPendingNodes);
      }
    };

    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type === "characterData") {
          queueNode(mutation.target);
        }

        if (mutation.type === "attributes" && mutation.target.nodeType === Node.ELEMENT_NODE) {
          queueNode(mutation.target);
        }

        for (const node of mutation.addedNodes) {
          queueNode(node);
        }
      }
    });

    observer.observe(root, {
      attributes: true,
      attributeFilter: [...TRANSLATED_ATTRIBUTES],
      characterData: true,
      childList: true,
      subtree: true,
    });
    i18n.on("languageChanged", sync);

    return () => {
      window.cancelAnimationFrame(frame);
      if (pendingFrame !== null) {
        window.cancelAnimationFrame(pendingFrame);
      }
      i18n.off("languageChanged", sync);
      observer.disconnect();
    };
  }, [i18n]);
}
