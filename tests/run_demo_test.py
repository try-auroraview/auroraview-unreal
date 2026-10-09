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

    def cache_inputs(self):
        prepared = self.editor_inputs()
        prepared.update(engine_version='5.7', derived_data_cache={'graph': run_demo.validate_game.DDC_GRAPH})
        config = Path(prepared['uproject']).parent / 'Config/DefaultEngine.ini'
        config.write_text(config.read_text() + '\n[AuroraViewValidationDDC]\n'
                          'Local=(Type=FileSystem,ReadOnly=false,Clean=false,Flush=false,DeleteUnused=false,'
                          'Path="%GAMEDIR%DerivedDataCache",DeleteOnly=false)\n', encoding='utf-8')
        prepared['products_sha256']['Project/Config/DefaultEngine.ini'] = run_demo.build_plugin.sha256(config)
        return prepared

    def test_runtime_cache_survives_unique_sessions_without_changing_verified_products(self):
        prepared = self.cache_inputs()
        output = Path(prepared['uproject']).parent.parent
        config = Path(prepared['uproject']).parent / 'Config/DefaultEngine.ini'
        original = config.read_bytes()
        cache = run_demo.editor_runtime_cache(prepared, output)
        (cache / 'owned-shader.udd').write_bytes(b'previous successful shader result')
        for name in ('First', 'Second'):
            session = output / name
            session.mkdir()
            project = run_demo.editor_session_project(prepared, session, run_demo.editor_runtime_cache(prepared, output))
            current = (project.parent / 'Config/DefaultEngine.ini').read_bytes()
            self.assertIn(('Path="' + cache.as_posix() + '"').encode(), current)
            self.assertNotIn(b'%GAMEDIR%DerivedDataCache', current)
            inputs = json.loads((session / 'editor-inputs.json').read_text())
            override = inputs['runtime_overrides']['Project/Config/DefaultEngine.ini']
            self.assertEqual(override['input_sha256'], prepared['products_sha256']['Project/Config/DefaultEngine.ini'])
            self.assertEqual(override['sha256'], run_demo.build_plugin.sha256(project.parent / 'Config/DefaultEngine.ini'))
            self.assertEqual(override['cache_directory'], str(cache))
            self.assertNotEqual(override['input_sha256'], override['sha256'])
            (project.parent / 'Config/DefaultEngine.ini').write_bytes(b'Editor generated settings')
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual((cache / 'owned-shader.udd').read_bytes(), b'previous successful shader result')
        self.assertFalse(any(path.name.startswith('.write-probe-') for path in cache.iterdir()))
        self.assertFalse(any('RuntimeDerivedDataCache' in name for name in prepared['products_sha256']))

    def test_runtime_cache_refuses_foreign_directory_protected_inputs_and_missing_graph(self):
        prepared = self.cache_inputs()
        output = Path(prepared['uproject']).parent.parent
        session = output / 'First'
        session.mkdir()
        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'belong'):
            run_demo.editor_session_project(prepared, session, self.root / 'foreign-cache')
        prepared['engine_root'] = str(output)
        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'protected'):
            run_demo.editor_runtime_cache(prepared, output)
        del prepared['engine_root']
        prepared['derived_data_cache'] = {}
        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'verified private'):
            run_demo.editor_runtime_cache(prepared, output)

    def test_runtime_override_preserves_ue58_store_graph_and_refuses_ambiguous_portable_path(self):
        prepared = self.editor_inputs()
        project = Path(prepared['uproject']).parent
        (project / 'Config/DefaultGame.ini').write_text('[Packaging]\n', encoding='utf-8')
        prepared['engine_version'] = '5.8'
        prepared['derived_data_cache'] = run_demo.validate_game.configure_project_cache(project, '5.8')
        config = project / 'Config/DefaultEngine.ini'
        prepared['products_sha256']['Project/Config/DefaultEngine.ini'] = run_demo.build_plugin.sha256(config)
        original = config.read_bytes()
        output = project.parent
        cache = run_demo.editor_runtime_cache(prepared, output)
        session = output / 'First'
        session.mkdir()
        copied = run_demo.editor_session_project(prepared, session, cache)
        actual = (copied.parent / 'Config/DefaultEngine.ini').read_bytes()
        self.assertEqual(actual, original.replace(b'Path="%GAMEDIR%DerivedDataCache"',
                                                 ('Path="' + cache.as_posix() + '"').encode()))
        self.assertIn(b'[DerivedDataCacheStores]', actual)
        config.write_bytes(original + b'\nPath="%GAMEDIR%DerivedDataCache"\n')
        prepared['products_sha256']['Project/Config/DefaultEngine.ini'] = run_demo.build_plugin.sha256(config)
        second = output / 'Second'
        second.mkdir()
        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'one verified portable'):
            run_demo.editor_session_project(prepared, second, cache)
        self.assertEqual((second / 'Project/Config/DefaultEngine.ini').read_bytes(), config.read_bytes())

    def test_runtime_cache_refuses_reparse_ancestors_and_keeps_colliding_probe(self):
        prepared = self.cache_inputs()
        output = Path(prepared['uproject']).parent.parent
        cache = output / 'RuntimeDerivedDataCache'
        cache.mkdir()
        existing = cache / '.write-probe-fixed'
        existing.write_bytes(b'preexisting owned evidence')
        with patch.object(run_demo.secrets, 'token_hex', return_value='fixed'):
            with self.assertRaises(FileExistsError):
                run_demo.editor_runtime_cache(prepared, output)
        self.assertEqual(existing.read_bytes(), b'preexisting owned evidence')
        original_lstat = Path.lstat
        def reparse(path):
            if path == cache:
                return Mock(st_file_attributes=run_demo.stat.FILE_ATTRIBUTE_REPARSE_POINT, st_mode=run_demo.stat.S_IFDIR)
            return original_lstat(path)
        with patch.object(Path, 'lstat', reparse):
            with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'ordinary'):
                run_demo.editor_runtime_cache(prepared, output)

    def test_startup_bounds_reject_before_output_or_host(self):
        with patch.object(run_demo.subprocess, 'Popen') as spawn:
            for seconds in (True, None, 0, 29, 3601, float('inf')):
                with self.subTest(seconds=seconds), self.assertRaises(run_demo.build_plugin.BuildError):
                    run_demo.launch({}, self.root / 'demo.html', self.root / 'Missing', 0, startup_timeout=seconds)
        spawn.assert_not_called()
        self.assertFalse((self.root / 'Missing').exists())

    def test_startup_uses_one_deadline_caps_rpc_and_refuses_late_success(self):
        receipt = {}
        client = Mock()
        with patch.object(run_demo.time, 'monotonic', side_effect=[100, 125, 129, 130]):
            budget = run_demo.StartupDeadline(30, receipt)
            self.assertEqual(budget.remaining('scene'), 5)
            with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'timeout during scene'):
                budget.call(client, 'unreal.engine.info')
        client.call.assert_called_once_with('unreal.engine.info', timeout=1)
        self.assertEqual(receipt['phase'], 'scene')
        self.assertEqual(receipt['elapsed_seconds'], 30)

    def test_expired_endpoint_keeps_failed_receipt_and_owned_cleanup(self):
        output = self.root / 'Run'
        output.mkdir()
        process = Mock(pid=123, returncode=0)
        process.poll.return_value = None
        process.wait.return_value = 0
        clock = [100.0]
        def connect(*_args, **_kwargs):
            clock[0] = 131.0
            raise TimeoutError('native endpoint not ready')
        with patch.object(run_demo, 'Client', side_effect=connect) as client, \
                patch.object(run_demo.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(run_demo.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'timeout during endpoint'):
                run_demo.launch(dict(mode='game', executable=str(self.root / 'Game.exe'), engine_version='5.7'),
                                self.root / 'unused.html', output, 0, startup_timeout=30)
        self.assertEqual(client.call_count, 1)
        process.wait.assert_called_once_with(timeout=60)
        receipt = json.loads(next(output.glob('Session-*/session.json')).read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['startup'], dict(timeout_seconds=30, phase='endpoint', elapsed_seconds=31.0))
        self.assertFalse(receipt['forced_cleanup'])

    def test_late_tool_registration_never_opens_view_or_publishes_ready(self):
        output = self.root / 'Run'
        output.mkdir()
        process = Mock(pid=123, returncode=0)
        process.poll.return_value = None
        process.wait.return_value = 0
        client = Mock(timeout=0.5, identity={'pid': 123, 'context': 'game', 'engine_version': '5.7.4'})
        client.call.return_value = {'engine_ready': True}
        tools = Mock()
        clock = [100.0]
        tools.register.side_effect = lambda **_kwargs: clock.__setitem__(0, 131.0)
        with patch.object(run_demo, 'Client', return_value=client), \
                patch.object(run_demo.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(run_demo.subprocess, 'Popen', return_value=process), \
                patch.object(run_demo.demo_tools, 'DemoTools', return_value=tools), \
                patch.object(run_demo.demo_tools, 'find_scene', return_value=('world', 'scene')):
            with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'timeout during tools'):
                run_demo.launch(dict(mode='game', executable=str(self.root / 'Game.exe'), engine_version='5.7'),
                                self.root / 'unused.html', output, 0, startup_timeout=30)
        self.assertEqual([call.args[0] for call in client.call.call_args_list],
                         ['unreal.engine.info', 'auroraview.host.shutdown'])
        tools.close.assert_called_once()
        client.close.assert_called_once()
        self.assertEqual(client.timeout, 5.0)
        receipt = json.loads(next(output.glob('Session-*/session.json')).read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['startup']['phase'], 'tools')
        self.assertNotIn('presentation', receipt)

    def test_late_hello_restores_normal_cleanup_timeout_before_deadline_check(self):
        output = self.root / 'Run'
        output.mkdir()
        process = Mock(pid=123, returncode=0)
        process.poll.return_value = None
        process.wait.return_value = 0
        client = Mock(timeout=0.5)
        clock = [100.0]
        cleanup_timeouts = []
        def connect(*_args, **_kwargs):
            clock[0] = 131.0
            return client
        client.call.side_effect = lambda *_args: cleanup_timeouts.append(client.timeout)
        with patch.object(run_demo, 'Client', side_effect=connect), \
                patch.object(run_demo.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(run_demo.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'timeout during scene'):
                run_demo.launch(dict(mode='game', executable=str(self.root / 'Game.exe'), engine_version='5.7'),
                                self.root / 'unused.html', output, 0, startup_timeout=30)
        self.assertEqual(cleanup_timeouts, [5.0])
        client.call.assert_called_once_with('auroraview.host.shutdown')
        client.close.assert_called_once()
        process.wait.assert_called_once_with(timeout=60)
        receipt = json.loads(next(output.glob('Session-*/session.json')).read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['startup']['phase'], 'scene')
        self.assertFalse(receipt['forced_cleanup'])

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

    def test_browser_handshake_pumps_tools_on_the_registered_owner_thread(self):
        dispatcher = run_demo.OwnerDispatcher()
        tools = Mock(browser_ready=threading.Event())
        process = Mock()
        process.poll.return_value = None
        owner, observed = threading.get_ident(), []
        def incoming_call():
            pending = dispatcher(lambda: observed.append(threading.get_ident()) or tools.browser_ready.set())
            pending.result(timeout=2)
        worker = threading.Thread(target=incoming_call)
        worker.start()
        try:
            self.assertTrue(run_demo.wait_for_browser(tools, process, timeout=1, dispatcher=dispatcher))
        finally:
            worker.join(2)
            dispatcher.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, [owner])

    def test_browser_wait_refuses_exited_host_and_bounded_missing_handshake(self):
        tools = Mock(browser_ready=threading.Event())
        process = Mock()
        process.poll.return_value = 1
        self.assertFalse(run_demo.wait_for_browser(tools, process, timeout=1))
        process.poll.return_value = None
        self.assertFalse(run_demo.wait_for_browser(tools, process, timeout=0))

    def test_missing_shared_package_fails_before_starting_a_host(self):
        with patch.object(run_demo.demo_tools, 'require_shared_tools', side_effect=ValueError('Pinned package missing')), \
                patch.object(run_demo.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'Pinned package missing'):
                run_demo.launch({}, self.root / 'demo.html', self.root / 'Run', 0, shared_tools=True)
        spawn.assert_not_called()

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
            def call(self, method, *_args, **_kwargs):
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
            def register(self, **_kwargs):
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
