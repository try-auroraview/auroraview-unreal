"""Packaged Game validation guards; synthetic files only, never native evidence."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('validate_game', ROOT / 'scripts/validate_game.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
sys.path.pop(0)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding='utf-8')


def executable(machine=0x8664, characteristics=0x22):
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, machine)
    struct.pack_into('<H', data, 150, characteristics)
    struct.pack_into('<H', data, 152, 0x20B)
    return bytes(data)


class PackagedGameGuards(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project, self.archive, self.engine = [self.root / name for name in ['Project', 'Archive', 'Engine']]
        self.game = self.archive / f'Windows/{validator.PROJECT}/Binaries/Win64/{validator.PROJECT}.exe'
        write(self.game, executable())
        write(self.project / f'Binaries/Win64/{validator.PROJECT}.exe', executable())
        write(self.project / f'Binaries/Win64/{validator.PROJECT}.target', json.dumps({
            'TargetName': validator.PROJECT, 'Platform': 'Win64', 'Configuration': 'Development', 'TargetType': 'Game'}))
        self.receipt = {'source_assets_sha256': {}}
        self.policy = validator.preflight_engine.engine_policy({'MajorVersion': 5, 'MinorVersion': 7})
        for relative in ['Resources/ue_transport.js', 'ThirdParty/AuroraViewCore/manifest.json']:
            path = self.archive / f'Windows/{validator.PROJECT}/Plugins/AuroraView/{relative}'
            write(path, 'synthetic Runtime resource')
            self.receipt['source_assets_sha256'][relative] = validator.build_plugin.sha256(path)
        for name in ['libcef.dll', 'icudtl.dat', 'resources.pak']:
            write(self.archive / f'Windows/Engine/Binaries/ThirdParty/CEF3/Win64/{name}', 'synthetic CEF resource')
            write(self.engine / f'Engine/Binaries/ThirdParty/CEF3/Win64/{name}', 'synthetic CEF resource')
        for directory in [self.archive / 'Windows', self.engine]:
            write(directory / 'Engine/Binaries/Win64/EpicWebHelper.exe', 'synthetic CEF subprocess')

    def stage(self):
        return validator.stage_evidence(self.project, self.archive, self.engine, self.receipt, self.policy)

    def test_stage_binds_actual_executable_resources_and_cef(self):
        game, evidence = self.stage()
        self.assertEqual(game, self.game)
        self.assertEqual(evidence['executable_sha256'], validator.build_plugin.sha256(self.game))
        self.assertEqual(evidence['editor_binaries'], 'absent')

    def test_editor_binary_in_game_is_rejected(self):
        write(self.game.parent / 'UnrealEditor-AuroraViewEditor.dll', b'unexpected editor binary')
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'Editor binary'):
            self.stage()

    def test_changed_staged_executable_is_rejected(self):
        write(self.game, executable() + b'different from compiled Game')
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'differs from the actual compiled'):
            self.stage()

    def test_missing_cef_cannot_pass_runtime_distribution(self):
        (self.archive / 'Windows/Engine/Binaries/ThirdParty/CEF3/Win64/libcef.dll').unlink()
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'CEF runtime'):
            self.stage()

    def test_legacy_cef_and_target_receipt_follow_ue418_distribution(self):
        self.policy = validator.preflight_engine.engine_policy({'MajorVersion': 4, 'MinorVersion': 18})
        for directory in [self.archive / 'Windows', self.engine]:
            (directory / 'Engine/Binaries/Win64/EpicWebHelper.exe').unlink()
            write(directory / 'Engine/Binaries/Win64/UnrealCEFSubProcess.exe', 'synthetic CEF subprocess')
        target = self.project / f'Binaries/Win64/{validator.PROJECT}.target'
        data = json.loads(target.read_text())
        del data['TargetType']
        write(target, json.dumps(data))
        (self.archive / 'Windows/Engine/Binaries/ThirdParty/CEF3/Win64/resources.pak').unlink()
        for name in ['cef.pak', 'cef_100_percent.pak', 'cef_200_percent.pak', 'cef_extensions.pak',
                     'devtools_resources.pak', 'natives_blob.bin', 'snapshot_blob.bin']:
            for directory in [self.archive / 'Windows', self.engine]:
                write(directory / f'Engine/Binaries/ThirdParty/CEF3/Win64/{name}', 'synthetic legacy CEF')
        self.stage()
        self.policy = validator.preflight_engine.engine_policy({'MajorVersion': 4, 'MinorVersion': 26})
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'not a Game'):
            self.stage()

    def test_cef_subprocess_must_match_installed_engine(self):
        write(self.archive / 'Windows/Engine/Binaries/Win64/EpicWebHelper.exe', 'changed subprocess')
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'CEF subprocess'):
            self.stage()

    def test_editor_target_receipt_is_rejected(self):
        path = self.project / f'Binaries/Win64/{validator.PROJECT}.target'
        data = json.loads(path.read_text())
        data['TargetType'] = 'Editor'
        write(path, json.dumps(data))
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'not a Game'):
            self.stage()

    def test_package_extra_or_changed_files_are_rejected(self):
        package = self.root / 'Package'
        write(package / 'asset.js', 'original')
        expected = validator.inventory(package)
        validator.verify_package(package, expected)
        write(package / 'extra.js', 'unexpected')
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'differ'):
            validator.verify_package(package, expected)

    def test_native_executable_rejects_dll_and_wrong_architecture(self):
        for value in [executable(machine=0x14c), executable(characteristics=0x2022)]:
            write(self.game, value)
            with self.assertRaises(validator.build_plugin.BuildError):
                validator.validate_executable(self.game)

    def test_buildcookrun_uses_explicit_game_and_legacy_toolchain(self):
        for version in preflight_versions():
            policy = validator.preflight_engine.engine_policy(version)
            command = validator.game_command(self.engine, self.project / 'Fixture.uproject', self.archive, policy)
            self.assertIn('-target=' + validator.PROJECT, command)
            self.assertIn('-clientconfig=Development', command)
            self.assertIn('-cook', command)
            self.assertNotIn('-skipbuildeditor', command)
            self.assertIn('-nocompileeditor', command)
            self.assertNotIn('-VS2019', command)
            self.assertEqual('-ubtargs=-2019 -NoHotReloadFromIDE' in command, policy['version'] == '4.26')
            editor = validator.editor_command(self.engine, self.project / 'Fixture.uproject', policy)
            self.assertIn(validator.PROJECT + 'Editor', editor)
            self.assertEqual('-2019' in editor, policy['version'] == '4.26')
            self.assertEqual('-NoHotReloadFromIDE' in editor, policy['version'] != '4.18')

    def test_fixture_targets_follow_engine_defaults_without_shared_build_override(self):
        package = self.root / 'Package'
        package.mkdir()
        for version in validator.preflight_engine.SUPPORTED_VERSIONS:
            root = self.root / version
            validator.create_project(root, package, version)
            for suffix in ['.Target.cs', 'Editor.Target.cs']:
                text = (root / 'Source' / f'{validator.PROJECT}{suffix}').read_text()
                self.assertNotIn('bOverrideBuildEnvironment', text)
                self.assertNotIn('BuildEnvironment =', text)
                self.assertEqual('BuildSettingsVersion.Latest' in text, version.startswith('5.'))
                self.assertEqual('EngineIncludeOrderVersion.Latest' in text, version.startswith('5.'))
                self.assertEqual('BuildSettingsVersion.V2' in text, version == '4.26')


def preflight_versions():
    return [{'MajorVersion': int(version.split('.')[0]), 'MinorVersion': int(version.split('.')[1])}
            for version in validator.preflight_engine.SUPPORTED_VERSIONS]


class ScriptedBrowserClient:
    """Synthetic replies exercise gate rejection, not a CEF implementation."""
    def __init__(self, ready=None, corrupt_event=False, stays_ready=False):
        self.callbacks = {}
        self.ready_payload = ready if ready is not None else {'call': 42, 'invoke': 43, 'via': 'cef-core'}
        self.corrupt_event, self.stays_ready = corrupt_event, stays_ready
        self.closed, self.removed = False, False

    def on(self, name, callback):
        self.callbacks[name] = callback
        return lambda: self.callbacks.pop(name, None)

    def call(self, method, params):
        if method == 'auroraview.view.open':
            self.callbacks['acceptance:browser-ready'](self.ready_payload)
            return True
        if method == 'auroraview.view.describe':
            return {'ready': not self.closed or self.stays_ready,
                    'generation': 1 if not self.closed or self.stays_ready else 0}
        if method == 'auroraview.view.close':
            self.closed = True
            return True
        if method == 'auroraview.view.remove':
            self.removed = True
            return True
        raise AssertionError('Unexpected fixture call: ' + method)

    def emit(self, name, data):
        if name != 'acceptance:browser-inbound':
            raise AssertionError('Unexpected fixture event')
        reply = dict(data, via='cef-core')
        if self.corrupt_event:
            reply['nonce'] = 'different challenge'
        self.callbacks['acceptance:browser-outbound'](reply)


class RenderedBrowserGuards(unittest.TestCase):
    def gate(self, client):
        return validator.validate_browser(client, SimpleNamespace(poll=lambda: None), 0.1)

    def test_complete_readback_and_teardown_are_required(self):
        client = ScriptedBrowserClient()
        result = self.gate(client)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual((result['core_call'], result['core_invoke']), (42, 43))
        self.assertTrue(client.closed and client.removed)
        self.assertEqual(client.callbacks, {})

    def test_invoke_wrong_result_rejects_rendered_gate(self):
        client = ScriptedBrowserClient(ready={'call': 42, 'invoke': 0, 'via': 'cef-core'})
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'call/invoke readback failed'):
            self.gate(client)
        self.assertTrue(client.removed)

    def test_unrelated_event_cannot_satisfy_browser_challenge(self):
        client = ScriptedBrowserClient(corrupt_event=True)
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'event to Python readback failed'):
            self.gate(client)

    def test_close_ack_alone_does_not_prove_view_was_retired(self):
        client = ScriptedBrowserClient(stays_ready=True)
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'remained live after close'):
            self.gate(client)


if __name__ == '__main__':
    unittest.main()
