/* SPDX-License-Identifier: GPL-3.0-only */
"use strict";
const input = document.getElementById("url");
const button = document.getElementById("send");
const result = document.getElementById("result");
let busy = false;
function update() { try { Pyrobolic.validateURL(input.value.trim()); button.disabled = busy; } catch { button.disabled = true; } }
function showResult(value) {
  if (!value) return;
  result.hidden = false;
  result.textContent = value.ok ? value.message : value.error;
  result.className = value.ok ? "success" : "error";
}
input.addEventListener("input", () => { result.hidden = true; update(); });
document.getElementById("help").addEventListener("click", () => browser.runtime.openOptionsPage());
document.getElementById("send-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy) return;
  busy = true; update();
  try { showResult(await browser.runtime.sendMessage({action: "send", url: input.value.trim()})); }
  catch { showResult({ok: false, error: "Could not send this link. Close and reopen the extension to try again."}); }
  finally { busy = false; update(); }
});
async function initialize() {
  try {
    const [tab] = await browser.tabs.query({active: true, currentWindow: true});
    document.getElementById("page-title").textContent = tab?.title || "Send a link";
    try {
      input.value = Pyrobolic.validateURL(tab?.url);
      document.getElementById("page-host").textContent = new URL(input.value).hostname;
    } catch { document.getElementById("page-host").textContent = "Open a website or paste a web address below."; }
  } catch { document.getElementById("page-title").textContent = "Paste a link to get started"; }
  update();
  try { showResult(await browser.runtime.sendMessage({action: "last-result"})); } catch { /* No previous action. */ }
  const connection = document.getElementById("connection");
  try {
    const reply = await Pyrobolic.request({action: "ping"});
    connection.textContent = "● Pyrobolic ready";
    connection.className = "connected";
    connection.title = `Bridge ${reply.version}`;
  } catch {
    connection.textContent = "● Setup needed";
    connection.className = "disconnected";
  }
}
void initialize();
