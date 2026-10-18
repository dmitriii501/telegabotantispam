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
  card: '<rect x="2.5" y="5" width="19" height="14" rx="3"/><path d="M2.5 10h19M6.5 15h3"/>',
};
const icon = (name) => `<svg class="i" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
const KIND_ICON = { prohibition: "x", permission: "check", context: "infoGlyph", everything: "ban" };

// Soft impressionist washes (water, lilies, willow, haystacks at dusk): a person keeps the same one everywhere.
const AVATAR = [
  ["#9fb3e8", "#6f86c9"], ["#e9b3c1", "#c9798f"], ["#a9c9b2", "#6f9a80"], ["#e8cf9c", "#c49a52"],
  ["#c3b9ea", "#8e82c9"], ["#a7d0d6", "#5f9aa6"], ["#efb9a0", "#cc8063"],
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

// More icons: the settings folders and the pay screen.
Object.assign(ICONS, {
  home: '<path d="M3 11 12 3l9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="3"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  spark: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
});

const num = (n) => Number(n).toLocaleString("ru-RU");
const dayWord = (n) => (n % 100 >= 11 && n % 100 <= 14 ? "дней" : n % 10 === 1 ? "день" : n % 10 >= 2 && n % 10 <= 4 ? "дня" : "дней");
const when = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const dateText = (ts) => new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });

// tab: home | rules | insights | settings.  screen: null or {name: "log" | "folder" | "pay", ...}: a page "inside" a tab.
const state = { chats: [], chatId: null, data: null, rules: [], dirty: false, tab: "home", screen: null, log: null, pay: null };

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.add("on");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("on"), 2400);
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
const atLeast = (v) => !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast(v));

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
  if (tg && tg.showConfirm && atLeast("6.2")) tg.showConfirm(text, done);
  else done(window.confirm(text));
}

function fail(e) {
  toast(e.message);
  haptic("error");
}

// The one big action button at the bottom: Telegram's own MainButton, or our bar in a plain browser.
let mainAction = null;

function setMain(text, fn) {
  mainAction = fn;
  if (nativeMain()) {
    tg.MainButton.setText(text);
    tg.MainButton.show();
    $("#savebar").hidden = true;
  } else {
    $("#save").textContent = text;
    $("#savebar").hidden = false;
  }
  document.body.classList.toggle("saving", !nativeMain());
}

function clearMain() {
  mainAction = null;
  if (nativeMain()) tg.MainButton.hide();
  $("#savebar").hidden = true;
  document.body.classList.remove("saving");
}

const runMain = () => mainAction && mainAction();

function setBusy(busy) {
  $("#save").disabled = busy;
  if (!nativeMain()) return;
  if (busy) tg.MainButton.showProgress(false);
  else tg.MainButton.hideProgress();
}

// Unsaved rules: the main button saves them, and Telegram asks before the panel is closed.
function setDirty(value) {
  state.dirty = value;
  syncMain();
  try {
    if (value) tg.enableClosingConfirmation();
    else tg.disableClosingConfirmation();
  } catch (_) {
    /* not supported */
  }
}

function syncMain() {
  if (state.screen && state.screen.name === "pay") return; // the pay screen runs its own button
  if (state.dirty && state.tab === "rules" && !state.screen) setMain("Сохранить правила", saveRules);
  else clearMain();
}
