# SPDX-License-Identifier: GPL-3.0-only
"""Run only in a private Xvfb/D-Bus session with disposable HOME and XDG dirs."""
import json
import os
from pathlib import Path
import subprocess
import time
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait

ROOT=Path(__file__).resolve().parents[1]
SCREENSHOTS=Path(os.environ.get('PYROBOLIC_SCREENSHOTS',ROOT.parent/'.qa/extensions'))
SCREENSHOTS.mkdir(parents=True,exist_ok=True)
UUID='90c32e0d-a9d6-4934-a42f-921fbd6553cd'
base=f'moz-extension://{UUID}/'
assert os.environ['HOME'].startswith('/tmp/pyrobolic-ui.'), 'Disposable HOME required'
manifest=json.loads((ROOT/'firefox/native-host.json').read_text())
manifest['path']=os.environ.get('PYROBOLIC_NATIVE_HOST',str(ROOT/'firefox/host.py'))
destination=Path.home()/'.mozilla/native-messaging-hosts'/f'{manifest["name"]}.json'
destination.parent.mkdir(parents=True)
destination.write_text(json.dumps(manifest))
options=Options()

options.set_preference('extensions.webextensions.uuids',json.dumps({'pyrobolic@woodpile72':UUID}))
options.set_preference('browser.shell.checkDefaultBrowser',False)
options.set_preference('browser.startup.homepage','about:blank')
options.set_preference('browser.newtabpage.enabled',False)
options.set_preference('datareporting.policy.dataSubmissionEnabled',False)
options.set_preference('toolkit.telemetry.enabled',False)
options.set_preference('browser.uiCustomization.state',json.dumps({'placements':{'nav-bar':['back-button','forward-button','stop-reload-button','urlbar-container','pyrobolic_woodpile72-browser-action']},'currentVersion':20}))
driver=webdriver.Firefox(service=Service(os.environ['GECKODRIVER'],service_args=['--allow-system-access'],log_output=str(ROOT/'tests/firefox-driver.log')),options=options)
wait=WebDriverWait(driver,20)
try:
 if os.environ.get('PYROBOLIC_APP_ROOT'):
  app=subprocess.Popen(['/usr/bin/python3','-m','pyrobolic'],cwd='/tmp',env={**os.environ,'PYTHONPATH':os.environ['PYROBOLIC_APP_ROOT']},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  time.sleep(2)
 driver.set_window_size(1050,800)
 driver.install_addon(os.environ['PYROBOLIC_FIREFOX_XPI'],temporary=True)
 driver.get(base+'popup.html')
 wait.until(lambda d:'ready' in d.find_element(By.ID,'connection').text)
 print('PASS: installed Firefox extension connects to real native bridge',flush=True)
 # Browser UI normally grants activeTab when the action is opened. This tab also
 # lets us test the real popup DOM and local native bridge with an edited URL.
 url=driver.find_element(By.ID,'url');url.send_keys('https://www.youtube.com/watch?v=pyrobolic-preview')
 url.send_keys(Keys.TAB)
 assert driver.execute_script('return document.activeElement.id') == 'send'
 driver.find_element(By.ID,'send').send_keys(Keys.ENTER)
 wait.until(lambda d:'Sent to Pyrobolic' in d.find_element(By.ID,'result').text)
 print('PASS: extension -> native host -> installed Pyrobolic launch',flush=True)
 time.sleep(3)
 windows=subprocess.check_output(['xdotool','search','--onlyvisible','--name','Pyrobolic'],text=True).splitlines()
 assert windows
 # Collect actual desktop state after handoff, including review dialog.
 subprocess.run(['import','-window','root',str(SCREENSHOTS/'firefox-handoff.png')],check=True)
 subprocess.run(['gdbus','call','--session','--dest','io.github.woodpile72.Pyrobolic','--object-path','/io/github/woodpile72/Pyrobolic','--method','org.gtk.Actions.Activate','quit','[]','{}'],check=True)
 time.sleep(1)
 driver.get(base+'popup.html')
 wait.until(lambda d:'ready' in d.find_element(By.ID,'connection').text)
 field=driver.find_element(By.ID,'url');field.send_keys('file:///etc/passwd')
 assert not driver.find_element(By.ID,'send').is_enabled()
 print('PASS: local-file URL blocked in real Firefox popup',flush=True)
 driver.find_element(By.ID,'help').click()
 wait.until(lambda d:len(d.window_handles)>1)
 print('PASS: Setup & Help opens',flush=True)
 # Show actual toolbar popup against a deterministic local web page.
 driver.switch_to.window(driver.window_handles[0])
 import http.server, threading
 class Page(http.server.BaseHTTPRequestHandler):
  def do_GET(self):
   self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
   self.wfile.write(b'<html><head><title>Live at National Bern - Barcelona Big Blues Band</title></head><body><h1>Browser integration test</h1><a href="https://example.org/video.mp4">Video link</a></body></html>')
  def log_message(self,*args):pass
 server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Page)
 threading.Thread(target=server.serve_forever,daemon=True).start()
 driver.get(f'http://127.0.0.1:{server.server_port}/')
 driver.set_context('chrome')
 print(driver.execute_script("return [...document.querySelectorAll('[id]')].map(e=>e.id).filter(id=>id.includes('pyrobolic'));"),flush=True)
 for theme,value in [('light',1),('dark',0)]:
  driver.execute_script('Services.prefs.setIntPref("ui.systemUsesDarkTheme", arguments[0]);',0 if theme=='light' else 1)
  driver.execute_script('Services.prefs.setIntPref("layout.css.prefers-color-scheme.content-override", arguments[0]);',value)
  action=driver.find_element(By.ID,'pyrobolic_woodpile72-browser-action')
  firefox_id=subprocess.check_output(['xdotool','search','--onlyvisible','--name','Live at National Bern.*Mozilla Firefox'],text=True).splitlines()[-1]
  subprocess.run(['xdotool','windowactivate','--sync',firefox_id],check=True)
  point=driver.execute_script('const r=arguments[0].getBoundingClientRect();return [Math.round(window.mozInnerScreenX+r.x+r.width/2),Math.round(window.mozInnerScreenY+r.y+r.height/2)];',action)
  print('Toolbar click',point,flush=True)
  subprocess.run(['xdotool','mousemove',str(point[0]),str(point[1]),'click','1'],check=True)
  time.sleep(1)
  wait.until(lambda d:d.execute_script('return document.getElementById("customizationui-widget-panel")?.state === "open"'))
  assert driver.execute_script('return document.getElementById("customizationui-widget-panel").getBoundingClientRect().width') > 370
  time.sleep(2)
  subprocess.run(['import','-window','root',str(SCREENSHOTS/f'firefox-popup-{theme}.png')],check=True)
  rect=driver.execute_script('const r=document.getElementById("customizationui-widget-panel").getBoundingClientRect();return [Math.round(r.width),Math.round(r.height),Math.round(window.mozInnerScreenX+r.x),Math.round(window.mozInnerScreenY+r.y)];')
  region=f'{rect[0]}x{rect[1]}+{rect[2]}+{rect[3]}'
  subprocess.run(['import','-window','root','-crop',region,str(SCREENSHOTS/f'popup-{theme}.png')],check=True)
  # Escape closes the popup without navigating the page.
  driver.execute_script('document.getElementById("customizationui-widget-panel").hidePopup();')
  time.sleep(.5)
 driver.set_context('content')
 print('PASS: real toolbar popup captured in light and dark themes',flush=True)
 ActionChains(driver).context_click(driver.find_element(By.TAG_NAME,'a')).perform()
 driver.set_context('chrome')
 menu=wait.until(lambda d:d.find_element(By.CSS_SELECTOR,'menuitem[label="Send Link to Pyrobolic"]'))
 point=driver.execute_script('const r=arguments[0].getBoundingClientRect();return [Math.round(window.mozInnerScreenX+r.x+r.width/2),Math.round(window.mozInnerScreenY+r.y+r.height/2)];',menu)
 subprocess.run(['import','-window','root',str(SCREENSHOTS/'firefox-context-menu.png')],check=True)
 subprocess.run(['xdotool','mousemove',str(point[0]),str(point[1]),'click','1'],check=True)
 time.sleep(3)
 assert subprocess.run(['xdotool','search','--onlyvisible','--name','Pyrobolic'],capture_output=True).returncode == 0
 subprocess.run(['import','-window','root',str(SCREENSHOTS/'firefox-link-handoff.png')],check=True)
 subprocess.run(['gdbus','call','--session','--dest','io.github.woodpile72.Pyrobolic','--object-path','/io/github/woodpile72/Pyrobolic','--method','org.gtk.Actions.Activate','quit','[]','{}'],check=True)
 print('PASS: real Firefox right-click link handoff',flush=True)
 # Override any system host too; deleting the user entry could fall back to it.
 missing_manifest=dict(manifest,path='/nonexistent/pyrobolic-native-host')
 destination.write_text(json.dumps(missing_manifest))
 driver.set_context('content')
 driver.get(base+'popup.html')
 wait.until(lambda d:'Setup needed' in d.find_element(By.ID,'connection').text)
 field=driver.find_element(By.ID,'url');field.send_keys('https://example.org/test')
 driver.find_element(By.ID,'send').click()
 wait.until(lambda d:'Install or update the Pyrobolic desktop package' in d.find_element(By.ID,'result').text)
 print('PASS: missing native host displays actionable error',flush=True)
finally:
 driver.quit()
 # Private D-Bus/Xvfb teardown also ends any remaining test windows.
