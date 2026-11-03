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
 driver.install_addon(os.environ['PYROBOLIC_FIREFOX_XPI'],temporary=True)
 driver.set_context('chrome')
 result=driver.execute_async_script("""const done=arguments[0];
 (async()=>{
 const {ExtensionPermissions}=ChromeUtils.importESModule('resource://gre/modules/ExtensionPermissions.sys.mjs');
 const {AddonManager}=ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
 await ExtensionPermissions.add('pyrobolic@woodpile72',{permissions:['internal:privateBrowsingAllowed'],origins:[]});
 const addon=await AddonManager.getAddonByID('pyrobolic@woodpile72');
 await addon.reload();
 return 'allowed';
 })().then(done,e=>done(String(e)));""")
 assert result=='allowed',result
 old=set(driver.window_handles)
 driver.execute_script('OpenBrowserWindow({private:true});')
 wait.until(lambda d:len(d.window_handles)>len(old))
 driver.switch_to.window(next(h for h in driver.window_handles if h not in old))
 assert driver.execute_script('return PrivateBrowsingUtils.isWindowPrivate(window)')
 driver.set_context('content')
 driver.get(base+'popup.html')
 wait.until(lambda d:'ready' in d.find_element(By.ID,'connection').text)
 allowed=driver.execute_async_script('browser.extension.isAllowedIncognitoAccess().then(arguments[0]);')
 assert allowed
 driver.find_element(By.ID,'url').send_keys('https://example.org/private-browser-check')
 driver.find_element(By.ID,'send').click()
 wait.until(lambda d:'Sent to Pyrobolic' in d.find_element(By.ID,'result').text)
 time.sleep(2)
 assert subprocess.run(['xdotool','search','--onlyvisible','--name','Pyrobolic'],capture_output=True).returncode==0
 subprocess.run(['import','-window','root',str(SCREENSHOTS/'private-window-handoff.png')],check=True)
 print('PASS: private permission enabled in isolated profile; private-window native handoff reached Pyrobolic',flush=True)
 subprocess.run(['gdbus','call','--session','--dest','io.github.woodpile72.Pyrobolic','--object-path','/io/github/woodpile72/Pyrobolic','--method','org.gtk.Actions.Activate','quit','[]','{}'],check=True)
finally:
 driver.quit()
