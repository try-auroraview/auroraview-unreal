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


def coff_archive():
    object_bytes = struct.pack('<H', 0x8664) + b'\0' * 30
    member = (b'fixture.obj/'.ljust(16) + b'0'.ljust(12) + b'0'.ljust(6) + b'0'.ljust(6)
              + b'0'.ljust(8) + str(len(object_bytes)).encode().ljust(10) + b'`\n')
    return b'!<arch>\n' + member + object_bytes


class BuildPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.template.cleanup)
        cls.template_source = Path(cls.template.name) / 'source'
        cls.template_engine = Path(cls.template.name) / 'engine'
        source, engine = cls.template_source, cls.template_engine
        source.mkdir()
        write(source / 'AuroraView.uplugin', json.dumps({'FileVersion': 3, 'SupportedTargetPlatforms': ['Win64'],
            'Modules': [{'Name': builder.MODULE, 'Type': 'Editor', 'LoadingPhase': 'Default',
                         'PlatformAllowList': ['Win64'], 'TargetAllowList': ['Editor']},
                        {'Name': builder.RUNTIME_MODULE, 'Type': 'Runtime', 'LoadingPhase': 'Default',
                         'PlatformAllowList': ['Win64']}]}))
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
        # The template's .git directory is copied below; background maintenance
        # must not create/remove lock files while copytree enumerates it.
        for arguments in [('init', '--quiet'), ('config', 'user.name', 'loonghao'),
                          ('config', 'user.email', 'hal.long@outlook.com'),
                          ('config', 'maintenance.auto', 'false'), ('config', 'gc.auto', '0'), ('add', '.'),
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

    def fake_uat(self, command, source, log_path, environment_overrides):
        package = Path(next(value[len('-Package='):] for value in command if value.startswith('-Package=')))
        self.assertFalse(package.exists(), 'UAT must receive a fresh package destination')
        package.mkdir()
        for name in ['Resources', 'ThirdParty']:
            shutil.copytree(source / name, package / name)
        for name in ['AuroraView.uplugin', 'LICENSE']:
            shutil.copy2(source / name, package / name)
        policy = builder.preflight_engine.engine_policy(json.loads(self.version.read_text()))
        editor = policy['editor_target']
        binaries = {name: f'{editor}-{name}.dll' for name in [builder.MODULE, builder.RUNTIME_MODULE]}
        for name in binaries.values():
            write(package / 'Binaries/Win64' / name, pe_dll())
        write(package / f'Binaries/Win64/{editor}.modules', json.dumps({
            'BuildId': 'fixture-engine-build', 'Modules': binaries}))
        host = package / 'HostProject'
        host_plugin = host / 'Plugins/AuroraView'
        for configuration in ['Development', 'Shipping']:
            if policy['legacy_receipts']:
                suffix = '' if configuration == 'Development' else '-Win64-Shipping'
                relative = Path(f'Binaries/Win64/UE4-{builder.RUNTIME_MODULE}{suffix}.lib')
                data = coff_archive()
                write(host_plugin / relative, data)
                write(package / relative, data)
                receipt_name = 'UE4Game.target' if configuration == 'Development' else 'UE4Game-Win64-Shipping.target'
                write(host_plugin / 'Binaries/Win64' / receipt_name, json.dumps({
                    'TargetName': 'UE4Game', 'Platform': 'Win64', 'Configuration': configuration,
                    'BuildProducts': [{'Path': str(host_plugin / relative), 'Type': 'StaticLibrary'}]}))
            else:
                intermediate = 'UE4' if policy['version'].startswith('4.') else policy['game_target']
                relative = Path(f'Intermediate/Build/Win64/x64/{intermediate}/{configuration}/{builder.RUNTIME_MODULE}')
                object_name = f'Module.{builder.RUNTIME_MODULE}.cpp.obj'
                object_data = struct.pack('<H', 0x8664) + b'\0' * 30
                precompiled_name = builder.RUNTIME_MODULE + '.precompiled'
                for root in [host_plugin, package]:
                    write(root / relative / object_name, object_data)
                    write(root / relative / precompiled_name, json.dumps({'OutputFiles': [object_name]}))
                manifest = builder.ET.Element('BuildManifest')
                products = builder.ET.SubElement(manifest, 'BuildProducts')
                for name in [precompiled_name, object_name]:
                    builder.ET.SubElement(products, 'string').text = str(host_plugin / relative / name)
                write(host / f'Saved/Manifest-{policy["game_target"]}-Win64-{configuration}.xml',
                      builder.ET.tostring(manifest))
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
        for name, value in builder.BUILD_ENVIRONMENT.items():
            self.assertEqual(receipt['environment_overrides'][name], value)
        self.assertEqual(receipt['unreal_game_compile'], 'pass')
        self.assertEqual(receipt['packaged_game'], 'not_run')
        self.assertEqual([target['configuration'] for target in receipt['game_targets']], ['Development', 'Shipping'])
        self.assertTrue((self.output / 'HostProject').is_dir())
        self.assertFalse((self.output / 'Package/HostProject').exists())
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

    def test_yearless_ue58_toolchain_log_preserves_actual_version(self):
        log = self.root / 'ue58.log'
        log.write_text(TOOLCHAIN_LOG.replace('Visual Studio 2022 ', 'Visual Studio '), encoding='utf-8')
        evidence = builder.compiler_evidence(log)
        self.assertEqual(evidence[0]['compiler'], 'Visual Studio')
        self.assertEqual(evidence[0]['toolchain_version'], '14.44.35221')
        self.assertEqual(evidence[0]['windows_sdk_version'], '10.0.26100.0')

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

    def test_editor_binaries_do_not_prove_game_runtime_compilation(self):
        self.mutate_package = lambda package: (package / 'HostProject/Saved/Manifest-UnrealGame-Win64-Shipping.xml').unlink()
        self.assert_failed(self.build(), 'Cannot read actual Game build manifest')

    def test_runtime_editor_dll_is_required_alongside_editor_facade(self):
        self.mutate_package = lambda package: (package / 'Binaries/Win64/UnrealEditor-AuroraViewRuntime.dll').unlink()
        self.assert_failed(self.build(), 'Required native DLL is absent')

    def test_editor_only_descriptor_cannot_satisfy_runtime_contract(self):
        path = self.source / 'AuroraView.uplugin'
        descriptor = json.loads(path.read_text())
        descriptor['Modules'] = descriptor['Modules'][:1]
        write(path, json.dumps(descriptor))
        self.assert_failed(self.build(), 'both Win64 Runtime and Editor modules')
        self.runner.assert_not_called()

    def test_object_files_without_reusable_runtime_manifest_do_not_form_a_package(self):
        def change(package):
            path = package / 'HostProject/Saved/Manifest-UnrealGame-Win64-Development.xml'
            tree = builder.ET.parse(path)
            products = tree.find('BuildProducts')
            for element in list(products):
                if element.text.endswith('.precompiled'):
                    products.remove(element)
            path.write_bytes(builder.ET.tostring(tree.getroot()))
        self.mutate_package = change
        self.assert_failed(self.build(), 'No packaged Runtime library or precompiled manifest')

    def test_shipping_manifest_cannot_reuse_development_runtime_products(self):
        def change(package):
            path = package / 'HostProject/Saved/Manifest-UnrealGame-Win64-Shipping.xml'
            write(path, path.read_bytes().replace(b'Shipping', b'Development'))
        self.mutate_package = change
        self.assert_failed(self.build(), 'does not belong to the requested Game target')

    def test_game_native_output_cannot_be_replaced_with_source_or_text(self):
        def change(package):
            for root in [package, package / 'HostProject/Plugins/AuroraView']:
                write(root / 'Intermediate/Build/Win64/x64/UnrealGame/Development/AuroraViewRuntime/Module.AuroraViewRuntime.cpp.obj',
                      b'not a native object file, even with consistent package hashes')
        self.mutate_package = change
        self.assert_failed(self.build(), 'Not a native Win64 COFF object')

    def test_game_precompiled_manifest_cannot_reference_missing_object(self):
        self.mutate_package = lambda package: (package /
            'Intermediate/Build/Win64/x64/UnrealGame/Shipping/AuroraViewRuntime/Module.AuroraViewRuntime.cpp.obj').unlink()
        self.assert_failed(self.build(), 'Runtime precompiled object is absent')

    def test_require_clean_rejects_dirty_checkout_before_uat(self):
        write(self.source / 'Resources/untracked.js', '// untracked fixture\n')
        receipt = builder.build(self.engine, self.output, self.source, require_clean=True)
        self.assert_failed(receipt, 'clean source checkout is required')
        self.runner.assert_not_called()

    def test_each_explicit_version_selects_actual_editor_and_game_targets(self):
        for index, version in enumerate(builder.preflight_engine.SUPPORTED_VERSIONS):
            with self.subTest(version=version):
                self.output = self.root / f'version-{index}'
                major, minor = map(int, version.split('.'))
                write(self.version, json.dumps({'MajorVersion': major, 'MinorVersion': minor}))
                editor = 'UE4Editor' if major == 4 else 'UnrealEditor'
                write(self.engine / f'Engine/Binaries/Win64/{editor}.exe', 'synthetic engine fixture')
                write(self.engine / f'Engine/Binaries/Win64/{editor}.modules', json.dumps({'BuildId': 'fixture-engine-build'}))
                receipt = builder.build(self.engine, self.output, self.source, expected_version=version)
                self.assertEqual(receipt['status'], 'pass', receipt['errors'])
                self.assertEqual('-StrictIncludes' in receipt['build_command'], version != '4.18')
                self.assertEqual('-VS2019' in receipt['build_command'], version == '4.26')
                self.assertEqual(receipt['game_targets'][0]['target'], 'UE4Game' if major == 4 else 'UnrealGame')
                self.assertEqual(receipt['packaged_game'], 'not_run')

    def test_legacy_compiler_not_logged_is_explicit_instead_of_invented(self):
        write(self.version, json.dumps({'MajorVersion': 4, 'MinorVersion': 18}))
        write(self.engine / 'Engine/Binaries/Win64/UE4Editor.exe', 'synthetic engine fixture')
        write(self.engine / 'Engine/Binaries/Win64/UE4Editor.modules', json.dumps({'BuildId': 'fixture-engine-build'}))
        self.uat_log = 'fixture legacy UBT emitted no compiler identity\n'
        receipt = self.build()
        self.assertEqual(receipt['status'], 'pass', receipt['errors'])
        self.assertEqual(receipt['compiler_toolchains'], [])
        self.assertEqual(receipt['compiler_evidence_status'], 'not_logged_by_legacy_ubt')

    def test_ue426_game_manifest_can_record_configuration_named_static_libraries(self):
        write(self.version, json.dumps({'MajorVersion': 4, 'MinorVersion': 26}))
        write(self.engine / 'Engine/Binaries/Win64/UE4Editor.exe', 'synthetic engine fixture')
        write(self.engine / 'Engine/Binaries/Win64/UE4Editor.modules', json.dumps({'BuildId': 'fixture-engine-build'}))
        def change(package):
            plugin = package / 'HostProject/Plugins/AuroraView'
            for configuration in ['Development', 'Shipping']:
                suffix = '' if configuration == 'Development' else '-Win64-Shipping'
                relative = Path(f'Binaries/Win64/UE4-AuroraViewRuntime{suffix}.lib')
                for root in [package, plugin]:
                    write(root / relative, coff_archive())
                manifest = builder.ET.Element('BuildManifest')
                products = builder.ET.SubElement(manifest, 'BuildProducts')
                builder.ET.SubElement(products, 'string').text = str(plugin / relative)
                write(package / f'HostProject/Saved/Manifest-UE4Game-Win64-{configuration}.xml', builder.ET.tostring(manifest))
        self.mutate_package = change
        receipt = self.build()
        self.assertEqual(receipt['status'], 'pass', receipt['errors'])
        self.assertEqual(receipt['unreal_game_compile'], 'pass')


if __name__ == '__main__':
    unittest.main()
