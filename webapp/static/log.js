"use strict";

// The log of what the bot did. Its pieces are used on the home page (decisions waiting for you, the latest actions)
// and on the full page opened from there.

async function loadLog(force) {
  if (!state.log || force) state.log = await api("GET", `/chat/${state.chatId}/log`);
  return state.log;
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


// Buttons on log cards: delete or keep, restore, trust. `after` redraws the page that shows them.
async function handleLogClick(e, after) {
  {
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
        toast("Больше не проверяю этого автора ⭐");
      } else return;
      haptic("success");
      state.log = null;
      await loadLog(true);
      await reloadChat();
      after();
    } catch (err) {
      toast(err.message);
    }
  }
}

const bindLogActions = (view, after) => (view.onclick = (e) => handleLogClick(e, after));

const isWaiting = (e) => e.action === "send_to_review" && !e.feedback;

async function renderLogScreen() {
  screenShell("Журнал", "Что бот делал в этом чате");
  $("#view").innerHTML = skeleton();
  try {
    await loadLog(true);
  } catch (e) {
    $("#view").innerHTML = emptyState("alert", "", esc(e.message));
    return;
  }
  if (!state.screen || state.screen.name !== "log") return;
  drawLogScreen();
}

function drawLogScreen() {
  const { entries } = state.log;
  const waiting = entries.filter(isWaiting);
  const rest = entries.filter((e) => !isWaiting(e));
  $("#view").innerHTML = `
    ${waiting.length ? `<div class="h">Ждут вашего решения <span class="count">${waiting.length}</span></div>` + waiting.map(entryCard).join("") : ""}
    <div class="h">Последние действия</div>
    ${rest.map(entryCard).join("") || emptyState("shieldCheck", "Тихо", "Пока бот ничего не удалял.")}`;
  bindLogActions($("#view"), drawLogScreen);
}
