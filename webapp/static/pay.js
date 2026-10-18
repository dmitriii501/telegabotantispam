"use strict";

// Paying for a plan without leaving the panel: Stars in Telegram's own payment window,
// roubles on the bank's page. The price is chosen here, and checked again on the server.

const EMAIL_RE = /^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$/;
const TERM_LABEL = { 1: "1 месяц", 3: "3 месяца", 12: "12 месяцев" };
const POLL_MS = 2500;
const POLL_TRIES = 96; // about four minutes

const money = (n, method) => (method === "rub" ? `${num(n)} ₽` : `${num(n)} ⭐`);

function payState() {
  if (!state.pay) {
    const b = state.data.billing;
    state.pay = {
      plan: b.state === "paid" && b.plan ? b.plan : "start",
      months: 1,
      method: b.rub_enabled ? "rub" : "stars",
      email: b.email || "",
      phase: "form", // form | waiting | done
      busy: false,
      before: b.state === "paid" ? b.until : 0, // access end before paying: a later one means the payment came through
      timer: null,
      tries: 0,
      timedOut: false,
    };
  }
  return state.pay;
}

const planOf = (p) => state.data.billing.plans.find((x) => x.key === p.plan);
const costOf = (p) => planOf(p).prices[String(p.months)][p.method];

// Until when the access will run after this payment: renewal adds to the time left, a plan change starts today.
function endsAt(p) {
  const b = state.data.billing;
  const now = Math.floor(Date.now() / 1000);
  const base = b.state === "paid" && b.plan === p.plan && b.until > now ? b.until : now;
  return base + p.months * 30 * 86400;
}

function renderPay() {
  const p = payState();
  screenShell("Тариф", "Оплата для этого чата");
  if (p.phase === "waiting") return drawPayWaiting(p);
  if (p.phase === "done") return drawPayDone(p);
  const b = state.data.billing;
  const rub = b.rub_enabled;
  if (!rub && p.method === "rub") p.method = "stars";
  const current =
    b.state === "expired"
      ? `<b>Срок закончился ${dateText(b.until)}</b><small>ИИ-проверка выключена. Работают списки ссылок, антифлуд и защита от очевидного спама.</small>`
      : `<b>${esc(b.title)}</b><small>${b.state === "trial" ? "бесплатно, " : "оплачен, "}осталось ${b.days_left} ${dayWord(b.days_left)} (до ${dateText(b.until)})</small>`;
  const plans = b.plans
    .map((pl) => {
      const per = pl.prices["1"][p.method];
      return `<button class="choice ${p.plan === pl.key ? "on" : ""}" data-plan="${pl.key}">
        <span class="radio"></span>
        <span class="body"><b>${esc(pl.title)}</b>
          <small>до ${num(pl.checks)} комментариев в месяц, около ${num(pl.per_day)} в день</small>
          <small>${esc(pl.pitch)}</small></span>
        <span class="price">${money(per, p.method)}<small>в месяц</small></span></button>`;
    })
    .join("");
  const terms = b.terms
    .map(
      (t) => `<button class="term ${p.months === t.months ? "on" : ""}" data-term="${t.months}">
        <b>${TERM_LABEL[t.months]}</b><span>${money(planOf(p).prices[String(t.months)][p.method], p.method)}</span>
        ${t.discount ? `<i>−${t.discount}%</i>` : ""}</button>`
    )
    .join("");
  const methods = rub
    ? `<div class="seg">${[["rub", "Картой или СБП"], ["stars", "Звёздами ⭐"]]
        .map(([v, label]) => `<button data-method="${v}" class="${p.method === v ? "on" : ""}">${label}</button>`)
        .join("")}</div>`
    : '<div class="tag">Оплата звёздами Telegram.</div>';
  const email =
    p.method === "rub"
      ? `<label class="field"><small>E-mail для кассового чека</small>
          <input id="email" type="email" inputmode="email" autocomplete="email" placeholder="name@mail.ru" value="${esc(p.email)}"></label>`
      : "";
  const fine =
    p.method === "rub"
      ? "Платёж проходит на защищённой странице Т-Банка, картой или через СБП. Чек придёт на почту. После оплаты вернитесь сюда: срок продлится сам."
      : "Платёж проходит в окне Telegram. После оплаты срок продлится сам.";
  $("#view").innerHTML = `
    <div class="card plan ${b.state}">${current}</div>
    <div class="h">Тариф</div><div class="choices">${plans}</div>
    <div class="h">На какой срок</div><div class="terms">${terms}</div>
    <div class="h">Как платить</div>${methods}${email}
    <div class="card summary">
      <div><small>Тариф «${esc(planOf(p).title)}», ${TERM_LABEL[p.months]}</small><span>действует до ${dateText(endsAt(p))}</span></div>
      <b>${money(costOf(p), p.method)}</b></div>
    <div class="tag center">${fine}</div>
    <div class="tag center">Оплачивая, вы принимаете <button class="linkbtn" data-href="/offer">условия оферты</button>.
      Вопросы и возврат: команда /paysupport у бота.</div>`;
  const view = $("#view");
  view.onclick = (e) => {
    const d = e.target.dataset;
    if (d.href) {
      const url = location.origin + d.href;
      if (tg.openLink) tg.openLink(url);
      else window.open(url, "_blank");
      return;
    }
    if (d.plan) p.plan = d.plan;
    else if (d.term) p.months = Number(d.term);
    else if (d.method) p.method = d.method;
    else return;
    haptic("light");
    renderPay();
  };
  view.oninput = (e) => {
    if (e.target.id === "email") p.email = e.target.value.trim();
  };
  setMain(`Оплатить ${money(costOf(p), p.method)}`, payGo);
}

