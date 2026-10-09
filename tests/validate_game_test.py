"""Packaged Game validation guards; synthetic files only, never native evidence."""
import importlib.util
import configparser
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('validate_game', ROOT / 'scripts/validate_game.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
sys.path.pop(0)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (bytes, bytearray)):
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


def resource_executable(reordered=False, code_page=0, initialized_size=0x200):
    """PE32+ with two real type/name/language resource trees and an overlay."""
    data = bytearray(0x800)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, 2, 0x12345678, 0, 0, 0xf0, 0x22)
    optional = 0x98
    struct.pack_into('<H', data, optional, 0x20b)
    struct.pack_into('<III', data, optional + 4, 0x200, initialized_size, 0)
    struct.pack_into('<IIQII', data, optional + 16, 0x1000, 0x1000, 0x140000000, 0x1000, 0x200)
    struct.pack_into('<HH', data, optional + 40, 6, 0)
    struct.pack_into('<HH', data, optional + 48, 6, 0)
    struct.pack_into('<II', data, optional + 56, 0x3000, 0x200)
    struct.pack_into('<HH', data, optional + 68, 3, 0x8160)
    struct.pack_into('<QQQQII', data, optional + 72, 0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    struct.pack_into('<II', data, optional + 128, 0x2000, 0x180)
    for offset, name, virtual_size, rva, raw_size, raw_offset, flags in [
        (0x188, b'.text', 0x180, 0x1000, 0x200, 0x200, 0x60000020),
        (0x1b0, b'.rsrc', 0x180, 0x2000, 0x400, 0x400, 0x40000040),
    ]:
        data[offset:offset + 8] = name.ljust(8, b'\0')
        struct.pack_into('<IIIIIIHHI', data, offset + 8,
                         virtual_size, rva, raw_size, raw_offset, 0, 0, 0, 0, flags)
    data[0x200:0x400] = b'\x90' * 0x200
    resource = memoryview(data)[0x400:0x800]
    layouts = (0x38, 0x20, 0x68, 0x50, 0x90, 0x80, 0xb0, 0x140, 0x120) if reordered else (
        0x20, 0x38, 0x50, 0x68, 0x80, 0x90, 0xa0, 0x120, 0x140)
    type_a, type_b, name_a, name_b, leaf_a, leaf_b, name_offset, payload_a, payload_b = layouts

    def directory(offset, entries):
        named = sum(bool(name & 0x80000000) for name, _ in entries)
        struct.pack_into('<IIHHHH', resource, offset, 0, 0, 0, 0, named, len(entries) - named)
        for index, (name, child) in enumerate(entries):
            struct.pack_into('<II', resource, offset + 16 + index * 8, name, child)

    directory(0, [(10, 0x80000000 | type_a), (24, 0x80000000 | type_b)])
    directory(type_a, [(0x80000000 | name_offset, 0x80000000 | name_a)])
    directory(type_b, [(1, 0x80000000 | name_b)])
    directory(name_a, [(1033, leaf_a)])
    directory(name_b, [(1033, leaf_b)])
    name = 'Fixture'.encode('utf-16-le')
    struct.pack_into('<H', resource, name_offset, len(name) // 2)
    resource[name_offset + 2:name_offset + 2 + len(name)] = name
    for leaf, offset, payload in [(leaf_a, payload_a, b'fixture resource A\0'),
                                  (leaf_b, payload_b, b'fixture resource B\0')]:
        struct.pack_into('<IIII', resource, leaf, 0x2000 + offset, len(payload), code_page, 0)
        resource[offset:offset + len(payload)] = payload
    del resource
    return bytes(data) + b'SYNTHETIC-OVERLAY'


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
            'TargetName': validator.PROJECT, 'Platform': 'Win64', 'Configuration': 'Development', 'TargetType': 'Game',
            'BuildProducts': [{'Type': 'Executable',
                               'Path': str(self.project / f'Binaries/Win64/{validator.PROJECT}.exe')}]}))
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

    def test_cef_resources_with_duplicate_names_bind_each_installed_path(self):
        relative = 'Engine/Binaries/ThirdParty/CEF3/Win64/Resources/icudtl.dat'
        for directory in [self.archive / 'Windows', self.engine]:
            write(directory / relative, 'second installed ICU resource')
        self.stage()
        write(self.archive / 'Windows' / relative, 'changed second ICU resource')
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'installed engine path'):
            self.stage()

    def test_cef_resource_matching_another_installed_path_is_rejected(self):
        relative = 'Engine/Binaries/ThirdParty/CEF3/Win64/Resources/icudtl.dat'
        write(self.archive / 'Windows' / relative, 'synthetic CEF resource')
        with self.assertRaisesRegex(validator.build_plugin.BuildError, 'installed engine path'):
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
            editor = validator.editor_command(self.engine, self.project / 'Fixture.uproject', policy,
                                              self.root / 'ubt-editor.log')
            self.assertIn(validator.PROJECT + 'Editor', editor)
            self.assertIn('-log=' + str(self.root / 'ubt-editor.log'), editor)
            self.assertEqual('-2019' in editor, policy['version'] == '4.26')
            self.assertEqual('-NoHotReloadFromIDE' in editor, policy['version'] != '4.18')

    def test_fixture_targets_follow_engine_defaults_without_shared_build_override(self):
        package = self.root / 'Package'
        package.mkdir()
        for version in validator.preflight_engine.SUPPORTED_VERSIONS:
            root = self.root / version
            validator.create_project(root, package, version)
            self.assertTrue((root / 'Content').is_dir())
            for suffix in ['.Target.cs', 'Editor.Target.cs']:
                text = (root / 'Source' / f'{validator.PROJECT}{suffix}').read_text()
                self.assertNotIn('bOverrideBuildEnvironment', text)
                self.assertNotIn('BuildEnvironment =', text)
                self.assertEqual('BuildSettingsVersion.Latest' in text, version.startswith('5.'))
                self.assertEqual('EngineIncludeOrderVersion.Latest' in text, version.startswith('5.'))
                self.assertEqual('BuildSettingsVersion.V2' in text, version == '4.26')

    def test_cook_selects_the_private_cache_graph_on_every_supported_engine(self):
        for version in validator.preflight_engine.SUPPORTED_VERSIONS:
            with self.subTest(version=version):
                major, minor = map(int, version.split('.'))
                policy = validator.preflight_engine.engine_policy({'MajorVersion': major, 'MinorVersion': minor})
                command = validator.game_command(self.engine, self.project / 'Fixture.uproject', self.archive, policy)
                self.assertEqual([arg for arg in command if arg.startswith('-AdditionalCookerOptions=')],
                                 ['-AdditionalCookerOptions=-ddc=AuroraViewValidationDDC'])

    def test_cache_graph_is_portable_writable_and_has_no_shared_store_or_path_overrides(self):
        package = self.root / 'CachePackage'
        package.mkdir()
        for version in validator.preflight_engine.SUPPORTED_VERSIONS:
            with self.subTest(version=version):
                project = self.root / ('cache-' + version)
                with patch.dict(validator.os.environ, {'UE-LocalDataCachePath': str(self.root / 'foreign-cache')}):
                    receipt = validator.create_project(project, package, version)
                config = project / 'Config/DefaultEngine.ini'
                text = config.read_text(encoding='utf-8')
                parser = configparser.RawConfigParser(strict=False)
                parser.optionxform = str
                parser.read_string(text)
                if version == '5.8':
                    self.assertEqual(parser['DerivedDataCacheGraphs']['AuroraViewValidationDDC'],
                                     '(AuroraViewValidationLocal)')
                    nodes = dict(parser['DerivedDataCacheStores'])
                    self.assertEqual(set(nodes), {'AuroraViewValidationLocal'})
                    local = nodes['AuroraViewValidationLocal']
                else:
                    nodes = dict(parser['AuroraViewValidationDDC'])
                    self.assertEqual(set(nodes), {'Root', 'AsyncPut', 'Local'})
                    self.assertEqual(nodes['Root'], '(Type=KeyLength,Length=120,Inner=AsyncPut)')
                    self.assertEqual(nodes['AsyncPut'], '(Type=AsyncPut,Inner=Local)')
                    self.assertIn('DeleteOnly=false', nodes['Local'])
                    local = nodes['Local']
                self.assertIn('Type=FileSystem', local)
                self.assertIn('ReadOnly=false', local)
                self.assertIn('Path="%GAMEDIR%DerivedDataCache"', local)
                for external in ['Zen', 'Shared', 'Cloud', 'Override']:
                    self.assertNotIn(external, '\n'.join(nodes.values()))
                self.assertNotIn(str(project), text)
                cache = project / 'DerivedDataCache'
                self.assertTrue(cache.is_dir())
                self.assertEqual(list(cache.iterdir()), [])
                self.assertEqual(receipt['directory'], str(cache))
                self.assertEqual(receipt['config_sha256'], validator.build_plugin.sha256(config))
                self.assertEqual(receipt['writable_probe'], 'pass')
                self.assertFalse((self.root / 'foreign-cache').exists())
                packaging = configparser.RawConfigParser(strict=False)
                packaging.read(project / 'Config/DefaultGame.ini', encoding='utf-8')
                if version.startswith('5.'):
                    self.assertFalse(packaging.getboolean('/Script/UnrealEd.ProjectPackagingSettings', 'bUseZenStore'))

    def test_legacy_cache_path_limit_fails_before_native_cook_or_config_writes(self):
        for version in ('4.18', '4.26'):
            with self.subTest(version=version):
                project = self.root / ('long-project-' + 'x' * 120)
                with self.assertRaisesRegex(validator.build_plugin.BuildError, '119 characters'):
                    validator.configure_project_cache(project, version)
                self.assertFalse(project.exists())


