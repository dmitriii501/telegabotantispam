"use strict";

// The home page: what needs you now, the numbers of the day, the latest actions, the plan.

const needsAttention = (b) => b.state === "expired" || b.days_left <= 3 || (b.limit > 0 && b.used / b.limit >= 0.9);

function planRow(b, loud) {
  const expired = b.state === "expired";
  const title = expired ? "Срок закончился" : b.state === "trial" ? "Пробный период" : `Тариф «${esc(b.title)}»`;
  const sub = expired
    ? "ИИ-проверка выключена, остальная защита работает"
    : `осталось ${b.days_left} ${dayWord(b.days_left)} · до ${dateText(b.until)}`;
  const pct = b.limit ? Math.min(100, Math.round((b.used / b.limit) * 100)) : 100;
  const usage = expired
    ? ""
    : `<div class="mini-track"><i style="width:${pct}%" class="${pct >= 90 ? "hot" : ""}"></i></div>
       <small>ИИ прочитал ${num(b.used)} из ${num(b.limit)} комментариев за 30 дней</small>`;
  return `<button class="planrow ${loud ? "loud" : ""} ${b.state}" data-go="pay">
    <div class="lbl"><b>${title}</b><small>${esc(sub)}</small>${usage}</div>
    ${loud ? `<span class="cta">${expired || b.state === "trial" ? "Выбрать тариф" : "Продлить"}</span>` : `<svg class="i chev" viewBox="0 0 24 24" aria-hidden="true">${ICONS.chevron}</svg>`}
  </button>`;
}

function miniRow(e) {
  const [label, tone, ico] = ACTION_PILL[e.action] || [e.action, "", "info"];
  return `<button class="mini" data-open-log>${avatar(e.user_name || "Без имени", e.user_id, "xs")}
    <div class="lbl"><b>${esc(e.user_name || "Без имени")}</b><small>${esc(e.text)}</small></div>
    <span class="pill ${tone}">${icon(ico)}${esc(label)}</span></button>`;
}

function homeLogHtml() {
  const { entries } = state.log;
  const waiting = entries.filter(isWaiting);
  const rest = entries.filter((e) => !isWaiting(e));
  const shown = waiting.slice(0, 3);
  let html = "";
  if (shown.length) {
    html += `<div class="h">Ждут вашего решения <span class="count">${waiting.length}</span></div>` + shown.map(entryCard).join("");
    if (waiting.length > shown.length)
      html += `<button class="btn soft wide" data-open-log>Ещё ${waiting.length - shown.length} в журнале</button>`;
  }
  html += `<div class="h">Последние действия</div>`;
  html += rest.length
    ? `<div class="group">${rest.slice(0, 4).map(miniRow).join("")}</div><button class="btn ghost wide" data-open-log>Весь журнал</button>`
    : emptyState("shieldCheck", "Тихо", "Пока бот ничего не удалял. Когда появятся нарушения, они будут здесь.");
  return html;
}

async function renderHome() {
  const b = state.data.billing;
  const st = state.data.stats || { checked: 0, deleted: 0, pending: 0 };
  const loud = needsAttention(b);
  $("#view").innerHTML = `
    ${loud ? planRow(b, true) : ""}
    <div class="kpis"><div class="cap">За сутки</div>
      <div class="kpi"><b>${num(st.checked)}</b><span>проверено</span></div>
      <div class="kpi"><b>${st.deleted}</b><span>${state.data.settings.observe ? "удалил бы" : "удалено"}</span></div>
      <div class="kpi ${st.pending ? "alert" : ""}"><b>${st.pending}</b><span>ждут вас</span></div></div>
    <div id="homeLog">${skeleton()}</div>
    ${loud ? "" : `<div class="h">Тариф</div>${planRow(b, false)}`}`;
  $("#view").onclick = (e) => {
    if (e.target.dataset.go === "pay") openScreen("pay");
    else if (e.target.dataset.openLog !== undefined) openScreen("log");
    else handleLogClick(e, drawHomeLog);
  };
  try {
    await loadLog();
  } catch (e) {
    if (state.tab === "home" && !state.screen) $("#homeLog").innerHTML = emptyState("alert", "", esc(e.message));
    return;
  }
  drawHomeLog();
}

function drawHomeLog() {
  const box = $("#homeLog");
  if (state.tab !== "home" || state.screen || !box) return;
  box.innerHTML = homeLogHtml();
  if (state.data.stats) state.data.stats.pending = state.log.entries.filter(isWaiting).length;
  renderHead();
}
