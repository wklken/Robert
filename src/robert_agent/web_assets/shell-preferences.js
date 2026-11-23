"use strict";

const RobertShell = (() => {
  const root = document.documentElement;
  const darkMode = matchMedia("(prefers-color-scheme: dark)");
  const commonLabels = {
    en: {
      theme: "Theme", themeSystem: "System", themeLight: "Light", themeDark: "Dark",
      language: "Language", languageEnglish: "English", languageChinese: "Chinese",
      primaryNavigation: "Primary navigation", navWork: "Work", navBoard: "Board",
      navHistory: "History", navTimeline: "Timeline", navProposals: "Proposals",
      navOperations: "Operations", navKnowledge: "Knowledge",
    },
    zh: {
      theme: "主题", themeSystem: "跟随系统", themeLight: "浅色", themeDark: "深色",
      language: "语言", languageEnglish: "English", languageChinese: "中文",
      primaryNavigation: "一级导航", navWork: "工作", navBoard: "任务看板",
      navHistory: "历史", navTimeline: "时间线", navProposals: "提案",
      navOperations: "运行状态", navKnowledge: "知识",
    },
  };

  function applyTheme(choice) {
    root.dataset.themeChoice = choice;
    root.dataset.theme = root.dataset.colorMode = choice === "system"
      ? (darkMode.matches ? "dark" : "light")
      : choice;
  }

  function applyLanguage(language) {
    root.lang = language === "zh" ? "zh-CN" : "en";
  }

  const savedLanguage = localStorage.getItem("robertLanguage");
  const savedTheme = localStorage.getItem("robertWorkbenchTheme");
  const initialLanguage = ["en", "zh"].includes(savedLanguage) ? savedLanguage
    : (/^zh/i.test(navigator.language || "") ? "zh" : "en");
  applyLanguage(initialLanguage);
  applyTheme(["system", "light", "dark"].includes(savedTheme) ? savedTheme : "system");

  function bindPreferences(onLanguage) {
    const languageSelect = document.getElementById("language-select");
    const themeSelect = document.getElementById("theme-select");
    languageSelect.value = initialLanguage;
    themeSelect.value = root.dataset.themeChoice;
    languageSelect.addEventListener("change", () => {
      const language = languageSelect.value;
      localStorage.setItem("robertLanguage", language);
      applyLanguage(language);
      onLanguage(language);
    });
    themeSelect.addEventListener("change", () => {
      localStorage.setItem("robertWorkbenchTheme", themeSelect.value);
      applyTheme(themeSelect.value);
    });
    darkMode.addEventListener("change", () => {
      if (themeSelect.value === "system") applyTheme("system");
    });
    onLanguage(initialLanguage);
  }

  function translate(language, pageLabels) {
    const text = { ...commonLabels[language], ...pageLabels };
    document.querySelectorAll("[data-i18n]").forEach((node) => {
      node.textContent = text[node.dataset.i18n];
    });
    document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
      node.setAttribute("aria-label", text[node.dataset.i18nAria]);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
      node.placeholder = text[node.dataset.i18nPlaceholder];
    });
  }

  return { bindPreferences, translate };
})();
