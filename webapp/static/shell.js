"use strict";

// The frame of the panel: the chat picker, the header, the four tabs and the pages that open inside them.

function updateBack() {
  if (!nativeBack()) return;
  if (state.screen || (state.chatId !== null && state.chats.length > 1)) tg.BackButton.show();
  else tg.BackButton.hide();
}

function goBack() {
  if (state.screen) {
    closeScreen();
    return;
  }
  const leave = () => {
    state.chatId = null;
    setDirty(false);
    renderPicker();
  };
  if (state.dirty) confirmAction("Несохранённые правки пропадут. Выйти?", leave);
  else leave();
}

// ------------------------------------------------------------------ chat picker

function chatStatus(c) {
  if (!c.enabled) return ["off", "модерация выключена"];
  if (c.observe) return ["trial", "пробный режим"];
  return ["", "защита включена"];
}

function renderPicker() {
  state.chatId = null;
  state.screen = null;
  setDirty(false);
  updateBack();
  const items = state.chats
    .map((c) => {
      const [dot, label] = chatStatus(c);
      return `<button class="chat-item" data-chat="${esc(c.chat_id)}">${avatar(c.title, c.chat_id)}
        <div class="meta"><b>${esc(c.title)}</b><small><span class="dot ${dot}"></span>${label}</small></div>
        <svg class="i chev" viewBox="0 0 24 24" aria-hidden="true">${ICONS.chevron}</svg></button>`;
    })
    .join("");
  const empty = emptyState(
    "shield",
    "Пока нет чатов",
    `<ol><li>Добавьте бота в группу обсуждений канала</li><li>Сделайте его админом: удалять сообщения и банить</li>
      <li>Напишите там <b>/rules</b> и правила словами</li></ol>`
  );
  $("#app").innerHTML = `<header class="hero"><div class="who-row">
      <div class="ava" style="--a1:#c3b9ea;--a2:#6f86c9">${icon("shield")}</div>
      <div class="meta"><h1>Ваши чаты</h1><div class="tag">Где вы админ и где работает DefenceAi</div></div></div></header>
    <section style="margin-top:14px">${items ? `<div class="group">${items}</div>` : empty}</section>`;
  $("#app").querySelectorAll("[data-chat]").forEach((b) => (b.onclick = () => openChat(Number(b.dataset.chat))));
}

// -------------------------------------------------------------------- main view

async function openChat(chatId) {
  state.chatId = chatId;
  state.screen = null;
  if (!state.data || state.data.chat_id !== chatId) $("#app").innerHTML = `<section style="padding-top:16px">${skeleton()}</section>`;
  try {
    state.data = await api("GET", "/chat/" + chatId);
  } catch (e) {
    fail(e);
    if (!state.data) renderPicker();
    return;
  }
  state.rules = state.data.rules.map((r) => ({ ...r }));
  state.log = null;
  state.tab = "home";
  clearMain();
  renderApp(true);
}

async function reloadChat() {
  const data = await api("GET", "/chat/" + state.chatId);
  state.data = data;
  return data;
}

function chatTitle() {
  const chat = state.chats.find((c) => c.chat_id === state.chatId);
  return chat ? chat.title : "Чат";
}

function statusInfo(s) {
  if (!s.enabled) return ["off", "Модерация выключена", "бот ничего не проверяет"];
  if (s.lockdown) return ["lock", "Удаляю всё от участников", "режим на время рейда"];
  if (s.observe) return ["trial", "Пробный режим", "бот ничего не удаляет, только записывает"];
  return ["on", "Защита включена", MODE_SHORT[s.mode] || ""];
}

const TABS = [
  ["home", "Главная"],
  ["rules", "Правила"],
  ["insights", "Аналитика"],
  ["settings", "Настройки"],
];

function renderApp(animate) {
  updateBack();
  $("#app").innerHTML = `
    <header class="hero" id="head"></header>
    <nav id="tabs">${TABS.map(([id, name]) => `<button data-t="${id}" class="${state.tab === id ? "on" : ""}">${name}</button>`).join("")}</nav>
    <section id="view"></section>`;
  renderHead();
  $("#tabs").querySelectorAll("button").forEach((b) => (b.onclick = () => switchTab(b.dataset.t)));
  renderTab(animate);
}

function renderHead() {
  const [tone, title, sub] = statusInfo(state.data.settings);
  $("#head").innerHTML = `
    <div class="who-row">${avatar(chatTitle(), state.chatId)}
      <div class="meta"><h1>${esc(chatTitle())}</h1>
        ${state.chats.length > 1 ? `<button class="switch-chat" id="switch">${icon("back")}Все чаты</button>` : '<div class="tag">Панель DefenceAi</div>'}</div></div>
    <div class="status ${tone}"><div class="txt"><b>${title}</b><small>${esc(sub)}</small></div></div>`;
  const sw = $("#switch");
  if (sw) sw.onclick = goBack;
  const pending = state.data.stats ? state.data.stats.pending : 0;
  const home = $('#tabs button[data-t="home"]');
  if (home) {
    const old = home.querySelector(".badge");
    if (old) old.remove();
    if (pending) home.insertAdjacentHTML("beforeend", `<span class="badge">${pending > 99 ? "99+" : pending}</span>`);
  }
}

function switchTab(tab) {
  if (tab === state.tab && !state.screen) return;
  state.tab = tab;
  state.screen = null;
  $("#tabs").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === tab));
  syncMain();
  haptic("light");
  renderTab(true);
}

function renderTab(animate) {
  const view = $("#view");
  view.onclick = null;
  view.onchange = null;
  view.style.animation = "none";
  if (animate) {
    void view.offsetWidth; // restart the fade-in only when the page really changes
    view.style.animation = "";
  }
  if (state.tab === "home") renderHome();
  else if (state.tab === "rules") renderRules();
  else if (state.tab === "settings") renderSettings();
  else renderInsights();
}

// ------------------------------------------------- pages opened inside a tab

function openScreen(name, params) {
  state.screen = { name, ...(params || {}) };
  if (name === "pay") state.pay = null;
  haptic("light");
  window.scrollTo(0, 0);
  updateBack();
  syncMain();
  renderScreen(true);
}

function closeScreen() {
  stopPayPolling();
  state.screen = null;
  haptic("light");
  updateBack();
  syncMain();
  renderApp(true);
}

// The page title and a way back; the content goes into #view.
function screenShell(title, subtitle) {
  $("#app").innerHTML = `
    <header class="sub-head"><button class="back" id="back">${icon("back")}Назад</button></header>
    <div class="sub-title"><h1>${title}</h1>${subtitle ? `<div class="tag">${subtitle}</div>` : ""}</div>
    <section id="view"></section>`;
  $("#back").onclick = goBack;
  const view = $("#view");
  view.onclick = null;
  view.onchange = null;
}

function renderScreen() {
  const s = state.screen;
  if (s.name === "log") renderLogScreen();
  else if (s.name === "folder") renderFolder(s.id);
  else if (s.name === "pay") renderPay();
}

// Re-draw whatever is on screen after a change.
function rerender() {
  if (state.screen) renderScreen();
  else {
    renderHead();
    renderTab();
  }
}