async function payGo() {
  const p = payState();
  if (p.busy || p.phase !== "form") return;
  if (p.method === "rub" && !EMAIL_RE.test(p.email)) {
    toast("Укажите e-mail: на него придёт кассовый чек");
    haptic("error");
    const box = $("#email");
    if (box) box.focus();
    return;
  }
  p.busy = true;
  setBusy(true);
  try {
    const res = await api("POST", `/chat/${state.chatId}/pay`, { method: p.method, plan: p.plan, months: p.months, email: p.email });
    if (res.kind === "invoice") {
      if (tg.openInvoice && atLeast("6.1")) tg.openInvoice(res.url, onInvoice);
      else {
        await api("POST", `/chat/${state.chatId}/pay/chat`); // an old Telegram: the bot sends the invoice to the chat
        toast("Счёт отправил вам в чат с ботом 💳");
      }
    } else {
      if (tg.openLink) tg.openLink(res.url);
      else window.open(res.url, "_blank");
      startPayPolling();
    }
  } catch (e) {
    fail(e);
  } finally {
    p.busy = false;
    setBusy(false);
  }
}

function onInvoice(status) {
  if (status === "paid") startPayPolling();
  else if (status === "failed") toast("Платёж не прошёл. Попробуйте ещё раз или выберите другой способ.");
}

// ------------------------------------------------------- waiting for the confirmation

function startPayPolling() {
  const p = payState();
  p.phase = "waiting";
  p.tries = 0;
  p.timedOut = false;
  clearMain();
  renderPay();
  schedulePoll();
}

function schedulePoll() {
  const p = state.pay;
  if (p) p.timer = setTimeout(pollPay, POLL_MS);
}

function stopPayPolling() {
  if (state.pay) clearTimeout(state.pay.timer);
}

async function pollPay() {
  const p = state.pay;
  if (!p || p.phase !== "waiting" || !state.screen || state.screen.name !== "pay") return;
  p.tries++;
  try {
    const b = (await reloadChat()).billing;
    if (b.state === "paid" && b.until > p.before) {
      p.phase = "done";
      haptic("success");
      renderPay();
      return;
    }
  } catch (_) {
    /* a failed check is retried */
  }
  if (p.tries >= POLL_TRIES) {
    p.timedOut = true;
    renderPay();
    return;
  }
  schedulePoll();
}

function drawPayWaiting(p) {
  $("#view").innerHTML = p.timedOut
    ? `<div class="state-box">${`<div class="bubble">${icon("clock")}</div>`}<b>Оплата ещё не подтверждена</b>
        <div class="tag">Если деньги списались, срок продлится сам в течение нескольких минут. Можно закрыть панель.</div>
        <button class="btn primary wide" data-act="recheck">Проверить снова</button>
        <button class="btn ghost wide" data-act="finish">Закрыть</button></div>`
    : `<div class="state-box"><div class="spinner" aria-hidden="true"></div><b>Ждём подтверждение платежа</b>
        <div class="tag">Обычно это несколько секунд. Если вы платили на странице банка, вернитесь сюда после оплаты.</div>
        <button class="btn ghost wide" data-act="back">Изменить выбор</button></div>`;
  $("#view").onclick = (e) => {
    const act = e.target.dataset.act;
    if (act === "recheck") {
      p.timedOut = false;
      p.tries = 0;
      renderPay();
      pollPay();
    } else if (act === "finish") closeScreen();
    else if (act === "back") {
      stopPayPolling();
      p.phase = "form";
      renderPay();
    }
  };
}

function drawPayDone() {
  const b = state.data.billing;
  clearMain();
  $("#view").innerHTML = `<div class="state-box"><div class="bubble ok">${icon("check")}</div><b>Оплачено</b>
    <div>Тариф «${esc(b.title)}» действует до ${dateText(b.until)}.</div>
    <div class="tag">Правила и настройки на месте, ИИ-проверка работает.</div>
    <button class="btn primary wide" data-act="finish">Готово</button></div>`;
  $("#view").onclick = (e) => {
    if (e.target.dataset.act === "finish") closeScreen();
  };
}

// Back from the bank's page: check at once instead of waiting for the next tick.
document.addEventListener("visibilitychange", () => {
  const p = state.pay;
  if (!document.hidden && p && p.phase === "waiting" && !p.timedOut) {
    clearTimeout(p.timer);
    pollPay();
  }
});
