"use strict";

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

