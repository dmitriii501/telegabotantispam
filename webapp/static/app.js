"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const $ = (s) => document.querySelector(s);

// Comments come from strangers: everything that reaches innerHTML goes through esc().
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ACTIONS = {
  ban: "Бан",
  mute: "Мут на сутки",
  warn: "Предупредить",
  delete: "Просто удалить",
  review: "Прислать мне",
  auto: "Авто",
};
// What the chosen action does, in plain words: shown under the selector of every rule.
const ACTION_HINT = {
  auto: "Спам-бота забанит, человеку объяснит причину, если не уверен — спросит вас.",
  ban: "Удалит сообщение и забанит автора.",
  mute: "Удалит сообщение и запретит автору писать 24 часа.",
  warn: "Удалит сообщение и объяснит автору, что нарушено.",
  delete: "Удалит сообщение молча.",
  review: "Ничего не удалит: пришлёт вам на решение.",
};
const KINDS = {
  prohibition: ["❌", "Запрет"],
  permission: ["✅", "Разрешено"],
  context: ["ℹ️", "Описание канала"],
  everything: ["🚫", "Удалять всё"],
};
const MODE_HINT = {
  soft: "Почти всё сомнительное бот присылает вам.",
  normal: "Сомнительные комментарии бот присылает вам.",
  strict: "Бот удаляет даже при небольшой уверенности.",
};
const KIND_LABEL = {
  question: "Вопросы",
  complaint: "Жалобы",
  praise: "Похвала",
  suggestion: "Предложения",
  bug: "Ошибки в постах",
  chat: "Общение",
};
// Log badges: severity is shown by an icon and a word, the color only repeats it.
const ACTION_PILL = {
  delete_and_ban: ["бан", "bad", "ban"],
  delete_and_mute: ["мут", "serious", "mute"],
  delete_and_explain: ["предупреждение", "warn", "alert"],
  delete_silent: ["удалено", "", "trash"],
  send_to_review: ["ждёт вас", "warn", "clock"],
};
const MODE_SHORT = { soft: "мягкая проверка", normal: "обычная строгость", strict: "строгая проверка" };

// Stroke icons, 24x24. Inline SVG keeps the page self-contained under the CSP.
const ICONS = {
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  shieldCheck: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  pause: '<circle cx="12" cy="12" r="9"/><path d="M10 15V9M14 15V9"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  rules: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
  chart: '<path d="M3 20h18"/><path d="M6 16v-5M11 16V6M16 16v-8M21 16v-3"/>',
  sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
  history: '<path d="M3 12a9 9 0 1 0 2.6-6.4L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l3 2"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  check: '<path d="m5 12 5 5L20 7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  infoGlyph: '<path d="M12 11v6M12 7h.01"/>',
  ban: '<circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  sparkles: '<path d="m12 3 1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"/><path d="m19 15 .8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8z"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  back: '<path d="m15 6-6 6 6 6"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
  star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1 6.2-5.5-2.9-5.5 2.9 1-6.2L3 9.6l6.2-.9z"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
  power: '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  repeat: '<path d="m17 2 4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15"/><path d="m7 22-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/>',
  timer: '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2 2M10 2h4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  robot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9.5 17h5"/>',
  users: '<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0"/><path d="M16 4a4 4 0 0 1 0 8M22 21a7 7 0 0 0-4-6.3"/>',
  eraser: '<path d="m7 21-4-4L14 6l7 7-8 8z"/><path d="M7 21h14M10 10l7 7"/>',
  flame: '<path d="M12 22c4 0 7-3 7-7 0-5-5-7-5-12-3 2-6 5-6 9-1-1-2-2-2-3-1 2-1 4-1 6 0 4 3 7 7 7z"/>',
  pie: '<path d="M21 12A9 9 0 1 1 12 3v9z"/><path d="M15 3.5A9 9 0 0 1 20.5 9H15z"/>',
  news: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 9h10M7 13h10M7 17h6"/>',
  link: '<path d="M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>',
  cart: '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.7 12.4a2 2 0 0 0 2 1.6h8.6a2 2 0 0 0 2-1.6L22 7H6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  mute: '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="m22 9-6 6M16 9l6 6"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
const KIND_ICON = { prohibition: "x", permission: "check", context: "infoGlyph", everything: "ban" };

// Telegram's own avatar gradients: a person or chat keeps the same color everywhere.
const AVATAR = [
  ["#ff885e", "#ff516a"], ["#ffcd6a", "#ffa85c"], ["#82b1ff", "#665fff"], ["#a0de7e", "#54cb68"],
  ["#53edd6", "#28c9b7"], ["#72d5fd", "#2a9ef1"], ["#e0a2f3", "#d669ed"],
];

function avatar(name, seed, size) {
  const words = String(name || "").replace(/[^\p{L}\p{N}\s]/gu, " ").trim().split(/\s+/).filter(Boolean);
  const letters = words.slice(0, 2).map((w) => Array.from(w)[0]).join("").toUpperCase() || "?";
  let h = 0;
  for (const c of String(seed == null ? name : seed)) h = (h * 31 + c.codePointAt(0)) >>> 0;
  const [a1, a2] = AVATAR[h % AVATAR.length];
  return `<div class="ava ${size || ""}" style="--a1:${a1};--a2:${a2}" aria-hidden="true">${esc(letters)}</div>`;
}

function emptyState(ico, title, text) {
  return `<div class="empty"><div class="bubble">${icon(ico)}</div>${title ? `<b>${title}</b>` : ""}<div>${text}</div></div>`;
}

const skeleton = () => '<div class="sk tall"></div><div class="sk"></div><div class="sk"></div>';

const state = { chats: [], chatId: null, data: null, rules: [], dirty: false, tab: "rules", log: null };

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("on");
  setTimeout(() => el.classList.remove("on"), 2200);
}

