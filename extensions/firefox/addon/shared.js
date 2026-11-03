/* SPDX-License-Identifier: GPL-3.0-only */
"use strict";
globalThis.Pyrobolic = {
  host: "io.github.woodpile72.pyrobolic",
  validateURL(value) {
    if (typeof value !== "string" || !value || value.length > 8192 || /[\s\u0000-\u001f\u007f]/u.test(value)) {
      throw new Error("Choose an HTTP or HTTPS page or link.");
    }
    let url;
    try { url = new URL(value); } catch { throw new Error("Choose a valid web address."); }
    if (!/^https?:\/\//i.test(value) || !["http:", "https:"].includes(url.protocol) || !url.hostname || url.username || url.password || value.includes("\\")) {
      throw new Error("Choose an HTTP or HTTPS address without embedded login details.");
    }
    return value;
  },
  async request(message) {
    let reply;
    try { reply = await browser.runtime.sendNativeMessage(this.host, message); }
    catch { throw new Error("Cannot reach Pyrobolic. Install or update the Pyrobolic desktop package, then try again. See Setup & Help."); }
    if (!reply || reply.ok !== true) throw new Error(reply?.error || "Pyrobolic could not accept this request.");
    return reply;
  }
};
