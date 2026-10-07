"""Package-contract tests with synthetic engine/files and a mocked UAT only.

These tests never invoke an Unreal compiler and provide no native build or UI
acceptance evidence. The production entry point invokes the installed RunUAT.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('build_plugin', ROOT / 'scripts/build_plugin.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
sys.path.pop(0)

TOOLCHAIN_LOG = (
    'TEST FIXTURE ONLY - no native compiler invoked\n'
    'Using Visual Studio 2022 14.44.35221 toolchain '
    '(C:\\VS\\VC\\Tools\\MSVC\\14.44.35207) and Windows 10.0.26100.0 SDK '
    '(C:\\Program Files (x86)\\Windows Kits\\10).\n'
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding='utf-8')


def pe_dll(machine=0x8664, characteristics=0x2022):
    """A structural PE fixture; not executable native build evidence."""
    data = bytearray(1024)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HH', data, 132, machine, 1)
    struct.pack_into('<HH', data, 148, 240, characteristics)
    struct.pack_into('<H', data, 152, 0x20B)
    data[392:400] = b'.text\0\0\0'
    struct.pack_into('<II', data, 408, 512, 512)
    return bytes(data)


class BuildPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.template.cleanup)
        cls.template_source = Path(cls.template.name) / 'source'
        cls.template_engine = Path(cls.template.name) / 'engine'
        source, engine = cls.template_source, cls.template_engine
        source.mkdir()
        write(source / 'AuroraView.uplugin', (ROOT / 'AuroraView.uplugin').read_text(encoding='utf-8'))
        write(source / 'Source/AuroraViewEditor/Fixture.cpp', '// unit test fixture\n')
        write(source / 'Resources/nested/fixture.html', '<!doctype html><title>fixture</title>\n')
        write(source / 'LICENSE', 'Plugin license fixture\n')
        core = source / 'ThirdParty/AuroraViewCore'
        write(core / 'bridge.js', '// bridge asset fixture\n')
        write(core / 'LICENSE', 'MIT License\nfixture\n')
        manifest = {'license': 'MIT', 'files': {'bridge.js': {
            'sha256': hashlib.sha256((core / 'bridge.js').read_bytes()).hexdigest()}}}
        write(core / 'manifest.json', json.dumps(manifest))
        write(engine / 'Engine/Build/Build.version', json.dumps({
            'MajorVersion': 5, 'MinorVersion': 7, 'PatchVersion': 0,
            'Changelist': 12345, 'CompatibleChangelist': 12345}))
        for relative in builder.preflight_engine.HEADERS + [
                'Build/BatchFiles/RunUAT.bat', 'Binaries/Win64/UnrealEditor.exe', 'Build/InstalledBuild.txt']:
            write(engine / 'Engine' / relative, 'synthetic inventory fixture\n')
        (engine / 'Engine/Binaries/ThirdParty/CEF3/Win64').mkdir(parents=True)
        write(engine / 'Engine/Binaries/Win64/UnrealEditor.modules',
              json.dumps({'BuildId': 'fixture-engine-build', 'Modules': {}}))
        for arguments in [('init', '--quiet'), ('config', 'user.name', 'loonghao'),
                          ('config', 'user.email', 'hal.long@outlook.com'), ('add', '.'),
                          ('commit', '--quiet', '-m', 'test: create synthetic build fixture')]:
            subprocess.run(['git', '-C', str(source), *arguments],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.engine = self.root / 'engine'
        self.output = self.root / 'run'
        shutil.copytree(self.template_source, self.source)
        shutil.copytree(self.template_engine, self.engine)
        self.core = self.source / 'ThirdParty/AuroraViewCore'
        self.version = self.engine / 'Engine/Build/Build.version'
        self.mutate_package = None
        self.mutate_after_uat = None
        self.uat_exit_code = 0
        self.uat_log = TOOLCHAIN_LOG
        self.runner = patch.object(builder, 'run_uat', side_effect=self.fake_uat).start()
        self.addCleanup(patch.stopall)

    def git(self, *arguments):
        process = subprocess.run(['git', '-C', str(self.source), *arguments],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return process.stdout.decode('utf-8').strip()

    def fake_uat(self, command, source, log_path):
        package = Path(next(value[len('-Package='):] for value in command if value.startswith('-Package=')))
        self.assertFalse(package.exists(), 'UAT must receive a fresh package destination')
        package.mkdir()
        for name in ['Resources', 'ThirdParty']:
            shutil.copytree(source / name, package / name)
        for name in ['AuroraView.uplugin', 'LICENSE']:
            shutil.copy2(source / name, package / name)
        write(package / 'Binaries/Win64' / builder.DLL, pe_dll())
        write(package / 'Binaries/Win64/UnrealEditor.modules', json.dumps({
            'BuildId': 'fixture-engine-build', 'Modules': {builder.MODULE: builder.DLL}}))
        write(log_path, self.uat_log.encode('utf-8'))
        if self.mutate_package:
            self.mutate_package(package)
        if self.mutate_after_uat:
            self.mutate_after_uat()
        return self.uat_exit_code

    def build(self):
        return builder.build(self.engine, self.output, self.source)

    def assert_failed(self, receipt, error_fragment):
        self.assertEqual(receipt['status'], 'failed')
        self.assertNotEqual(receipt['unreal_compile'], 'pass')
        self.assertEqual(receipt['unreal_ui'], 'not_run')
        self.assertTrue(any(error_fragment in error for error in receipt['errors']), receipt['errors'])
        self.assertEqual(json.loads((self.output / 'build-receipt.json').read_text()), receipt)

    def test_mocked_build_binds_git_engine_dll_assets_and_actual_log(self):
        receipt = self.build()
        self.assertEqual(receipt['status'], 'pass', receipt['errors'])
        self.assertEqual(receipt['unreal_compile'], 'pass')
        self.assertEqual(receipt['unreal_ui'], 'not_run')
        self.assertEqual(receipt['source']['commit'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(receipt['source']['tree'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertFalse(receipt['source']['dirty'])
        self.assertEqual(receipt['engine']['build_id'], 'fixture-engine-build')
        self.assertEqual(receipt['package']['dll']['sha256'], hashlib.sha256(pe_dll()).hexdigest())
        self.assertEqual(receipt['uat_log_sha256'], hashlib.sha256(TOOLCHAIN_LOG.encode()).hexdigest())
        self.assertEqual(receipt['compiler_toolchains'][0]['toolchain_version'], '14.44.35221')
        self.assertEqual(receipt['compiler_toolchains'][0]['windows_sdk_version'], '10.0.26100.0')
        self.assertEqual(receipt['compiler_toolchains'][0]['windows_sdk_path'],
                         'C:\\Program Files (x86)\\Windows Kits\\10')
        self.assertEqual(receipt['environment_overrides'], builder.BUILD_ENVIRONMENT)
        self.assertIn('-StrictIncludes', receipt['build_command'])
        self.assertIn('-TargetPlatforms=Win64', receipt['build_command'])
        self.assertEqual(receipt['uat_exit_code'], 0)
        for name, digest in receipt['source_assets_sha256'].items():
            self.assertEqual(receipt['package']['files_sha256'][name], digest)

    def test_dirty_source_is_explicit_and_hash_bound(self):
        write(self.source / 'Source/AuroraViewEditor/Fixture.cpp', '// changed fixture\n')
        write(self.source / 'Resources/untracked.js', '// untracked fixture\n')
        receipt = self.build()
        self.assertEqual(receipt['status'], 'pass', receipt['errors'])
        self.assertTrue(receipt['source']['dirty'])
        self.assertIn('Resources/untracked.js', receipt['source']['files'])

    def test_preflight_blocks_other_engine_versions_before_uat(self):
        write(self.version, json.dumps({'MajorVersion': 5, 'MinorVersion': 6}))
        receipt = self.build()
        self.assert_failed(receipt, 'preflight blocked')
        self.assertEqual(receipt['unreal_compile'], 'not_run')
        self.runner.assert_not_called()

    def test_missing_installed_engine_marker_blocks_before_uat(self):
        (self.engine / 'Engine/Build/InstalledBuild.txt').unlink()
        self.assert_failed(self.build(), 'not an installed build')
        self.runner.assert_not_called()

    def test_missing_engine_build_id_blocks_before_uat(self):
        write(self.engine / 'Engine/Binaries/Win64/UnrealEditor.modules', '{}')
        self.assert_failed(self.build(), 'contains no BuildId')
        self.runner.assert_not_called()

    def test_invalid_source_manifest_blocks_before_uat(self):
        write(self.core / 'bridge.js', '// unexpected source asset\n')
        self.assert_failed(self.build(), 'Core manifest asset hash mismatch')
        self.runner.assert_not_called()

    def test_source_manifest_cannot_escape_core_directory(self):
        write(self.core / 'manifest.json', json.dumps({'files': {'../escape.js': {'sha256': 'unused'}}}))
        self.assert_failed(self.build(), 'Invalid Core manifest asset path')
        self.runner.assert_not_called()

    def test_nonzero_uat_exit_cannot_pass_with_existing_dll(self):
        self.uat_exit_code = 6
        self.assert_failed(self.build(), 'RunUAT failed with exit code 6')

    def test_uat_launch_error_is_recorded_and_fails(self):
        self.runner.side_effect = OSError('fixture UAT launch failure')
        self.assert_failed(self.build(), 'fixture UAT launch failure')

    def test_missing_toolchain_log_cannot_claim_native_compilation(self):
        self.uat_log = 'fixture process returned 0, no compiler was invoked\n'
        self.assert_failed(self.build(), 'no actual Visual Studio compiler/toolchain evidence')

    def test_missing_packaged_assets_and_licenses_fail(self):
        for index, relative in enumerate(['Resources/nested/fixture.html',
                                          'ThirdParty/AuroraViewCore/bridge.js',
                                          'ThirdParty/AuroraViewCore/manifest.json',
                                          'ThirdParty/AuroraViewCore/LICENSE', 'LICENSE']):
            with self.subTest(relative=relative):
                self.output = self.root / f'missing-asset-{index}'
                self.mutate_package = lambda package, name=relative: (package / name).unlink()
                self.assert_failed(self.build(), 'Packaged asset is absent')

    def test_modified_packaged_resource_fails_hash_verification(self):
        self.mutate_package = lambda package: write(package / 'Resources/nested/fixture.html', 'changed')
        self.assert_failed(self.build(), 'Packaged asset hash mismatch')

    def test_missing_dll_cannot_pass(self):
        self.mutate_package = lambda package: (package / 'Binaries/Win64' / builder.DLL).unlink()
        self.assert_failed(self.build(), 'Required native DLL is absent')

    def test_renamed_text_dll_cannot_pass(self):
        self.mutate_package = lambda package: write(package / 'Binaries/Win64' / builder.DLL, 'not native code')
        self.assert_failed(self.build(), 'Not a Windows PE DLL')

    def test_wrong_architecture_or_non_dll_cannot_pass(self):
        for index, dll in enumerate([pe_dll(machine=0xAA64), pe_dll(characteristics=0x22)]):
            with self.subTest(index=index):
                self.output = self.root / f'invalid-dll-{index}'
                self.mutate_package = lambda package, data=dll: write(package / 'Binaries/Win64' / builder.DLL, data)
                self.assert_failed(self.build(), 'Output is not a Win64 DLL')

    def test_truncated_pe_section_cannot_pass(self):
        self.mutate_package = lambda package: write(package / 'Binaries/Win64' / builder.DLL, pe_dll()[:600])
        self.assert_failed(self.build(), 'Truncated PE section data')

    def test_wrong_packaged_descriptor_module_cannot_pass(self):
        def change(package):
            path = package / 'AuroraView.uplugin'
            descriptor = json.loads(path.read_text())
            descriptor['Modules'][0]['Type'] = 'Runtime'
            write(path, json.dumps(descriptor))
        self.mutate_package = change
        self.assert_failed(self.build(), 'Descriptor must declare')

    def test_wrong_module_build_id_or_dll_mapping_cannot_pass(self):
        for index, modules in enumerate([
                {'BuildId': 'different-engine', 'Modules': {builder.MODULE: builder.DLL}},
                {'BuildId': 'fixture-engine-build', 'Modules': {builder.MODULE: 'Different.dll'}}]):
            with self.subTest(index=index):
                self.output = self.root / f'invalid-modules-{index}'
                self.mutate_package = lambda package, value=modules: write(
                    package / 'Binaries/Win64/UnrealEditor.modules', json.dumps(value))
                self.assert_failed(self.build(), 'BuildId' if index == 0 else 'does not map')

    def test_source_change_during_uat_fails_provenance(self):
        self.mutate_after_uat = lambda: write(self.source / 'Source/AuroraViewEditor/Fixture.cpp', '// changed during UAT\n')
        self.assert_failed(self.build(), 'changed during the build')

    def test_engine_change_during_uat_fails_provenance(self):
        self.mutate_after_uat = lambda: write(self.version, json.dumps({'MajorVersion': 5, 'MinorVersion': 7, 'PatchVersion': 1}))
        self.assert_failed(self.build(), 'engine identity changed during the build')

    def test_output_cannot_overlap_source_or_engine_in_either_direction(self):
        for output in [self.source, self.source / 'build', self.engine, self.engine / 'build', self.root]:
            with self.subTest(output=output):
                with self.assertRaisesRegex(builder.BuildError, 'isolated'):
                    builder.build(self.engine, output, self.source)
        self.runner.assert_not_called()

    def test_existing_nonempty_output_is_never_overwritten(self):
        write(self.output / 'keep.txt', 'previous evidence')
        with self.assertRaisesRegex(builder.BuildError, 'not an empty directory'):
            self.build()
        self.assertEqual((self.output / 'keep.txt').read_text(), 'previous evidence')
        self.runner.assert_not_called()

    def test_empty_output_is_allowed_but_cannot_be_reused(self):
        self.output.mkdir()
        receipt = self.build()
        self.assertEqual(receipt['status'], 'pass', receipt['errors'])
        original = (self.output / 'build-receipt.json').read_bytes()
        with self.assertRaisesRegex(builder.BuildError, 'not an empty directory'):
            self.build()
        self.assertEqual((self.output / 'build-receipt.json').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