async function api(method, path, body) {
  let res;
  try {
    res = await fetch("/api" + path, {
      method,
      headers: { "X-Init-Data": tg ? tg.initData : "", "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (_) {
    throw new Error("Нет связи с сервером. Проверьте интернет и попробуйте ещё раз.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Ошибка " + res.status);
  return data;
}

// ------------------------------------------------- native Telegram controls (with fallbacks)

const nativeMain = () => !!(tg && tg.MainButton && tg.MainButton.setText);
const nativeBack = () => !!(tg && tg.BackButton && tg.BackButton.show);

function haptic(kind) {
  try {
    if (!tg || !tg.HapticFeedback) return;
    if (["success", "error", "warning"].includes(kind)) tg.HapticFeedback.notificationOccurred(kind);
    else tg.HapticFeedback.impactOccurred(kind || "light");
  } catch (_) {
    /* older clients: no vibration */
  }
}

function confirmAction(text, onYes, onNo) {
  const done = (ok) => (ok ? onYes() : onNo && onNo());
  if (tg && tg.showConfirm && tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) tg.showConfirm(text, done);
  else done(window.confirm(text));
}

function fail(e) {
  toast(e.message);
  haptic("error");
}

function setBusy(busy) {
  $("#save").disabled = busy;
  if (!nativeMain()) return;
  if (busy) tg.MainButton.showProgress(false);
  else tg.MainButton.hideProgress();
}

function setDirty(value) {
  state.dirty = value;
  const show = value && state.tab === "rules";
  if (nativeMain()) {
    // The big button at the bottom of Telegram itself replaces our own bar.
    if (show) {
      tg.MainButton.setText("Сохранить правила");
      tg.MainButton.show();
    } else tg.MainButton.hide();
    $("#savebar").hidden = true;
  } else $("#savebar").hidden = !show;
  document.body.classList.toggle("saving", !$("#savebar").hidden); // the toast moves above the bar
  try {
    // Telegram asks "close without saving?" while edits are pending.
    if (value) tg.enableClosingConfirmation();
    else tg.disableClosingConfirmation();
  } catch (_) {
    /* not supported */
  }
}

function updateBack() {
  if (!nativeBack()) return;
  if (state.chatId !== null && state.chats.length > 1) tg.BackButton.show();
  else tg.BackButton.hide();
}

function goBack() {
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
      <div class="ava" style="--a1:#72d5fd;--a2:#2a9ef1">${icon("shield")}</div>
      <div class="meta"><h1>Ваши чаты</h1><div class="tag">Где вы админ и где работает DefenceAi</div></div></div></header>
    <section style="margin-top:14px">${items ? `<div class="group">${items}</div>` : empty}</section>`;
  $("#app").querySelectorAll("[data-chat]").forEach((b) => (b.onclick = () => openChat(Number(b.dataset.chat))));
}

// -------------------------------------------------------------------- main view

async function openChat(chatId) {
  state.chatId = chatId;
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
  state.tab = "rules";
  renderApp(true);
}

function chatTitle() {
  const chat = state.chats.find((c) => c.chat_id === state.chatId);
  return chat ? chat.title : "Чат";
}

function statusInfo(s) {
  if (!s.enabled) return ["off", "pause", "Модерация выключена", "Бот ничего не проверяет"];
  if (s.lockdown) return ["lock", "alert", "Удаляю всё от участников", "Режим на время рейда"];
  if (s.observe) return ["trial", "eye", "Пробный режим", "Бот ничего не удаляет, только записывает"];
  return ["on", "shieldCheck", "Защита включена", MODE_SHORT[s.mode] || ""];
}

const TABS = [
  ["rules", "Правила", "rules"],
  ["insights", "Аналитика", "chart"],
  ["settings", "Настройки", "sliders"],
  ["log", "Журнал", "history"],
];

function renderApp(animate) {
  updateBack();
  $("#app").innerHTML = `
    <header class="hero" id="head"></header>
    <nav id="tabs">${TABS.map(
      ([id, name, ico]) => `<button data-t="${id}" class="${state.tab === id ? "on" : ""}">${icon(ico)}<span>${name}</span></button>`
    ).join("")}</nav>
    <section id="view"></section>`;
  renderHead();
  $("#tabs").querySelectorAll("button").forEach((b) => (b.onclick = () => switchTab(b.dataset.t)));
  renderTab(animate);
}

function renderHead() {
  const s = state.data.settings;
  const st = state.data.stats;
  const [tone, ico, title, sub] = statusInfo(s);
  const kpis = st
    ? `<div class="kpis"><div class="cap">За сутки</div>
        <div class="kpi"><b>${st.checked}</b><span>проверено</span></div>
        <div class="kpi"><b>${st.deleted}</b><span>${s.observe ? "удалил бы" : "удалено"}</span></div>
        <button class="kpi ${st.pending ? "alert" : ""}" id="kpiPending"><b>${st.pending}</b><span>ждут вас</span></button>
      </div>`
    : "";
  $("#head").innerHTML = `
    <div class="who-row">${avatar(chatTitle(), state.chatId)}
      <div class="meta"><h1>${esc(chatTitle())}</h1>
        ${state.chats.length > 1 ? `<button class="switch-chat" id="switch">${icon("back")}Все чаты</button>` : '<div class="tag">Панель DefenceAi</div>'}</div></div>
    <div class="status ${tone}">${icon(ico)}<div class="txt"><b>${title}</b><small>${esc(sub)}</small></div></div>
    ${kpis}`;
  const sw = $("#switch");
  if (sw) sw.onclick = goBack;
  const pending = $("#kpiPending");
  if (pending) pending.onclick = () => switchTab("log");
  const logTab = $('#tabs button[data-t="log"]');
  if (logTab) {
    const old = logTab.querySelector(".badge");
    if (old) old.remove();
    if (st && st.pending) logTab.insertAdjacentHTML("beforeend", `<span class="badge">${st.pending > 99 ? "99+" : st.pending}</span>`);
  }
}

function switchTab(tab) {
  if (tab === state.tab) return;
  state.tab = tab;
  $("#tabs").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === tab));
  setDirty(state.dirty);
  haptic("light");
  renderTab(true);
}

function renderTab(animate) {
  const view = $("#view");
  view.onclick = null;
  view.onchange = null;
  view.style.animation = "none";
  if (animate) {
    void view.offsetWidth; // restart the fade-in only when the tab really changes
    view.style.animation = "";
  }
  if (state.tab === "rules") renderRules();
  else if (state.tab === "settings") renderSettings();
  else if (state.tab === "insights") renderInsights();
  else renderLog();
}

// ------------------------------------------------------------------------ rules

const PARSE_LABEL = `${icon("sparkles")}Разобрать на правила`;

function renderRules() {
  const cards = state.rules
    .map((r, i) => {
      const kinds = Object.entries(KINDS)
        .map(([k, v]) => `<option value="${k}" ${k === r.kind ? "selected" : ""}>${v[1]}</option>`)
        .join("");
      const acts =
        r.kind === "prohibition" || r.kind === "everything"
          ? `<select class="act" data-act="${i}" aria-label="Что делать">${Object.entries(ACTIONS)
              .map(([k, v]) => `<option value="${k}" ${k === (r.action || "auto") ? "selected" : ""}>${v}</option>`)
              .join("")}</select>`
          : "";
      return `<div class="card rule">
        <div class="kind ${r.kind}">${icon(KIND_ICON[r.kind])}</div>
        <textarea class="txt" rows="1" maxlength="300" data-i="${i}" placeholder="Например: реклама других каналов">${esc(r.text)}</textarea>
        <button class="del" data-del="${i}" title="Удалить правило" aria-label="Удалить правило">${icon("x")}</button>
        <div class="row"><select data-kind="${i}" aria-label="Тип правила">${kinds}</select>${acts}</div>
        ${acts ? `<div class="hint" id="hint-${i}">${icon("info")}<span>${esc(hintFor(r))}</span></div>` : ""}</div>`;
    })
    .join("");
  $("#view").innerHTML = `
    <div class="h">Правила чата <span class="count">${state.rules.length ? state.rules.length : ""}</span></div>
    <div id="ruleList">${cards || emptyState("rules", "", "Правил пока нет. Добавьте первое или напишите их словами ниже.")}</div>
    <div class="card base"><div class="kind">${icon("shield")}</div>
      <div><b>Спам и мошенничество</b><span>Реклама, казино, наркотики и обман запрещены всегда</span></div>
      <span class="lockpill">всегда</span></div>
    <button class="btn dashed wide" id="add">${icon("plus")}Добавить правило</button>
    <div class="h">Написать словами</div>
    <div class="card ai-box">
      <div class="ai-title">${icon("sparkles")}ИИ разберёт текст на правила</div>
      <div class="tag">Каждое правило с действием: бан, мут, удалить, предупредить</div>
      <textarea class="paste" id="paste" maxlength="2000" placeholder="Например: рекламу нельзя — бан, политику — удалять, мат можно"></textarea>
      <button class="btn soft wide" style="margin-top:10px" id="parse">${PARSE_LABEL}</button>
    </div>`;

  const list = $("#ruleList");
  list.querySelectorAll("textarea.txt").forEach(autoGrow);
  list.oninput = (e) => {
    if (e.target.dataset.i === undefined) return;
    state.rules[e.target.dataset.i].text = e.target.value;
    autoGrow(e.target);
    setDirty(true);
  };
  list.onchange = (e) => {
    const t = e.target.dataset;
    if (t.kind !== undefined) {
      const r = state.rules[t.kind];
      r.kind = e.target.value;
      if (r.kind !== "prohibition" && r.kind !== "everything") r.action = null;
      setDirty(true);
      renderRules();
    } else if (t.act !== undefined) {
      state.rules[t.act].action = e.target.value === "auto" ? null : e.target.value;
      $(`#hint-${t.act} span`).textContent = hintFor(state.rules[t.act]);
      setDirty(true);
    }
  };
  list.onclick = (e) => {
    if (e.target.dataset.del === undefined) return;
    const index = Number(e.target.dataset.del);
    const remove = () => {
      state.rules.splice(index, 1);
      haptic("medium");
      setDirty(true);
      renderRules();
    };
    const text = state.rules[index].text.trim();
    if (text) confirmAction(`Удалить правило «${text.slice(0, 80)}»?`, remove);
    else remove();
  };
  $("#add").onclick = () => {
    state.rules.push({ kind: "prohibition", text: "", action: null });
    setDirty(true);
    renderRules();
    const boxes = document.querySelectorAll("textarea.txt");
    if (boxes.length) boxes[boxes.length - 1].focus();
  };
  $("#parse").onclick = parseText;
}

function hintFor(rule) {
  if (rule.kind === "everything")
    return (rule.action ? ACTION_HINT[rule.action] : "Удалит сообщение молча.") + " Действует на все комментарии без разбора.";
  return ACTION_HINT[rule.action || "auto"];
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}

async function parseText() {
  const text = $("#paste").value.trim();
  if (!text) return;
  const btn = $("#parse");
  btn.disabled = true;
  btn.textContent = "Разбираю…";
  try {
    const res = await api("POST", `/chat/${state.chatId}/parse`, { text });
    state.rules.push(...res.rules);
    setDirty(true);
    renderRules();
    toast("Добавлено правил: " + res.rules.length + ". Проверьте действия и нажмите «Сохранить»");
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.innerHTML = PARSE_LABEL;
  }
}

async function saveRules() {
  const rules = state.rules.map((r) => ({ ...r, text: r.text.trim() })).filter((r) => r.text);
  setBusy(true);
  try {
    await api("PUT", `/chat/${state.chatId}/rules`, { rules });
    state.rules = rules;
    setDirty(false);
    toast("Правила сохранены ✅");
    haptic("success");
    renderTab();
  } catch (e) {
    fail(e);
  } finally {
    setBusy(false);
  }
}

// --------------------------------------------------------------------- settings

async function saveSetting(key, value) {
  const s = state.data.settings;
  const old = s[key];
  s[key] = value;
  try {
    await api("PUT", `/chat/${state.chatId}/settings`, { [key]: value });
    toast("Сохранено ✅");
    haptic("light");
  } catch (e) {
    s[key] = old;
    fail(e);
  }
  renderHead();
  renderTab();
}

// One row of the settings list: a colored icon, a title with a hint, a switch.
function toggle(key, ico, color, title, hint, danger) {
  const on = state.data.settings[key];
  return `<div class="set ${danger ? "dangerous" : ""}"><div class="ico" style="background:${color}">${icon(ico)}</div>
    <div class="lbl">${title}<small>${hint}</small></div>
    <label class="sw"><input type="checkbox" data-set="${key}" ${on ? "checked" : ""} aria-label="${title}"><i></i></label></div>`;
}

const INT_LIMITS = { night_from: [0, 23], night_to: [0, 23] };

const FLOOD_FIELDS = [
  ["flood_messages", "Сообщений", 3, 30],
  ["flood_window", "За секунд", 5, 120],
  ["flood_mute", "Мут, минут", 1, 10080],
];

function numberFields(fields) {
  const s = state.data.settings;
  return `<div class="sub">${fields
    .map(
      ([key, label, low, high]) => `<label class="num"><small>${label}</small>
      <input type="number" inputmode="numeric" min="${low}" max="${high}" value="${esc(s[key])}" data-int="${key}"></label>`
    )
    .join("")}</div>`;
}

function floodFields() {
  return state.data.settings.antiflood ? numberFields(FLOOD_FIELDS) : "";
}

function nightFields() {
  return state.data.settings.night_mode
    ? numberFields([["night_from", "С часа (МСК)", 0, 23], ["night_to", "До часа (МСК)", 0, 23]])
    : "";
}

const LINK_HINT = {
  ai: "ИИ удаляет подозрительные ссылки, остальные можно.",
  block: "Разрешены только ссылки из списка ниже.",
  newcomers: "Новичкам ссылки нельзя, остальным решает ИИ.",
};

function linksCard() {
  const l = state.data.links;
  return `<div class="h">Ссылки</div>
    <div class="group">
      <div class="block"><div class="title">Что можно публиковать</div>
        <div class="seg">${[["ai", "Решает ИИ"], ["block", "Из списка"], ["newcomers", "Не новичкам"]]
          .map(([v, label]) => `<button data-links-mode="${v}" class="${l.mode === v ? "on" : ""}">${label}</button>`)
          .join("")}</div>
        <div class="tag">${esc(LINK_HINT[l.mode])}</div></div>
      <div class="block">
        <label class="field-label" style="margin-top:0">Разрешённые адреса</label>
        <textarea class="paste" rows="3" data-links-list="allowed" style="min-height:64px" placeholder="t.me/mychannel&#10;example.com">${esc(l.allowed.join("\n"))}</textarea>
        <label class="field-label">Запрещённые: сообщение с такой ссылкой удаляется сразу</label>
        <textarea class="paste" rows="3" data-links-list="blocked" style="min-height:64px" placeholder="casino.com">${esc(l.blocked.join("\n"))}</textarea>
        <div class="tag">По одному в строке: сайт или канал вроде t.me/mychannel</div>
      </div>
    </div>`;
}

async function saveLinks(patch) {
  try {
    const res = await api("PUT", `/chat/${state.chatId}/links`, patch);
    Object.assign(state.data.links, patch, { allowed: res.allowed, blocked: res.blocked });
    toast("Сохранено ✅");
    haptic("light");
  } catch (e) {
    fail(e);
  }
  renderTab();
}

function copyCard() {
  const others = state.chats.filter((c) => c.chat_id !== state.chatId);
  if (!others.length) return "";
  return `<div class="h">Перенос настроек</div>
    <div class="group"><div class="block">
      <div class="title">Скопировать из другого чата</div>
      <div class="copy-row">
        <select id="copySource" aria-label="Чат-источник">${others.map((c) => `<option value="${esc(c.chat_id)}">${esc(c.title)}</option>`).join("")}</select>
        <button class="btn soft small" id="copyGo">${icon("copy")}Скопировать</button>
      </div>
      <div class="tag">Правила и настройки заменятся. Владелец, доверенные и накопленные решения останутся.</div>
    </div></div>`;
}

async function copySettings() {
  const select = $("#copySource");
  const title = select.options[select.selectedIndex].text;
  confirmAction(`Правила и настройки этого чата будут заменены настройками чата «${title}». Продолжить?`, async () => {
    try {
      await api("POST", `/chat/${state.chatId}/copy`, { from: Number(select.value) });
      toast("Настройки скопированы ✅");
      haptic("success");
      openChat(state.chatId);
    } catch (e) {
      fail(e);
    }
  });
}

function segmented(key, options) {
  const cur = state.data.settings[key];
  return `<div class="seg">${options
    .map(([v, label]) => `<button data-seg="${key}" data-v="${v}" class="${cur === v ? "on" : ""}">${label}</button>`)
    .join("")}</div>`;
}

function renderSettings() {
  const s = state.data.settings;
  const trusted = state.data.trusted
    .map(
      (t) => `<div class="set">${avatar(t.name || "Пользователь", t.user_id, "xs")}
        <div class="lbl">${esc(t.name || "Пользователь")}<small>ID ${esc(t.user_id)}</small></div>
        <button class="btn ghost small" data-untrust="${esc(t.user_id)}">Убрать</button></div>`
    )
    .join("");
  $("#view").innerHTML = `
    <div class="h">Режим работы</div>
    <div class="group">
      ${toggle("enabled", "power", "#34c759", "Модерация", "Выключите, чтобы бот временно ничего не проверял")}
      ${toggle("observe", "eye", "#ff9500", "Пробный режим", "Бот ничего не удаляет, только записывает, что сделал бы. Первые находки пришлёт вам в личку, итог в /report")}
      <div class="block"><div class="title">Строгость</div>
        ${segmented("mode", [["soft", "Мягко"], ["normal", "Обычно"], ["strict", "Строго"]])}
        <div class="tag">${MODE_HINT[s.mode]}</div></div>
    </div>

    <div class="h">Защита от спама</div>
    <div class="group">
      ${toggle("antiflood", "zap", "#ff2d55", "Антифлуд", "Мут, если человек пишет слишком часто: столько сообщений за столько секунд")}
      ${floodFields()}
      ${toggle("escalation", "repeat", "#ff3b30", "Мут и бан за повторы", "3-е нарушение за 30 дней — мут на сутки, 5-е — бан")}
      ${toggle("first_strict", "timer", "#5856d6", "Строго к первым комментариям", "Новички в первые 2 минуты после поста (так спам-боты занимают первое место) проверяются строже")}
      ${toggle("image_ocr", "image", "#af52de", "Читать текст на картинках", "Реклама картинкой или стикером у новичков. На автобан не влияет")}
      ${toggle("night_mode", "moon", "#3a4a8c", "Ночной режим", "В эти часы проверка строже, а новичкам нельзя ссылки")}
      ${nightFields()}
    </div>

    <div class="h">Новички</div>
    <div class="group">
      ${toggle("profile_check", "user", "#007aff", "Проверка профиля", "Имя, ник, описание и аватарка: ловит аккаунты-приманки")}
      ${toggle("captcha", "robot", "#30b0c7", "Проверка «я человек»", "После первого сообщения новичок нажимает нужную кнопку за 2 минуты, иначе мут на сутки")}
    </div>

    <div class="h">Рейды и порядок</div>
    <div class="group">
      ${toggle("antiraid", "users", "#ff6b35", "Предупреждать о рейдах", "Сообщу, если за минуту зашло много новых участников, и предложу заморозить чат")}
      ${toggle("clean_service", "eraser", "#8e8e93", "Убирать служебные сообщения", "«Вошёл в группу», «вышел» и подобные записи")}
      ${toggle("lockdown", "alert", "#d03b3b", "Удалять всё от участников", "Временная мера при рейде: удаляются все сообщения не-админов", true)}
    </div>

    ${linksCard()}

    <div class="h">Уведомления и аналитика</div>
    <div class="group">
      ${toggle("analytics", "pie", "#5ac8fa", "Аналитика комментариев", "Тип, тон и «ждёт ответа» для каждого комментария")}
      ${toggle("conflicts", "flame", "#ff9500", "Предупреждать о ссорах", "Сообщу вам, если обсуждение накаляется")}
      ${toggle("digest", "news", "#34aadc", "Ежедневная сводка", "Каждый день в 09:00 по Москве")}
      <div class="block"><div class="title">Полезные комментарии присылать</div>
        ${segmented("useful_mode", [["digest", "В сводку"], ["instant", "Сразу"], ["off", "Не нужно"]])}</div>
    </div>

    ${copyCard()}

    <div class="h">Не проверять</div>
    <div class="group">${trusted || '<div class="block tag" style="margin:0">Никого. Добавить можно командой /trust ответом на сообщение или кнопкой «Доверять» в журнале.</div>'}</div>`;

  const view = $("#view");
  view.onclick = (e) => {
    const t = e.target;
    if (t.dataset.seg) saveSetting(t.dataset.seg, t.dataset.v);
    if (t.dataset.linksMode) saveLinks({ mode: t.dataset.linksMode });
    if (t.id === "copyGo") copySettings();
    if (t.dataset.untrust) untrust(Number(t.dataset.untrust));
  };
  view.onchange = (e) => {
    if (e.target.dataset.set) {
      const key = e.target.dataset.set;
      const on = e.target.checked;
      const warning =
        key === "lockdown" && on
          ? "Включить удаление всех сообщений участников? Пока это включено, бот удаляет всё, что пишут не-админы."
          : key === "observe" && !on
            ? "Выключить пробный режим? Бот начнёт по-настоящему удалять сообщения."
            : key === "enabled" && !on
              ? "Выключить модерацию? Бот перестанет проверять комментарии."
              : null;
      if (warning) confirmAction(warning, () => saveSetting(key, on), renderSettings);
      else saveSetting(key, on);
    }
    if (e.target.dataset.linksList) {
      const lines = e.target.value.split("\n").map((x) => x.trim()).filter(Boolean);
      saveLinks({ [e.target.dataset.linksList]: lines });
    }
    if (e.target.dataset.int) {
      const [low, high] = INT_LIMITS[e.target.dataset.int] || FLOOD_FIELDS.find((f) => f[0] === e.target.dataset.int).slice(2);
      const value = Math.min(high, Math.max(low, parseInt(e.target.value, 10) || low));
      saveSetting(e.target.dataset.int, value);
    }
  };
}

async function untrust(userId) {
  try {
    await api("DELETE", `/chat/${state.chatId}/trusted/${userId}`);
    state.data.trusted = state.data.trusted.filter((t) => t.user_id !== userId);
    renderTab();
  } catch (e) {
    toast(e.message);
  }
}

// -------------------------------------------------------------------- insights

function messageLink(messageId) {
  return `https://t.me/c/${String(state.chatId).replace("-100", "")}/${messageId}`;
}

function when(ts) {
  return new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

async function renderInsights() {
  $("#view").innerHTML = skeleton();
  let data;
  try {
    data = await api("GET", `/chat/${state.chatId}/insights?days=7`);
  } catch (e) {
    $("#view").innerHTML = emptyState("alert", "", esc(e.message));
    return;
  }
  if (state.tab !== "insights") return;
  if (!data.analytics) {
    $("#view").innerHTML = emptyState("pie", "Аналитика выключена", "Включите её в настройках, и бот начнёт размечать комментарии.");
    return;
  }
  const kinds = Object.entries(data.kinds).sort((a, b) => b[1] - a[1]);
  const total = kinds.reduce((sum, k) => sum + k[1], 0);
  const max = Math.max(1, ...kinds.map((k) => k[1]));
  // One series, one color; the value sits at the end of its bar.
  const bars = kinds
    .map(([k, n]) => {
      const name = esc(KIND_LABEL[k] || k);
      return `<div class="bar" title="${name}: ${n}"><span>${name}</span>
        <div class="track"><div class="fill" style="width:calc((100% - 34px) * ${(n / max).toFixed(3)})"></div><b>${n}</b></div></div>`;
    })
    .join("");
  const mood = data.mood;
  const neutral = Math.max(0, mood.n - mood.negative - mood.positive);
  const parts = [["n", "негативные", mood.negative], ["m", "нейтральные", neutral], ["p", "позитивные", mood.positive]];
  const moodCard =
    mood.n >= 5
      ? `<div class="card"><div class="mood" role="img" aria-label="Тон: негативных ${mood.negative}, нейтральных ${neutral}, позитивных ${mood.positive}">
          ${parts.filter((p) => p[2] > 0).map(([c, label, n]) => `<i class="${c}" style="flex:${n}" title="${label}: ${n}"></i>`).join("")}</div>
          <div class="legend">${parts.map(([c, label, n]) => `<span><i class="mood-key ${c}"></i>${label} <b>${n}</b></span>`).join("")}</div></div>`
      : '<div class="card tag">Нужно хотя бы 5 размеченных комментариев, чтобы показать тон.</div>';
  const leads = data.waiting.filter((w) => w.lead >= 0.6).length;
  const waiting = data.waiting
    .map(
      (w) => `<div class="card entry">
        <div class="top">${avatar(w.user_name || "Без имени", w.user_name, "sm")}
          <div class="name"><b>${esc(w.user_name || "Без имени")}</b><small>${esc(when(w.ts))}</small></div>
          ${w.lead >= 0.6 ? `<span class="pill good">${icon("cart")}хочет купить</span>` : `<span class="pill warn">${icon("clock")}ждёт ответа</span>`}</div>
        <blockquote>${esc(w.text)}</blockquote>
        <div class="acts"><button class="btn soft" data-open="${esc(messageLink(w.message_id))}">${icon("open")}Открыть в чате</button></div></div>`
    )
    .join("");
  $("#view").innerHTML = `
    <div class="kpis" style="margin-top:12px"><div class="cap">За 7 дней</div>
      <div class="kpi"><b>${total}</b><span>размечено</span></div>
      <div class="kpi ${data.waiting.length ? "alert" : ""}"><b>${data.waiting.length}</b><span>ждут ответа</span></div>
      <div class="kpi"><b>${leads}</b><span>хотят купить</span></div>
    </div>
    <div class="h">О чём пишут</div>
    <div class="card">${bars || '<div class="tag">Пока нет данных: бот размечает комментарии по мере проверки.</div>'}</div>
    <div class="h">Тон комментариев</div>
    ${moodCard}
    <div class="h">Ждут вашего ответа <span class="count">${data.waiting.length || ""}</span></div>
    ${waiting || emptyState("check", "", "Всё разобрано: вопросов без ответа нет.")}`;
  $("#view").onclick = (e) => {
    const url = e.target.dataset.open;
    if (!url) return;
    if (tg.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, "_blank");
  };
}

// -------------------------------------------------------------------------- log

async function renderLog() {
  if (!state.log) $("#view").innerHTML = skeleton();
  try {
    state.log = await api("GET", `/chat/${state.chatId}/log`);
  } catch (e) {
    $("#view").innerHTML = emptyState("alert", "", esc(e.message));
    return;
  }
  if (state.tab !== "log") return;
  state.data.stats = state.log.stats;
  renderHead();
  drawLog();
}

function entryCard(e) {
  const [label, tone, ico] = ACTION_PILL[e.action] || [e.action, "", "info"];
  const conf =
    e.confidence == null
      ? ""
      : `<span class="conf" title="Уверенность бота"><span class="track"><i style="width:${Math.round(e.confidence * 100)}%"></i></span>${Math.round(e.confidence * 100)}%</span>`;
  let acts = "";
  let status = "";
  const dryRun = e.executed === false && e.action !== "send_to_review";
  if (dryRun) status = `<span class="pill note">${icon("eye")}пробный режим: не удалено</span>`;
  else if (e.feedback === "not_spam") status = `<span class="pill good note">${icon("check")}решено: оставлено</span>`;
  else if (e.feedback === "confirmed") status = `<span class="pill note">${icon("trash")}решено: удалено</span>`;
  else if (e.action === "send_to_review")
    acts = `<div class="acts"><button class="btn primary" data-review="${e.id}" data-del="1">${icon("trash")}Удалить</button>
      <button class="btn ghost" data-review="${e.id}" data-del="0">${icon("check")}Оставить</button></div>`;
  else
    acts = `<div class="acts"><button class="btn ghost" data-restore="${e.id}">${icon("undo")}Вернуть</button>
      <button class="btn ghost" data-trust="${e.id}">${icon("star")}Доверять</button></div>`;
  return `<div class="card entry">
    <div class="top">${avatar(e.user_name || "Без имени", e.user_id, "sm")}
      <div class="name"><b>${esc(e.user_name || "Без имени")}</b><small>${esc(when(e.ts))}</small></div>
      <span class="pill ${tone}">${icon(ico)}${esc(label)}</span></div>
    <blockquote>${esc(e.text)}</blockquote>
    <div class="reason"><span>${esc(e.reason || "")}</span>${conf}</div>
    ${status}${acts}</div>`;
}

function drawLog() {
  const { entries } = state.log;
  const waiting = entries.filter((e) => e.action === "send_to_review" && !e.feedback);
  const rest = entries.filter((e) => !waiting.includes(e));
  $("#view").innerHTML = `
    ${waiting.length ? `<div class="h">Ждут вашего решения <span class="count">${waiting.length}</span></div>` + waiting.map(entryCard).join("") : ""}
    <div class="h">Последние действия</div>
    ${rest.map(entryCard).join("") || emptyState("shieldCheck", "Тихо", "Пока бот ничего не удалял.")}`;
  $("#view").onclick = async (e) => {
    const d = e.target.dataset;
    const base = `/chat/${state.chatId}/log/`;
    try {
      if (d.review) {
        const res = await api("POST", base + d.review + "/review", { delete: d.del === "1" });
        toast(res.result);
      } else if (d.restore) {
        await api("POST", base + d.restore + "/restore");
        toast("Комментарий восстановлен ✅");
      } else if (d.trust) {
        await api("POST", base + d.trust + "/trust");
        state.data = await api("GET", "/chat/" + state.chatId);
        toast("Больше не проверяю этого автора ⭐");
        return;
      } else return;
      haptic("success");
      renderLog();
    } catch (err) {
      toast(err.message);
    }
  };
}

// ------------------------------------------------------------------------ start

function applyTheme() {
  // Follow the Telegram theme, not the phone's: someone can use dark Telegram on a light phone.
  if (tg && tg.colorScheme) document.documentElement.dataset.theme = tg.colorScheme;
  try {
    if (tg.isVersionAtLeast && tg.isVersionAtLeast("6.1")) {
      tg.setHeaderColor("secondary_bg_color");
      tg.setBackgroundColor("secondary_bg_color");
    }
  } catch (_) {
    /* older clients keep their own colors */
  }
}

async function start() {
  if (!tg || !tg.initData) {
    $("#app").innerHTML = emptyState("chat", "Откройте панель из Telegram", "Команда /panel у бота @DefenceAiBot.");
    return;
  }
  tg.ready();
  tg.expand();
  applyTheme();
  if (tg.onEvent) tg.onEvent("themeChanged", applyTheme);
  $("#save").onclick = saveRules;
  if (nativeMain()) tg.MainButton.onClick(saveRules);
  if (nativeBack()) tg.BackButton.onClick(goBack);
  try {
    state.chats = (await api("GET", "/chats")).chats;
  } catch (e) {
    $("#app").innerHTML = emptyState("alert", "", esc(e.message));
    return;
  }
  const wanted = Number(new URLSearchParams(location.search).get("chat"));
  const chat = state.chats.find((c) => c.chat_id === wanted) || (state.chats.length === 1 ? state.chats[0] : null);
  if (chat) openChat(chat.chat_id);
  else renderPicker();
}

start();
