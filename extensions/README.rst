Pyrobolic browser extensions
============================

Browser integration ships in the main Pyrobolic Debian package. Firefox is the
first supported browser. The core application does not import extension code.
Copyright 2026 Erick Woods. GPL-3.0-only; see the repository LICENSE.

Layout for contributors
-----------------------

* ``firefox/addon/``: Firefox WebExtension source, interface and icons.
* ``firefox/host.py``: Firefox's native-message caller validation.
* ``firefox/native-host.json``: Firefox registration and allowed extension ID.
* ``native/bridge.py``: bounded JSON framing, HTTP(S) validation and fixed-executable
  handoff shared by browser adapters. It imports no desktop application modules.
* ``build.py``: deterministic submission XPI builder and Debian staging hook.
* ``tests/``: protocol, routing and isolated Firefox integration checks.

Additional browsers belong in sibling directories such as ``chromium/`` with their
own manifest, caller validation and registration. Reuse the common bridge protocol;
do not add browser-specific branches to the download queue or GTK application.
No Chromium implementation is provided yet. Native messaging APIs and caller
identities differ between browsers and require dedicated tests.

Installation and use
--------------------

Install Pyrobolic 1.2.0 or newer. The main package installs the bridge and native
Firefox registration automatically. Firefox itself remains optional.

Install the signed XPI from the matching GitHub release, or select
``/usr/share/pyrobolic/extensions/firefox/pyrobolic-firefox.xpi`` using Firefox's
Add-ons Manager / Install Add-on From File. Firefox still asks the user to approve
extension installation. The Debian installer does not modify profiles or browser
policy, and does not force-install the extension.

For private windows, open ``about:addons``, select Pyrobolic, and set Run in Private
Windows to Allow. This permission belongs to the user. Pyrobolic's normal download
history still applies to submitted downloads.

Click the toolbar action or right-click a page, link, video or audio element. The
URL opens in Pyrobolic's review dialog, with no automatic download. Native Linux
Firefox 140+ is the target; Firefox 153 was tested. Snap/Flatpak Firefox, other
browsers and real Wayland/screen-reader acceptance remain unverified.

Distribution and updates
------------------------

Mozilla signing is UNLISTED / self-distributed (choose "On your own"). No public
addons.mozilla.org listing is requested. Distribute the signed XPI through GitHub
and in the main Debian package. Automatic extension updates are not configured in
this initial release. Installing a newer deb replaces its bundled XPI, but users
must install that signed XPI in Firefox to update the copy in their profile.

A development build without a signed input includes
``pyrobolic-firefox-unsigned.xpi`` instead. Load it
through ``about:debugging`` / Load Temporary Add-on; it lasts until Firefox exits.
Release builds fail until a signed XPI matching the extension source is supplied.
The build validates payload identity and signature metadata; a normal, non-temporary
Firefox installation must verify Mozilla's signature before publishing.

The old ``pyrobolic-external-plugins`` 0.1.x preview is superseded. APT removes it
when installing the combined package. The extension ID and native host name stay
the same, preserving an existing browser installation's identity. If a temporary
extension was loaded from the old package directory, reload it from the new XPI.
Removing Pyrobolic removes the bridge; it does not erase Firefox profiles or media.

Privacy and protocol
--------------------

The extension requests activeTab, menus and nativeMessaging only. It sends explicitly
selected URLs to the local bridge, without cookies, clipboard access, URL logging,
analytics or browsing-history scanning. The core app's network and authentication
settings apply after handoff. Browser caller allowlists do not isolate against other
processes already running as the same operating-system user.

Messages: {"action":"ping"} or {"action":"open","url":"https://..."}.
Responses: {"ok":true,"version":"1.0.0","status":"ready"|"launch-requested"}
or {"ok":false,"error":"..."}. A launch acknowledgement is not download completion.
Each process handles one message. Inputs are bounded to 64 KiB; URLs to 8192 characters.

Signing and verification
------------------------

Run ``python3 extensions/build.py /absolute/path/pyrobolic-firefox-1.0.0-unsigned.xpi``.
Submit that file to Mozilla's Developer Hub using "On your own", targeting Linux.
Provide ``firefox/REVIEWER-NOTES.txt`` and the main package/source when requested.
Download Mozilla's signed XPI as ``extensions/firefox/signed.xpi``, add that path to
the source list in ``release-files.json``, then test normal installation and updates.
Never edit a signed XPI. Any source change requires a new extension version/signature.

Tests:
``python3 -m unittest discover -s extensions/tests -p 'test_*.py' -v``
``node --test extensions/tests/test_extension.cjs``
``web-ext lint --source-dir extensions/firefox/addon``

Firefox UI tests require Selenium, Xvfb, Metacity, xdotool, ImageMagick and geckodriver.
Set GECKODRIVER and PYROBOLIC_FIREFOX_XPI to absolute paths, then run
``bash extensions/tests/run-isolated-ui.sh wm /path/to/venv/bin/python extensions/tests/firefox_smoke.py``.
Set PYROBOLIC_NATIVE_HOST to test an extracted package's adapter. Tests use a fresh
HOME and session bus; never run automation against the user's desktop/profile.

Set PYROBOLIC_SIGNED_INSTALL=1 to exercise permanent installation in the smoke test.
Run ``extensions/tests/firefox_signed.py`` through the same isolated wrapper to
verify signature enforcement and native connectivity across a Firefox restart.
