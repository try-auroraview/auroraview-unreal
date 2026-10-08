"""Integrity and teardown failures must not become a successful demonstration."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_demo


class DemoLaunchGuards(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def bundle(self):
        bundle = self.root / 'Bundle'
        exe = bundle / 'Game/Windows/Demo/Binaries/Win64/Demo.exe'
        exe.parent.mkdir(parents=True)
        data = bytearray(256)
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 60, 128)
        data[128:132] = b'PE\0\0'
        struct.pack_into('<H', data, 132, 0x8664)
        struct.pack_into('<H', data, 150, 0x22)
        struct.pack_into('<H', data, 152, 0x20B)
        exe.write_bytes(data)
        html = bundle / 'Resources/live_demo.html'
        html.parent.mkdir()
        html.write_text('<h1>Actual demo input</h1>', encoding='utf-8')
        manifest = dict(schema_version=1, configuration='Development', engine_version='5.7',
                        executable=exe.relative_to(bundle).as_posix(),
                        files_sha256=run_demo.validate_game.inventory(bundle))
        (bundle / 'demo-package.json').write_text(json.dumps(manifest), encoding='utf-8')
        return bundle

    def test_intact_relocated_bundle_has_a_bound_native_executable(self):
        bundle = self.bundle()
        prepared, html = run_demo.bundle_inputs(bundle)
        self.assertEqual(prepared['engine_version'], '5.7')
        self.assertTrue(Path(prepared['executable']).is_file())
        self.assertEqual(html, bundle / 'Resources/live_demo.html')

    def test_unrecorded_native_library_rejects_bundle(self):
        bundle = self.bundle()
        (bundle / 'Game/Windows/Demo/Binaries/Win64/Unrecorded.dll').write_bytes(b'not accepted')
        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'inventory differs'):
            run_demo.bundle_inputs(bundle)

    def test_changed_html_rejects_bundle(self):
        bundle = self.bundle()
        (bundle / 'Resources/live_demo.html').write_text('<h1>Changed</h1>', encoding='utf-8')
        with self.assertRaises(run_demo.build_plugin.BuildError):
            run_demo.bundle_inputs(bundle)

    def test_output_cannot_overlap_protected_inputs_or_replace_existing(self):
        protected = self.root / 'Source'
        protected.mkdir()
        for output in [protected, protected / 'Run', self.root]:
            with self.assertRaises(run_demo.build_plugin.BuildError):
                run_demo.isolated_output(output, [protected])
        output = self.root / 'Existing'
        output.mkdir()
        with self.assertRaises(FileExistsError):
            run_demo.isolated_output(output, [protected])

    def test_client_close_failure_still_waits_redacts_and_finalizes(self):
        output = self.root / 'Run'
        output.mkdir()
        html = self.root / 'demo.html'
        html.write_text('<h1>Demo</h1>', encoding='utf-8')
        token = 'private-token-for-test-' * 3

        class Process:
            pid, returncode, running, waited = 123, 0, True, False
            def poll(self):
                return None if self.running else self.returncode
            def wait(self, timeout):
                self.waited = True
                return self.returncode
        process = Process()

        class Client:
            identity = {'pid': 123, 'context': 'game', 'engine_version': '5.7.4'}
            def __init__(self, *_args, **_kwargs):
                pass
            def call(self, method, *_args):
                if method == 'unreal.engine.info':
                    return {'engine_ready': True}
                if method == 'auroraview.view.open':
                    process.running = False
                    return True
                raise AssertionError(method)
            def close(self):
                raise TimeoutError('User callback did not finish')

        class Tools:
            def __init__(self, *_args):
                self.browser_ready = threading.Event()
                self.browser_ready.set()
            def register(self):
                pass
            def close(self):
                pass

        def popen(_command, **kwargs):
            kwargs['stdout'].write(token.encode())
            return process

        with patch.object(run_demo, 'Client', Client), patch.object(run_demo.subprocess, 'Popen', popen), \
                patch.object(run_demo.demo_tools, 'DemoTools', Tools), \
                patch.object(run_demo.demo_tools, 'find_scene', return_value=('world', 'scene')), \
                patch.object(run_demo.secrets, 'token_urlsafe', return_value=token):
            result = run_demo.launch(dict(mode='game', executable=str(self.root / 'Game.exe'), engine_version='5.7'),
                                     html, output, 0)
        self.assertTrue(process.waited)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['forced_cleanup'])
        self.assertIn('Client cleanup', result['cleanup_errors'][0])
        receipt = next(output.glob('Session-*/session.json'))
        self.assertEqual(json.loads(receipt.read_text())['status'], 'failed')
        self.assertNotIn(token, (receipt.parent / 'console.log').read_text())


if __name__ == '__main__':
    unittest.main()
