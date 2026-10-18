"use strict";

// Start: the Telegram theme, the buttons, the first request.

function applyTheme() {
  // Follow the Telegram theme, not the phone's: someone can use dark Telegram on a light phone.
  if (tg && tg.colorScheme) document.documentElement.dataset.theme = tg.colorScheme;
  try {
    // Telegram's own bars take the color of the paper, so the page has no visible edge.
    const paper = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    if (atLeast("6.1")) tg.setBackgroundColor(paper);
    if (atLeast("6.9")) tg.setHeaderColor(paper);
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
  $("#save").onclick = runMain;
  if (nativeMain()) tg.MainButton.onClick(runMain);
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
