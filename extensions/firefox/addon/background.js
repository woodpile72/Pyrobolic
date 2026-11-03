/* SPDX-License-Identifier: GPL-3.0-only */
"use strict";
let lastResult = null;
let sending = false;
const menuItems = [
  {id: "pyrobolic-link", title: "Send Link to Pyrobolic", contexts: ["link"], targetUrlPatterns: ["http://*/*", "https://*/*"]},
  {id: "pyrobolic-page", title: "Send Page to Pyrobolic", contexts: ["page"], documentUrlPatterns: ["http://*/*", "https://*/*"]},
  {id: "pyrobolic-media", title: "Send Media to Pyrobolic", contexts: ["video", "audio"]}
];
async function setupMenus() {
  await browser.menus.removeAll();
  for (const item of menuItems) browser.menus.create(item);
}
browser.runtime.onInstalled.addListener(setupMenus);
browser.runtime.onStartup.addListener(setupMenus);
async function send(url) {
  if (sending) return {ok: false, error: "A request is already being sent. Try again in a moment."};
  sending = true;
  try {
    await Pyrobolic.request({action: "open", url: Pyrobolic.validateURL(url)});
    lastResult = {ok: true, message: "Sent to Pyrobolic. Review your download in the app."};
    await browser.action.setBadgeText({text: ""});
    await browser.action.setTitle({title: "Send to Pyrobolic"});
  } catch (error) {
    lastResult = {ok: false, error: error.message};
    await browser.action.setBadgeBackgroundColor({color: "#a51d2d"});
    await browser.action.setBadgeText({text: "!"});
    await browser.action.setTitle({title: "Pyrobolic needs attention — open for details"});
  } finally { sending = false; }
  return lastResult;
}
browser.menus.onClicked.addListener((info, tab) => {
  let url = info.menuItemId === "pyrobolic-link" ? info.linkUrl : info.menuItemId === "pyrobolic-media" ? info.srcUrl : info.pageUrl || tab?.url;
  // blob/data sources are browser-local. Let yt-dlp inspect their containing page instead.
  if (info.menuItemId === "pyrobolic-media" && !/^https?:\/\//i.test(url || "")) url = info.frameUrl || info.pageUrl || tab?.url;
  if (menuItems.some(item => item.id === info.menuItemId)) void send(url);
});
browser.runtime.onMessage.addListener((message, sender) => {
  if (sender.id !== browser.runtime.id || sender.url !== browser.runtime.getURL("popup.html")) return undefined;
  if (message.action === "send") return send(message.url);
  if (message.action === "last-result") return Promise.resolve(lastResult?.ok === false ? lastResult : null);
  return undefined;
});
