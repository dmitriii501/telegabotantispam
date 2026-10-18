"use strict";
// Old buttons in chats open the site root inside Telegram: send them on to the panel, keeping what Telegram added.
if (location.pathname === "/" && /tgWebApp(Data|Version)/.test(location.hash + location.search)) {
  location.replace("/app/" + location.search + location.hash);
}
