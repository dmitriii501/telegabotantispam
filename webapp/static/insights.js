"use strict";

// -------------------------------------------------------------------- insights

function messageLink(messageId) {
  return `https://t.me/c/${String(state.chatId).replace("-100", "")}/${messageId}`;
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

