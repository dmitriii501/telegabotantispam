"use strict";

const tg = window.Telegram && window.Telegram.WebApp;
const $ = (s) => document.querySelector(s);

// Comments come from strangers: everything that reaches innerHTML goes through esc().
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ACTIONS = {
  ban: "Бан",
  mute: "Мут на сутки",
  warn: "С предупреждением",
  delete: "Просто удалить",
  review: "Прислать мне",
  auto: "По ситуации",
};
const KINDS = {
  prohibition: ["❌", "Запрет"],
  permission: ["✅", "Разрешено"],
  context: ["ℹ️", "Описание канала"],
  everything: ["🚫", "Ко всем комментариям"],
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
const ACTION_PILL = {
  delete_and_ban: ["бан", "bad"],
  delete_and_mute: ["мут", "bad"],
  delete_and_explain: ["предупреждение", "warn"],
  delete_silent: ["удалено", ""],
  send_to_review: ["ждёт вас", "warn"],
};

const state = { chats: [], chatId: null, data: null, rules: [], dirty: false, tab: "rules", log: null };

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("on");
  setTimeout(() => el.classList.remove("on"), 2200);
}

async function api(method, path, body) {
  const res = await fetch("/api" + path, {
    method,
    headers: { "X-Init-Data": tg ? tg.initData : "", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Ошибка " + res.status);
  return data;
}

function setDirty(value) {
  state.dirty = value;
  $("#savebar").hidden = !(value && state.tab === "rules");
}

// ------------------------------------------------------------------ chat picker

function renderPicker() {
  setDirty(false);
  const items = state.chats
    .map(
      (c) => `<div class="card set"><div>${esc(c.title)}<small>${c.enabled ? "модерация включена" : "модерация выключена"}</small></div>
        <button class="btn ghost small" data-chat="${esc(c.chat_id)}">Открыть</button></div>`
    )
    .join("");
  $("#app").innerHTML = `<header><h1>Ваши чаты</h1><div class="sub">Где вы админ и где работает бот</div></header>
    <section>${items || '<p class="empty">Здесь пока пусто. Добавьте бота в группу обсуждений канала, сделайте админом и напишите там /rules.</p>'}</section>`;
  $("#app").querySelectorAll("[data-chat]").forEach((b) => (b.onclick = () => openChat(Number(b.dataset.chat))));
}

// -------------------------------------------------------------------- main view

async function openChat(chatId) {
  try {
    state.chatId = chatId;
    state.data = await api("GET", "/chat/" + chatId);
  } catch (e) {
    toast(e.message);
    return;
  }
  state.rules = state.data.rules.map((r) => ({ ...r }));
  state.log = null;
  state.tab = "rules";
  renderApp();
}

function chatTitle() {
  const chat = state.chats.find((c) => c.chat_id === state.chatId);
  return chat ? chat.title : "Чат";
}

function renderApp() {
  const s = state.data.settings;
  $("#app").innerHTML = `
    <header>
      <h1>${esc(chatTitle())}</h1>
      <div class="sub"><span class="dot ${s.enabled ? "" : "off"}"></span>
        ${!s.enabled ? "Модерация выключена" : s.observe ? "Режим наблюдения: ничего не удаляю" : "Модерация включена"}
        ${state.chats.length > 1 ? '<span>·</span><button class="link" id="switch">сменить чат</button>' : ""}</div>
    </header>
    <nav id="tabs">
      ${[["rules", "Правила"], ["insights", "Аналитика"], ["settings", "Настройки"], ["log", "Журнал"]]
        .map(([id, name]) => `<button data-t="${id}" class="${state.tab === id ? "on" : ""}">${name}</button>`)
        .join("")}
    </nav>
    <section id="view"></section>`;
  const sw = $("#switch");
  if (sw) sw.onclick = () => (setDirty(false), renderPicker());
  $("#tabs").querySelectorAll("button").forEach((b) => (b.onclick = () => switchTab(b.dataset.t)));
  renderTab();
}

function switchTab(tab) {
  state.tab = tab;
  $("#tabs").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.t === tab));
  setDirty(state.dirty);
  renderTab();
}

function renderTab() {
  const view = $("#view");
  view.onclick = null;
  view.onchange = null;
  if (state.tab === "rules") renderRules();
  else if (state.tab === "settings") renderSettings();
  else if (state.tab === "insights") renderInsights();
  else renderLog();
}

// ------------------------------------------------------------------------ rules

function renderRules() {
  const cards = state.rules
    .map((r, i) => {
      const kinds = Object.entries(KINDS)
        .map(([k, v]) => `<option value="${k}" ${k === r.kind ? "selected" : ""}>${v[1]}</option>`)
        .join("");
      const acts =
        r.kind === "prohibition" || r.kind === "everything"
          ? `<select data-act="${i}">${Object.entries(ACTIONS)
              .map(([k, v]) => `<option value="${k}" ${k === (r.action || "auto") ? "selected" : ""}>${v}</option>`)
              .join("")}</select>`
          : "";
      return `<div class="card rule">
        <div class="top"><span class="icon">${KINDS[r.kind][0]}</span>
          <textarea class="txt" rows="1" maxlength="300" data-i="${i}">${esc(r.text)}</textarea>
          <button class="del" data-del="${i}" title="Удалить правило">✕</button></div>
        <div class="row"><select data-kind="${i}">${kinds}</select>${acts}</div></div>`;
    })
    .join("");
  $("#view").innerHTML = `
    <div class="h">Правила чата</div>
    <div id="ruleList">${cards || '<p class="empty">Правил пока нет. Добавьте первое или вставьте текстом.</p>'}</div>
    <div class="card base"><span>🛡</span><span>Спам, казино, наркотики и мошенничество удаляются всегда. Это правило нельзя отключить.</span></div>
    <button class="btn ghost wide" id="add">＋ Добавить правило</button>
    <div class="h">Или вставьте текстом</div>
    <textarea class="paste" id="paste" maxlength="2000" placeholder="Например: рекламу нельзя — бан, политику — удалять"></textarea>
    <button class="btn ghost wide" style="margin-top:8px" id="parse">Разобрать текст на правила</button>`;

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
      setDirty(true);
    }
  };
  list.onclick = (e) => {
    if (e.target.dataset.del === undefined) return;
    state.rules.splice(Number(e.target.dataset.del), 1);
    setDirty(true);
    renderRules();
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
    toast("Добавил " + res.rules.length + ". Проверьте действия и сохраните");
  } catch (e) {
    toast(e.message);
    btn.disabled = false;
    btn.textContent = "Разобрать текст на правила";
  }
}

