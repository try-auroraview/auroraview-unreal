"""Integrity and teardown failures must not become a successful demonstration."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

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

    def editor_inputs(self):
        build = self.root / 'Build'
        project = build / 'Project'
        inputs = {
            'AuroraViewGameFixture.uproject': b'{"FileVersion":3}',
            'Config/DefaultEngine.ini': b'[Engine]\nOriginal=True\n',
            'Binaries/Win64/UnrealEditor-AuroraViewGameFixture.dll': b'verified native build',
            'Source/AuroraViewGameFixture.Target.cs': b'public class FixtureTarget {}',
        }
        products = {}
        for relative, data in inputs.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            products['Project/' + relative] = run_demo.build_plugin.sha256(path)
        return dict(uproject=str(project / 'AuroraViewGameFixture.uproject'), products_sha256=products)

    def test_editor_config_writes_do_not_change_verified_build_or_next_session(self):
        prepared = self.editor_inputs()
        first, second = self.root / 'First', self.root / 'Second'
        first.mkdir()
        second.mkdir()
        runtime = run_demo.editor_session_project(prepared, first)
        (runtime.parent / 'Config/DefaultEngine.ini').write_bytes(b'Editor generated settings')
        reopened = run_demo.editor_session_project(prepared, second)
        original = Path(prepared['uproject']).parent / 'Config/DefaultEngine.ini'
        self.assertEqual((reopened.parent / 'Config/DefaultEngine.ini').read_bytes(), original.read_bytes())
        self.assertEqual(run_demo.build_plugin.sha256(original), prepared['products_sha256']['Project/Config/DefaultEngine.ini'])

    def test_editor_copy_rejects_changed_input_and_path_escape(self):
        prepared = self.editor_inputs()
        first, second = self.root / 'First', self.root / 'Second'
        first.mkdir()
        second.mkdir()
        (Path(prepared['uproject']).parent / 'Config/DefaultEngine.ini').write_bytes(b'changed')
        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'input changed'):
            run_demo.editor_session_project(prepared, first)
        prepared['products_sha256'] = {'Project/../../escape.dll': '0' * 64}
        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'Unsafe prepared'):
            run_demo.editor_session_project(prepared, second)

    def test_dock_retries_only_layout_readiness_and_returns_real_attachment(self):
        client = Mock()
        state = {'attached_to_root_window': True}
        client.call.side_effect = [run_demo.RemoteError('EditorUnavailable', 'Layout not ready', 'EDITOR_UNAVAILABLE'), state]
        with patch.object(run_demo.time, 'sleep') as sleep:
            self.assertIs(run_demo.dock_editor_view(client), state)
        self.assertEqual(client.call.call_count, 2)
        sleep.assert_called_once_with(0.2)

    def test_dock_preserves_hard_failure_and_bounds_readiness_wait(self):
        client = Mock()
        client.call.side_effect = run_demo.RemoteError('DockFailed', 'Native attachment failed', 'VIEW_DOCK_FAILED')
        with patch.object(run_demo.time, 'sleep') as sleep:
            with self.assertRaises(run_demo.RemoteError):
                run_demo.dock_editor_view(client)
            sleep.assert_not_called()
        client.call.side_effect = run_demo.RemoteError('EditorUnavailable', 'Layout not ready', 'EDITOR_UNAVAILABLE')
        with patch.object(run_demo.time, 'monotonic', side_effect=[1.0, 2.0]):
            with self.assertRaises(run_demo.RemoteError):
                run_demo.dock_editor_view(client, timeout=0.5)

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
                if method == 'auroraview.view.describe':
                    return {'ready': True, 'presentation': 'floating'}
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

    def test_failed_forced_wait_keeps_original_failure_and_redacts_all_evidence(self):
        output = self.root / 'Run'
        output.mkdir()
        html = self.root / 'demo.html'
        html.write_text('<h1>Demo</h1>', encoding='utf-8')
        token = 'private-forced-wait-fixture-' * 3
        process = Mock(pid=123, returncode=None)
        process.poll.return_value = None
        process.wait.side_effect = [run_demo.subprocess.TimeoutExpired(['Game.exe', token], 60),
                                    run_demo.subprocess.TimeoutExpired(['Game.exe', token], 15)]

        def popen(command, **kwargs):
            kwargs['stdout'].write(token.encode())
            log = Path(next(value[len('-abslog='):] for value in command if value.startswith('-abslog=')))
            log.write_text('Host command token=' + token, encoding='utf-8')
            return process

        with patch.object(run_demo, 'Client', side_effect=run_demo.build_plugin.BuildError('Initial browser failure ' + token)), \
                patch.dict(run_demo.os.environ, {'SystemRoot': 'C:\\Windows'}), \
                patch.object(run_demo.subprocess, 'Popen', popen), \
                patch.object(run_demo.subprocess, 'run', return_value=Mock(returncode=1)), \
                patch.object(run_demo.secrets, 'token_urlsafe', return_value=token):
            with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'Initial browser failure') as raised:
                run_demo.launch(dict(mode='game', executable=str(self.root / 'Game.exe'), engine_version='5.7'),
                                html, output, 0)

        self.assertNotIn(token, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual([call.kwargs for call in process.wait.call_args_list], [{'timeout': 60}, {'timeout': 15}])
        receipt = next(output.glob('Session-*/session.json'))
        result = json.loads(receipt.read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Initial browser failure', result['error'])
        self.assertIn('Process cleanup', result['cleanup_errors'][0])
        self.assertTrue(result['forced_cleanup'])
        self.assertIsNone(result['exit_code'])
        self.assertTrue(result['completed_utc'])
        for path in receipt.parent.iterdir():
            self.assertNotIn(token, path.read_text())


if __name__ == '__main__':
    unittest.main()