class StagedExecutableGuards(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'Project'
        self.compiled = self.project / f'Binaries/Win64/{validator.PROJECT}.exe'
        self.staged = self.root / f'Archive/Windows/{validator.PROJECT}/Binaries/Win64/{validator.PROJECT}.exe'
        self.policy = validator.preflight_engine.engine_policy({'MajorVersion': 4, 'MinorVersion': 18})
        self.target = {
            'TargetName': validator.PROJECT, 'Platform': 'Win64', 'Configuration': 'Development', 'TargetType': 'Game',
            'BuildProducts': [{'Type': 'Executable', 'Path': str(self.compiled)}],
        }
        write(self.compiled, resource_executable())
        write(self.staged, resource_executable(reordered=True, code_page=1252))

    def evidence(self):
        return validator.staged_executable_evidence(self.project, self.staged, self.target, self.policy)

    def test_identical_files_use_whole_file_comparison_on_every_supported_engine(self):
        write(self.staged, self.compiled.read_bytes())
        for version in preflight_versions():
            with self.subTest(version=version):
                self.policy = validator.preflight_engine.engine_policy(version)
                result = self.evidence()
                self.assertEqual(result['comparison'], 'whole_file')

    def test_ue418_accepts_directory_reordering_and_zero_to_1252_code_page(self):
        result = self.evidence()
        self.assertEqual(result['comparison'], 'ue418_resource_update')
        self.assertEqual(result['compiled_sha256'], validator.build_plugin.sha256(self.compiled))
        self.assertEqual(result['staged_sha256'], validator.build_plugin.sha256(self.staged))
        self.assertTrue(result['normalized_sha256'])
        self.assertNotEqual(result['compiled_sha256'], result['staged_sha256'])

    def test_ue418_accepts_directory_reordering_without_code_page_change(self):
        write(self.staged, resource_executable(reordered=True))
        self.assertEqual(self.evidence()['comparison'], 'ue418_resource_update')

    def test_ue418_accepts_exact_initialized_data_derivation(self):
        write(self.staged, resource_executable(reordered=True, code_page=1252, initialized_size=0x400))
        self.assertEqual(self.evidence()['comparison'], 'ue418_resource_update')

    def test_ue418_accepts_derived_resource_sizes_inside_unchanged_raw_layout(self):
        staged = bytearray(resource_executable(reordered=True, code_page=1252, initialized_size=0x400))
        struct.pack_into('<I', staged, 0x11c, 0x170)
        struct.pack_into('<I', staged, 0x1b8, 0x170)
        write(self.staged, staged)
        result = self.evidence()
        self.assertEqual(result['resource']['raw_size'], 0x400)
        self.assertEqual(result['header_fields']['ResourceVirtualSize']['compiled'], 0x180)
        self.assertEqual(result['header_fields']['ResourceVirtualSize']['staged'], 0x170)

    def test_resource_normalization_is_unavailable_outside_ue418(self):
        for version in preflight_versions():
            if version == {'MajorVersion': 4, 'MinorVersion': 18}:
                continue
            with self.subTest(version=version):
                self.policy = validator.preflight_engine.engine_policy(version)
                with self.assertRaises(validator.build_plugin.BuildError):
                    self.evidence()

    def test_payload_and_nonresource_bytes_cannot_change(self):
        original = self.staged.read_bytes()
        for name, offset in [('resource_payload', 0x540), ('text', 0x210), ('timestamp', 0x88),
                             ('checksum', 0xd8), ('header_padding', 0x1f0),
                             ('text_raw_padding', 0x3f0), ('overlay', len(original) - 1)]:
            with self.subTest(changed=name):
                changed = bytearray(original)
                changed[offset] ^= 1
                write(self.staged, changed)
                with self.assertRaises(validator.build_plugin.BuildError):
                    self.evidence()

    def test_resource_leaf_identity_cannot_change(self):
        original = self.staged.read_bytes()
        for name, offset in [('type', 0x410), ('name', 0x4b2), ('language', 0x478)]:
            with self.subTest(changed=name):
                changed = bytearray(original)
                changed[offset] ^= 1
                write(self.staged, changed)
                with self.assertRaises(validator.build_plugin.BuildError):
                    self.evidence()

    def test_other_code_page_changes_are_rejected(self):
        for compiled_page, staged_page in [(0, 65001), (1252, 0), (1252, 65001)]:
            with self.subTest(compiled=compiled_page, staged=staged_page):
                write(self.compiled, resource_executable(code_page=compiled_page))
                write(self.staged, resource_executable(reordered=True, code_page=staged_page))
                with self.assertRaises(validator.build_plugin.BuildError):
                    self.evidence()

    def test_executable_resource_section_and_raw_layout_changes_are_rejected(self):
        original = self.staged.read_bytes()
        for name, offset, value in [('executable_rsrc', 0x1d4, 0x60000040),
                                    ('rsrc_raw_size', 0x1c0, 0x200),
                                    ('rsrc_raw_offset', 0x1c4, 0x200)]:
            with self.subTest(changed=name):
                changed = bytearray(original)
                struct.pack_into('<I', changed, offset, value)
                write(self.staged, changed)
                with self.assertRaises(validator.build_plugin.BuildError):
                    self.evidence()

    def test_invalid_resource_header_relationships_are_rejected(self):
        original = self.staged.read_bytes()
        for name, offset, value in [('initialized_data', 0xa0, 0x201),
                                    ('resource_rva', 0x118, 0x2010),
                                    ('resource_size', 0x11c, 0x181),
                                    ('resource_virtual_size', 0x1b8, 0x181)]:
            with self.subTest(changed=name):
                changed = bytearray(original)
                struct.pack_into('<I', changed, offset, value)
                write(self.staged, changed)
                with self.assertRaises(validator.build_plugin.BuildError):
                    self.evidence()

    def test_file_length_change_is_rejected_even_with_valid_resources(self):
        write(self.staged, self.staged.read_bytes() + b'new overlay')
        with self.assertRaises(validator.build_plugin.BuildError):
            self.evidence()

    def test_every_comparison_requires_unique_project_executable_receipt(self):
        actual = {'Type': 'Executable', 'Path': str(self.compiled)}
        outside = self.root / f'Other/Binaries/Win64/{validator.PROJECT}.exe'
        renamed = self.compiled.with_name('Unrelated.exe')
        write(outside, self.compiled.read_bytes())
        write(renamed, self.compiled.read_bytes())
        for comparison, staged in [('resource_update', self.staged.read_bytes()),
                                   ('whole_file', self.compiled.read_bytes())]:
            write(self.staged, staged)
            for name, products in [
                ('missing_field', None),
                ('empty', []),
                ('null', None),
                ('not_array', actual),
                ('nonobject_product', [None]),
                ('wrong_type', [dict(actual, Type='DynamicLibrary')]),
                ('missing_path', [{'Type': 'Executable'}]),
                ('nonstring_path', [dict(actual, Path=42)]),
                ('outside_project', [dict(actual, Path=str(outside))]),
                ('wrong_game_name', [dict(actual, Path=str(renamed))]),
                ('duplicate', [actual, dict(actual)]),
                ('multiple_executables', [actual, dict(actual, Path=str(renamed))]),
            ]:
                with self.subTest(comparison=comparison, receipt=name):
                    if name == 'missing_field':
                        self.target.pop('BuildProducts', None)
                    else:
                        self.target['BuildProducts'] = products
                    with self.assertRaises(validator.build_plugin.BuildError):
                        self.evidence()


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


class GameExecutionPolicyTests(unittest.TestCase):
    def test_ue55_headless_acceptance_initializes_an_offscreen_rhi(self):
        mode, arguments = validator.game_execution_policy('5.5', False)
        self.assertEqual(mode, 'offscreen_d3d11')
        self.assertIn('-RenderOffscreen', arguments)
        self.assertIn('-d3d11', arguments)
        self.assertIn('-AllowSoftwareRendering', arguments)
        self.assertNotIn('-NullRHI', arguments)

    def test_other_admitted_versions_preserve_null_rhi_acceptance(self):
        for version in ['4.18', '4.26', '5.7', '5.8']:
            with self.subTest(version=version):
                mode, arguments = validator.game_execution_policy(version, False)
                self.assertEqual(mode, 'null_rhi')
                self.assertEqual(arguments, ['-NullRHI'])

    def test_rendered_browser_gate_remains_interactive_for_every_version(self):
        for version in ['4.18', '4.26', '5.5', '5.7', '5.8']:
            with self.subTest(version=version):
                mode, arguments = validator.game_execution_policy(version, True)
                self.assertEqual(mode, 'rendered_browser')
                self.assertIn('-Windowed', arguments)
                self.assertNotIn('-NullRHI', arguments)
                self.assertNotIn('-RenderOffscreen', arguments)


class GameCleanupTests(unittest.TestCase):
    def test_failed_forced_wait_preserves_host_failure_and_finalizes_redacted_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            token = 'private-cleanup-fixture-token-' * 3
            process = Mock(pid=123, returncode=None)
            process.poll.return_value = None
            process.wait.side_effect = validator.subprocess.TimeoutExpired(['Game.exe', token], 15)
            client = Mock(identity={'pid': 123})
            client.call.return_value = {}  # Reject the live identity before any mutation.
            client.close.side_effect = TimeoutError('Client cleanup token=' + token)
            api = SimpleNamespace(Client=Mock(return_value=client), ProtocolError=ValueError, RemoteError=RuntimeError)

            def popen(_command, **kwargs):
                kwargs['stdout'].write(token.encode())
                (evidence / 'Game.log').write_text('Host command token=' + token, encoding='utf-8')
                return process

            with patch.dict(sys.modules, {'auroraview_unreal': api}), \
                    patch.dict(validator.os.environ, {'SystemRoot': 'C:\\Windows'}), \
                    patch.object(validator.secrets, 'token_urlsafe', return_value=token), \
                    patch.object(validator.subprocess, 'STARTUPINFO', return_value=SimpleNamespace(dwFlags=0), create=True), \
                    patch.object(validator.subprocess, 'STARTF_USESHOWWINDOW', 1, create=True), \
                    patch.object(validator.subprocess, 'Popen', popen), \
                    patch.object(validator.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
                with self.assertRaisesRegex(validator.build_plugin.BuildError, 'Live Game engine identity'):
                    validator.run_game(evidence / 'Game.exe', evidence, '5.7', 30)

            process.wait.assert_called_once_with(timeout=15)
            result = json.loads((evidence / 'game-process.json').read_text())
            self.assertIn('Live Game engine identity', result['error'])
            self.assertEqual(len(result['cleanup_errors']), 2)
            self.assertTrue(result['forced_cleanup'])
            self.assertIsNone(result['exit_code'])
            self.assertTrue(result['completed_utc'])
            for path in evidence.iterdir():
                self.assertNotIn(token, path.read_text())

    def test_launch_exception_does_not_expose_its_command_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary)
            token = 'private-startup-fixture-token-' * 3
            api = SimpleNamespace(Client=Mock(), ProtocolError=ValueError, RemoteError=RuntimeError)
            failure = validator.subprocess.TimeoutExpired(['Game.exe', '-AuroraViewHostToken=' + token], 5)
            with patch.dict(sys.modules, {'auroraview_unreal': api}), \
                    patch.object(validator.secrets, 'token_urlsafe', return_value=token), \
                    patch.object(validator.subprocess, 'STARTUPINFO', return_value=SimpleNamespace(dwFlags=0), create=True), \
                    patch.object(validator.subprocess, 'STARTF_USESHOWWINDOW', 1, create=True), \
                    patch.object(validator.subprocess, 'Popen', side_effect=failure):
                with self.assertRaises(validator.build_plugin.BuildError) as raised:
                    validator.run_game(evidence / 'Game.exe', evidence, '5.7', 30)
            self.assertNotIn(token, str(raised.exception))
            self.assertTrue(raised.exception.__suppress_context__)
            result = json.loads((evidence / 'game-process.json').read_text())
            self.assertIn('<redacted>', result['error'])
            self.assertTrue(result['completed_utc'])
            self.assertNotIn(token, json.dumps(result))


if __name__ == '__main__':
    unittest.main()