async function saveRules() {
  const rules = state.rules.map((r) => ({ ...r, text: r.text.trim() })).filter((r) => r.text);
  const btn = $("#save");
  btn.disabled = true;
  try {
    await api("PUT", `/chat/${state.chatId}/rules`, { rules });
    state.rules = rules;
    setDirty(false);
    toast("Правила сохранены ✅");
    renderTab();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
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
  } catch (e) {
    s[key] = old;
    toast(e.message);
  }
  renderApp();
}

function toggle(key, title, hint) {
  const on = state.data.settings[key];
  return `<div class="set"><div>${title}<small>${hint}</small></div>
    <label class="sw"><input type="checkbox" data-set="${key}" ${on ? "checked" : ""}><i></i></label></div>`;
}

const FLOOD_FIELDS = [
  ["flood_messages", "Сообщений подряд", 3, 30],
  ["flood_window", "За сколько секунд", 5, 120],
  ["flood_mute", "Мут, минут", 1, 10080],
];

function floodFields() {
  const s = state.data.settings;
  if (!s.antiflood) return "";
  return `<div class="set flood">${FLOOD_FIELDS.map(
    ([key, label, low, high]) => `<label class="num"><small>${label}</small>
      <input type="number" inputmode="numeric" min="${low}" max="${high}" value="${esc(s[key])}" data-int="${key}"></label>`
  ).join("")}</div>`;
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
      (t) => `<div class="set"><div>${esc(t.name || "Пользователь")}<small>ID ${esc(t.user_id)}</small></div>
        <button class="btn ghost small" data-untrust="${esc(t.user_id)}">Убрать</button></div>`
    )
    .join("");
  $("#view").innerHTML = `
    <div class="h">Строгость</div>
    <div class="card"><div>Насколько уверенно удалять</div>
      ${segmented("mode", [["soft", "Мягко"], ["normal", "Обычно"], ["strict", "Строго"]])}
      <div class="tag" style="margin-top:8px">${MODE_HINT[s.mode]}</div></div>
    <div class="h">Поведение</div>
    <div class="card">
      ${toggle("enabled", "Модерация", "Выключите, чтобы бот временно ничего не проверял")}
      ${toggle("observe", "Режим наблюдения", "Бот ничего не удаляет, только записывает, что сделал бы. Итог в /report")}
      ${toggle("antiflood", "Антифлуд", "Мут за много сообщений подряд. Пороги ниже настраиваются")}
      ${floodFields()}
      ${toggle("analytics", "Аналитика комментариев", "Тип, тон и «ждёт ответа» для каждого комментария")}
      ${toggle("lockdown", "Режим тишины", "Удалять все сообщения не-админов")}
      ${toggle("escalation", "Мут и бан за повторы", "3-е нарушение — мут на сутки, 5-е — бан")}
      ${toggle("conflicts", "Предупреждать о ссорах", "Сообщу вам, если обсуждение накаляется")}
      ${toggle("digest", "Ежедневная сводка", "Каждый день в 09:00 по Москве")}
    </div>
    <div class="h">Полезные комментарии</div>
    <div class="card">${segmented("useful_mode", [["digest", "В сводку"], ["instant", "Сразу"], ["off", "Не нужно"]])}</div>
    <div class="h">Не проверять</div>
    <div class="card">${trusted || '<div class="tag">Никого. Добавить можно командой /trust ответом на сообщение или в журнале.</div>'}</div>`;

  const view = $("#view");
  view.onclick = (e) => {
    const t = e.target;
    if (t.dataset.seg) saveSetting(t.dataset.seg, t.dataset.v);
    if (t.dataset.untrust) untrust(Number(t.dataset.untrust));
  };
  view.onchange = (e) => {
    if (e.target.dataset.set) saveSetting(e.target.dataset.set, e.target.checked);
    if (e.target.dataset.int) {
      const [, , low, high] = FLOOD_FIELDS.find((f) => f[0] === e.target.dataset.int);
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

async function renderInsights() {
  $("#view").innerHTML = '<p class="empty">Загрузка…</p>';
  let data;
  try {
    data = await api("GET", `/chat/${state.chatId}/insights?days=7`);
  } catch (e) {
    $("#view").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }
  if (!data.analytics) {
    $("#view").innerHTML = '<p class="empty">Аналитика выключена. Включите её в настройках, и бот начнёт размечать комментарии.</p>';
    return;
  }
  const kinds = Object.entries(data.kinds).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...kinds.map((k) => k[1]));
  const bars = kinds
    .map(
      ([k, n]) => `<div class="bar"><span>${esc(KIND_LABEL[k] || k)}</span>
        <div class="track"><div class="fill" style="width:${Math.round((n / max) * 100)}%"></div></div><b>${n}</b></div>`
    )
    .join("");
  const mood = data.mood;
  const moodLine = mood.n >= 5
    ? `<div class="tag" style="margin-top:8px">Тон: негативных ${mood.negative}, позитивных ${mood.positive} из ${mood.n}</div>`
    : "";
  const waiting = data.waiting
    .map(
      (w) => `<div class="card entry">
        <div class="who"><span>${esc(w.user_name || "Без имени")}</span>
          <span class="pill ${w.lead >= 0.6 ? "good" : "warn"}">${w.lead >= 0.6 ? "🛒 хочет купить" : "ждёт ответа"}</span></div>
        <blockquote>${esc(w.text)}</blockquote>
        <div class="acts"><button class="btn ghost" data-open="${esc(messageLink(w.message_id))}">Открыть в чате</button></div></div>`
    )
    .join("");
  $("#view").innerHTML = `
    <div class="h">О чём пишут за неделю</div>
    <div class="card">${bars || '<div class="tag">Пока нет данных: бот размечает комментарии по мере проверки.</div>'}${moodLine}</div>
    <div class="h">Ждут вашего ответа</div>
    ${waiting || '<p class="empty">Всё разобрано: вопросов без ответа нет.</p>'}`;
  $("#view").onclick = (e) => {
    const url = e.target.dataset.open;
    if (!url) return;
    if (tg.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, "_blank");
  };
}

// -------------------------------------------------------------------------- log

async function renderLog() {
  $("#view").innerHTML = '<p class="empty">Загрузка…</p>';
  try {
    state.log = await api("GET", `/chat/${state.chatId}/log`);
  } catch (e) {
    $("#view").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }
  drawLog();
}

function entryCard(e) {
  const [label, tone] = ACTION_PILL[e.action] || [e.action, ""];
  const when = new Date(e.ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const conf = e.confidence == null ? "" : ` · ${Math.round(e.confidence * 100)}%`;
  let acts = "";
  let status = "";
  const dryRun = e.executed === false && e.action !== "send_to_review";
  if (dryRun) status = '<span class="pill">наблюдение: не удалено</span>';
  else if (e.feedback === "not_spam") status = '<span class="pill good">решено: оставлено</span>';
  else if (e.feedback === "confirmed") status = '<span class="pill">решено: удалено</span>';
  else if (dryRun) acts = "";
  else if (e.action === "send_to_review")
    acts = `<div class="acts"><button class="btn primary" data-review="${e.id}" data-del="1">🗑 Удалить</button>
      <button class="btn ghost" data-review="${e.id}" data-del="0">✅ Оставить</button></div>`;
  else
    acts = `<div class="acts"><button class="btn ghost" data-restore="${e.id}">↩️ Вернуть</button>
      <button class="btn ghost" data-trust="${e.id}">⭐ Доверять</button></div>`;
  return `<div class="card entry">
    <div class="who"><span>${esc(e.user_name || "Без имени")} · ${esc(when)}</span><span class="pill ${tone}">${esc(label + conf)}</span></div>
    <blockquote>${esc(e.text)}</blockquote>
    <span class="tag">${esc(e.reason || "")}</span> ${status}${acts}</div>`;
}

function drawLog() {
  const { stats, entries } = state.log;
  const waiting = entries.filter((e) => e.action === "send_to_review" && !e.feedback);
  const rest = entries.filter((e) => !waiting.includes(e));
  $("#view").innerHTML = `
    <div class="stats">
      <div class="stat"><b>${stats.checked}</b><span>проверено за сутки</span></div>
      <div class="stat"><b>${stats.deleted}</b><span>удалено</span></div>
      <div class="stat"><b>${stats.pending}</b><span>ждут вас</span></div>
    </div>
    ${waiting.length ? '<div class="h">Ждут вашего решения</div>' + waiting.map(entryCard).join("") : ""}
    <div class="h">Последние удаления</div>
    ${rest.map(entryCard).join("") || '<p class="empty">Пока ничего не удалялось.</p>'}`;
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
      renderLog();
    } catch (err) {
      toast(err.message);
    }
  };
}

// ------------------------------------------------------------------------ start

async function start() {
  if (!tg || !tg.initData) {
    $("#app").innerHTML = '<p class="empty">Откройте панель из Telegram: команда /panel у бота.</p>';
    return;
  }
  tg.ready();
  tg.expand();
  $("#save").onclick = saveRules;
  try {
    state.chats = (await api("GET", "/chats")).chats;
  } catch (e) {
    $("#app").innerHTML = `<p class="empty">${esc(e.message)}</p>`;
    return;
  }
  const wanted = Number(new URLSearchParams(location.search).get("chat"));
  const chat = state.chats.find((c) => c.chat_id === wanted) || (state.chats.length === 1 ? state.chats[0] : null);
  if (chat) openChat(chat.chat_id);
  else renderPicker();
}

start();
