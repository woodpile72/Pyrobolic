# SPDX-License-Identifier: GPL-3.0-only
"""Verify Mozilla signing and persistence in a disposable Firefox profile."""
import json
import os
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
assert os.environ['HOME'].startswith('/tmp/pyrobolic-ui.'), 'Disposable HOME required'
profile = Path.home() / 'signed-profile'
profile.mkdir()
manifest = json.loads((ROOT / 'firefox/native-host.json').read_text())
manifest['path'] = os.environ['PYROBOLIC_NATIVE_HOST']
host = Path.home() / '.mozilla/native-messaging-hosts' / (manifest['name'] + '.json')
host.parent.mkdir(parents=True)
host.write_text(json.dumps(manifest))
for restart in (False, True):
    options = Options()
    options.add_argument('-profile')
    options.add_argument(str(profile))
    options.set_preference('browser.shell.checkDefaultBrowser', False)
    options.set_preference('datareporting.policy.dataSubmissionEnabled', False)
    driver = webdriver.Firefox(service=Service(os.environ['GECKODRIVER'], service_args=['--allow-system-access'], log_output=str(profile / 'driver.log')), options=options)
    try:
        if not restart:
            driver.install_addon(os.environ['PYROBOLIC_FIREFOX_XPI'], temporary=False)
        driver.set_context('chrome')
        state = driver.execute_async_script('''
            const done = arguments[arguments.length - 1];
            const {AddonManager} = ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
            AddonManager.getAddonByID('pyrobolic@woodpile72').then(a => done(a && {
                signed: a.signedState, temporary: a.temporarilyInstalled,
                active: a.isActive, version: a.version,
                required: Services.prefs.getBoolPref('xpinstall.signatures.required', true),
                uuid: JSON.parse(Services.prefs.getStringPref('extensions.webextensions.uuids'))['pyrobolic@woodpile72']
            }));
        ''')
        assert state and state['signed'] > 0 and state['required'] and state['active'] and not state['temporary'], state
        assert state['version'] == '1.0.0', state
        driver.set_context('content')
        driver.get('moz-extension://' + state['uuid'] + '/popup.html')
        WebDriverWait(driver, 20).until(lambda d: 'ready' in d.find_element(By.ID, 'connection').text)
        print('PASS: signed permanent extension and native connection ' + ('after Firefox restart' if restart else 'on initial install'), flush=True)
    finally:
        driver.quit()
