// SPDX-License-Identifier: GPL-3.0-only
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '..', 'firefox', 'addon');
function setup(fail=false) {
  const events = {}, sent = [], menus = [], badges=[];
  const on = key => ({addListener:fn=>events[key]=fn});
  const browser = {
    runtime: {id:'pyrobolic@woodpile72',getURL:x=>'moz-extension://test/'+x,
      onInstalled:on('install'),onStartup:on('startup'),onMessage:on('message'),
      sendNativeMessage:async(host,message)=>{sent.push({host,message});if(fail)throw Error('absent');return {ok:true};}},
    menus:{removeAll:async()=>menus.splice(0),create:m=>menus.push(m),onClicked:on('click')},
    action:{setBadgeText:async x=>badges.push(x.text),setBadgeBackgroundColor:async()=>{},setTitle:async()=>{}}
  };
  const context=vm.createContext({browser,URL});
  for(const f of ['shared.js','background.js'])vm.runInContext(fs.readFileSync(path.join(root,f),'utf8'),context);
  return {events,sent,menus,badges,context};
}
const sender={id:'pyrobolic@woodpile72',url:'moz-extension://test/popup.html'};
const tick=()=>new Promise(r=>setImmediate(r));
test('menus register without duplicate startup entries',async()=>{const s=setup();await s.events.install();await s.events.startup();assert.equal(s.menus.length,3);});
test('popup sends exactly one URL with fixed host',async()=>{const s=setup();const r=await s.events.message({action:'send',url:'https://example.org/watch?v=1'},sender);assert.equal(r.ok,true);assert.equal(s.sent.length,1);assert.equal(s.sent[0].host,'io.github.woodpile72.pyrobolic');});
test('external and non-popup senders cannot invoke bridge',()=>{const s=setup();assert.equal(s.events.message({action:'send',url:'https://example.org'},{}),undefined);assert.equal(s.sent.length,0);});
test('link, page, media and blob fallback route correctly',async()=>{const s=setup();for(const [info,want] of [[{menuItemId:'pyrobolic-link',linkUrl:'https://example.org/link'},'https://example.org/link'],[{menuItemId:'pyrobolic-page',pageUrl:'https://example.org/page'},'https://example.org/page'],[{menuItemId:'pyrobolic-media',srcUrl:'https://example.org/media.mp4'},'https://example.org/media.mp4'],[{menuItemId:'pyrobolic-media',srcUrl:'blob:abc',frameUrl:'https://example.org/frame'},'https://example.org/frame']]){s.events.click(info,{});await tick();assert.equal(s.sent.at(-1).message.url,want);}});
test('unsafe URLs never reach native host',async()=>{const s=setup();for(const url of ['file:///etc/passwd','javascript:alert(1)','https://u:p@example.org','https://example.org/\na','https://example.org/\\x','--help']){const r=await s.events.message({action:'send',url},sender);assert.equal(r.ok,false);}assert.equal(s.sent.length,0);});
test('missing host reports error and badge',async()=>{const s=setup(true);const r=await s.events.message({action:'send',url:'https://example.org'},sender);assert.equal(r.ok,false);assert.match(r.error,/Install or update the Pyrobolic desktop package/);assert.equal(s.badges.at(-1),'!');});
test('manifest has no broad site or cookie access',()=>{const m=JSON.parse(fs.readFileSync(path.join(root,'manifest.json')));assert.deepEqual(m.permissions,['activeTab','menus','nativeMessaging']);assert.equal(m.content_scripts,undefined);assert.equal(m.host_permissions,undefined);});
