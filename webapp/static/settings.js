"use strict";

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
  rerender();
}

// One row of the settings list: a colored icon, a title with a hint, a switch.
function toggle(key, ico, color, title, hint, danger) {
  const on = state.data.settings[key];
  return `<div class="set ${danger ? "dangerous" : ""}"><div class="ico">${icon(ico)}</div>
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
  return `<div class="group">
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
  rerender();
}

function copyCard() {
  const others = state.chats.filter((c) => c.chat_id !== state.chatId);
  if (!others.length) return "";
  return `<div class="group"><div class="block">
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



async function untrust(userId) {
  try {
    await api("DELETE", `/chat/${state.chatId}/trusted/${userId}`);
    state.data.trusted = state.data.trusted.filter((t) => t.user_id !== userId);
    rerender();
  } catch (e) {
    toast(e.message);
  }
}


// ---------------------------------------------------------------- the folders

const SETTING_KEYS = {
  spam: ["antiflood", "escalation", "first_strict", "image_ocr", "night_mode"],
  newbies: ["profile_check", "captcha"],
  raids: ["antiraid", "clean_service", "lockdown"],
  notify: ["analytics", "conflicts", "digest"],
};
const LINK_LABEL = { ai: "решает ИИ", block: "только из списка", newcomers: "не новичкам" };
const countOn = (keys) => `${keys.filter((k) => state.data.settings[k]).length} из ${keys.length} включено`;

// Each folder: an icon, a title, a line that tells its state without opening it, and its page.
const FOLDERS = {
  mode: {
    icon: "power",
    title: "Режим работы",
    hint: "Включение бота, пробный режим и строгость",
    summary: (s) => `${!s.enabled ? "Выключена" : s.observe ? "Пробный режим" : "Работает"} · ${MODE_SHORT[s.mode]}`,
    body: () => `
      <div class="group">
        ${toggle("enabled", "power", null, "Модерация", "Выключите, чтобы бот временно ничего не проверял")}
        ${toggle("observe", "eye", null, "Пробный режим", "Бот ничего не удаляет, только записывает, что сделал бы. Первые находки пришлёт вам в личку, итог в /report")}
      </div>
      <div class="h">Строгость</div>
      <div class="card">${segmented("mode", [["soft", "Мягко"], ["normal", "Обычно"], ["strict", "Строго"]])}
        <div class="tag" style="margin-top:10px">${MODE_HINT[state.data.settings.mode]}</div></div>`,
  },
  spam: {
    icon: "zap",
    title: "Защита от спама",
    hint: "Флуд, повторные нарушения, первые комментарии, картинки, ночь",
    summary: () => countOn(SETTING_KEYS.spam),
    body: () => `<div class="group">
      ${toggle("antiflood", "zap", null, "Антифлуд", "Мут, если человек пишет слишком часто: столько сообщений за столько секунд")}
      ${floodFields()}
      ${toggle("escalation", "repeat", null, "Мут и бан за повторы", "3-е нарушение за 30 дней — мут на сутки, 5-е — бан")}
      ${toggle("first_strict", "timer", null, "Строго к первым комментариям", "Новички в первые 2 минуты после поста (так спам-боты занимают первое место) проверяются строже")}
      ${toggle("image_ocr", "image", null, "Читать текст на картинках", "Реклама картинкой или стикером у новичков. На автобан не влияет")}
      ${toggle("night_mode", "moon", null, "Ночной режим", "В эти часы проверка строже, а новичкам нельзя ссылки")}
      ${nightFields()}</div>`,
  },
  newbies: {
    icon: "user",
    title: "Новички",
    hint: "Проверка тех, кто только что пришёл",
    summary: () => countOn(SETTING_KEYS.newbies),
    body: () => `<div class="group">
      ${toggle("profile_check", "user", null, "Проверка профиля", "Имя, ник, описание и аватарка: ловит аккаунты-приманки")}
      ${toggle("captcha", "robot", null, "Проверка «я человек»", "После первого сообщения новичок нажимает нужную кнопку за 2 минуты, иначе мут на сутки")}</div>`,
  },
  raids: {
    icon: "users",
    title: "Рейды и порядок",
    hint: "Массовые заходы и служебные сообщения",
    summary: () => countOn(SETTING_KEYS.raids),
    body: () => `<div class="group">
      ${toggle("antiraid", "users", null, "Предупреждать о рейдах", "Сообщу, если за минуту зашло много новых участников, и предложу заморозить чат")}
      ${toggle("clean_service", "eraser", null, "Убирать служебные сообщения", "«Вошёл в группу», «вышел» и подобные записи")}
      ${toggle("lockdown", "alert", null, "Удалять всё от участников", "Временная мера при рейде: удаляются все сообщения не-админов", true)}</div>`,
  },
  links: {
    icon: "link",
    title: "Ссылки",
    hint: "Что можно публиковать и списки адресов",
    summary: () => {
      const l = state.data.links;
      return `${LINK_LABEL[l.mode]} · ${l.allowed.length} разреш., ${l.blocked.length} запрещ.`;
    },
    body: () => linksCard(),
  },
  notify: {
    icon: "bell",
    title: "Уведомления и аналитика",
    hint: "Что бот присылает вам и что размечает",
    summary: () => countOn(SETTING_KEYS.notify),
    body: () => `<div class="group">
      ${toggle("analytics", "pie", null, "Аналитика комментариев", "Тип, тон и «ждёт ответа» для каждого комментария")}
      ${toggle("conflicts", "flame", null, "Предупреждать о ссорах", "Сообщу вам, если обсуждение накаляется")}
      ${toggle("digest", "news", null, "Ежедневная сводка", "Каждый день в 09:00 по Москве")}
      <div class="block"><div class="title">Полезные комментарии присылать</div>
        ${segmented("useful_mode", [["digest", "В сводку"], ["instant", "Сразу"], ["off", "Не нужно"]])}</div></div>`,
  },
  trusted: {
    icon: "star",
    title: "Не проверять",
    hint: "Люди, которых бот пропускает без проверки",
    summary: () => {
      const n = state.data.trusted.length;
      return n ? `${n} ${n === 1 ? "человек" : n < 5 ? "человека" : "человек"}` : "Никого";
    },
    body: () => {
      const rows = state.data.trusted
        .map(
          (t) => `<div class="set">${avatar(t.name || "Пользователь", t.user_id, "xs")}
        <div class="lbl">${esc(t.name || "Пользователь")}<small>ID ${esc(t.user_id)}</small></div>
        <button class="btn ghost small" data-untrust="${esc(t.user_id)}">Убрать</button></div>`
        )
        .join("");
      return `<div class="group">${rows || '<div class="block tag" style="margin:0">Никого. Добавить можно командой /trust ответом на сообщение или кнопкой «Доверять» в журнале.</div>'}</div>`;
    },
  },
  copy: {
    icon: "copy",
    title: "Перенос настроек",
    hint: "Скопировать правила и настройки из другого чата",
    summary: () => "Из другого вашего чата",
    available: () => state.chats.filter((c) => c.chat_id !== state.chatId).length > 0,
    body: () => copyCard(),
  },
};

function folderRow(id) {
  const f = FOLDERS[id];
  return `<button class="nav-row" data-folder="${id}"><span class="ico">${icon(f.icon)}</span>
    <div class="lbl"><b>${f.title}</b><small>${esc(f.summary(state.data.settings))}</small></div>
    <svg class="i chev" viewBox="0 0 24 24" aria-hidden="true">${ICONS.chevron}</svg></button>`;
}

function planSummary() {
  const b = state.data.billing;
  if (b.state === "expired") return "Срок закончился";
  return `${b.title} · осталось ${b.days_left} ${dayWord(b.days_left)}`;
}

function renderSettings() {
  const ids = ["mode", "spam", "newbies", "raids", "links", "notify"];
  const tail = ["trusted", "copy"].filter((id) => !FOLDERS[id].available || FOLDERS[id].available());
  $("#view").innerHTML = `
    <div class="group first"><button class="nav-row" data-go="pay"><span class="ico">${icon("card")}</span>
      <div class="lbl"><b>Тариф и оплата</b><small>${esc(planSummary())}</small></div>
      <svg class="i chev" viewBox="0 0 24 24" aria-hidden="true">${ICONS.chevron}</svg></button></div>
    <div class="h">Защита</div>
    <div class="group">${ids.map(folderRow).join("")}</div>
    <div class="h">Ещё</div>
    <div class="group">${tail.map(folderRow).join("")}</div>`;
  $("#view").onclick = (e) => {
    const t = e.target;
    if (t.dataset.go === "pay") openScreen("pay");
    else if (t.dataset.folder) openScreen("folder", { id: t.dataset.folder });
  };
}

function renderFolder(id) {
  const f = FOLDERS[id];
  screenShell(f.title, f.hint);
  $("#view").innerHTML = f.body();
  bindSettings($("#view"));
}

function bindSettings(view) {
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
      if (warning) confirmAction(warning, () => saveSetting(key, on), rerender);
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

