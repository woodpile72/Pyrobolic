# SPDX-License-Identifier: GPL-3.0-only
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge", ROOT / "native/bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def encode(value):
    payload = json.dumps(value).encode()
    return struct.pack("=I", len(payload)) + payload


class BridgeTests(unittest.TestCase):
    def test_valid_urls_preserved(self):
        for url in ['https://www.youtube.com/watch?v=abc&list=def', 'https://example.org/日本語?q=%20', "https://example.org/$(touch%20/tmp/nope)", 'http://[::1]:8080/']:
            with self.subTest(url=url):self.assertEqual(bridge.validate_url(url), url)

    def test_reject_invalid_urls(self):
        for url in [None, 4, {}, '', 'file:///etc/passwd', '--help', 'javascript:alert(1)', 'https://', 'https://user:pass@example.org/', 'https://example.org/a\nb', 'https://example.org:99999/', 'http://[::1', 'https://example.org/a b', 'https://example.org/\\x', 'https://example.org/'+('a'*8192)]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):bridge.validate_url(url)

    def test_framing(self):
        value = {'action': 'open', 'url': 'https://example.org/日本語'}
        buf = io.BytesIO();bridge.write_message(buf,value);buf.seek(0)
        self.assertEqual(bridge.read_message(buf),value)

    def test_bad_frames(self):
        for raw in [b'', b'\x01', struct.pack('=I',0), struct.pack('=I',65537), struct.pack('=I',8)+b'{}', struct.pack('=I',1)+b'\xff', struct.pack('=I',1)+b'{']:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):bridge.read_message(io.BytesIO(raw))

    def test_schema_and_missing_app(self):
        for message in [[],None,{'action':'delete'},{'action':'open'}, {'action':'ping','url':'https://example.org'}]:
            with self.subTest(message=message):
                with self.assertRaises(ValueError):bridge.handle(message)
        with patch.object(bridge,'LAUNCHER',Path('/no/such/pyrobolic')):
            with self.assertRaisesRegex(ValueError,'Install Pyrobolic'):bridge.handle({'action':'ping'})

    def test_ping_never_launches(self):
        with patch.object(bridge,'LAUNCHER',Path('/usr/bin/python3')), patch.object(bridge,'launch') as launch:
            self.assertEqual(bridge.handle({'action':'ping'})['status'],'ready');launch.assert_not_called()

    def test_open_calls_once(self):
        with patch.object(bridge,'LAUNCHER',Path('/usr/bin/python3')),patch.object(bridge,'launch') as launch:
            self.assertTrue(bridge.handle({'action':'open','url':'https://example.org/?x=1&y=2'})['ok'])
            launch.assert_called_once_with('https://example.org/?x=1&y=2')

    def test_launch_fixed_argv_and_detached_stdio(self):
        child=Mock();child.wait.side_effect=subprocess.TimeoutExpired('pyrobolic',.25)
        with patch.object(bridge.subprocess,'Popen',return_value=child) as popen:
            bridge.launch('https://example.org/?x=$(whoami)')
            args,kwargs=popen.call_args
            self.assertEqual(args[0],['/usr/bin/pyrobolic','--','https://example.org/?x=$(whoami)'])
            self.assertTrue(kwargs['start_new_session']);self.assertEqual(kwargs['stdout'],subprocess.DEVNULL)
            self.assertNotIn('shell',kwargs)

    def test_launch_failures(self):
        child=Mock();child.wait.return_value=1
        with patch.object(bridge.subprocess,'Popen',return_value=child):
            with self.assertRaises(ValueError):bridge.launch('https://example.org')
        with patch.object(bridge.subprocess,'Popen',side_effect=OSError):
            with self.assertRaises(ValueError):bridge.launch('https://example.org')

    def test_process_protocol_and_origin(self):
        for caller,expected in [("pyrobolic@woodpile72",True),('wrong@example.org',False)]:
            proc=subprocess.run([sys.executable,str(ROOT/'firefox/host.py'),'/test/manifest.json',caller],input=encode({'action':'ping'}),capture_output=True,timeout=3)
            self.assertEqual(proc.returncode,0);self.assertEqual(proc.stderr,b'')
            self.assertEqual(bridge.read_message(io.BytesIO(proc.stdout))['ok'],expected)


if __name__=='__main__':unittest.main()
