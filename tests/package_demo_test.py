"""Offline prerequisite integrity checks; synthetic fixtures are not native acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import package_demo


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding='utf-8')


def signature(**changes):
    result = dict(status='Valid', subject='CN=Microsoft Corporation, O=Microsoft Corporation, C=US',
                  thumbprint='A' * 40, version='14.44.35211.0',
                  product='Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.44.35211',
                  original_filename='VC_redist.x64.exe')
    return dict(result, **changes)


class MicrosoftSignatureTests(unittest.TestCase):
    def verify(self, result):
        with patch.object(package_demo.sys, 'platform', 'win32'), patch.object(package_demo.subprocess, 'run') as run:
            run.return_value.stdout = json.dumps(result)
            verified = package_demo.microsoft_installer(Path('redist with spaces.exe'))
            command = run.call_args.args[0]
            self.assertNotIn('redist with spaces.exe', command[-1])
            self.assertEqual(run.call_args.kwargs['env']['AURORAVIEW_REDIST_PATH'], 'redist with spaces.exe')
            self.assertIn('Get-AuthenticodeSignature', command[-1])
            self.assertNotIn('Start-Process', command[-1])
            return verified

    def test_verified_microsoft_signature_is_required(self):
        self.assertEqual(self.verify(signature())['version'], '14.44.35211.0')
        for invalid in [signature(status='HashMismatch'), signature(status='NotSigned'),
                        signature(subject='CN=Microsoft Corporation, O=Another Company, C=US'),
                        signature(thumbprint=''), signature(version='unknown'),
                        signature(product='Microsoft Visual C++ 2015-2022 Redistributable (x86)'),
                        signature(original_filename='VC_redist.arm64.exe')]:
            with self.subTest(invalid=invalid), self.assertRaises(package_demo.build_plugin.BuildError):
                self.verify(invalid)

    def test_signature_process_failure_rejects_installer(self):
        with patch.object(package_demo.sys, 'platform', 'win32'), patch.object(package_demo.subprocess, 'run') as run:
            run.side_effect = subprocess.CalledProcessError(1, 'powershell')
            with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'Unable to verify'):
                package_demo.microsoft_installer(Path('redist.exe'))


class OfflineBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'Source'
        self.engine = self.root / 'EngineInstallation'
        self.game = self.root / 'GameRun'
        self.output = self.root / 'Bundle'
        self.installer = self.engine / 'Engine/Extras/Redist/en-us/vc_redist.x64.exe'
        write(self.installer, 'synthetic signed-installer input; never executable')
        version_file = self.engine / 'Engine/Build/Build.version'
        version = dict(MajorVersion=5, MinorVersion=7, PatchVersion=4)
        write(version_file, json.dumps(version))
        modules_file = self.engine / 'Engine/Binaries/Win64/UnrealEditor.modules'
        write(modules_file, json.dumps({'BuildId': 'fixture-build'}))
        self.identity = dict(dirty=False, commit='fixture-commit', tree='fixture-tree',
                             working_files_sha256={'fixture': 'hash'})
        executable = 'Windows/AuroraViewGameFixture/Binaries/Win64/AuroraViewGameFixture.exe'
        write(self.game / 'PackagedGame' / executable, 'synthetic receipt-bound game input')
        self.receipt = dict(status='passed', packaged_game='pass', source=self.identity,
                            stage={'files_sha256': package_demo.validate_game.inventory(self.game / 'PackagedGame')},
                            runtime=dict(exit_code=0, forced_cleanup=False, actions={'demo_scene': {'height': 0}}),
                            compiler_toolchains=[dict(toolchain_version='14.44.35225')],
                            engine=dict(root=str(self.engine), version=version, build_id='fixture-build',
                                        version_sha256=package_demo.build_plugin.sha256(version_file),
                                        modules_sha256=package_demo.build_plugin.sha256(modules_file)))
        for name in ['python/auroraview_unreal/__init__.py', 'Resources/live_demo.html', 'LICENSE']:
            write(self.source / name, 'synthetic source file')
        for name in ['run_demo.py', 'demo_tools.py', 'owner_dispatch.py', 'build_plugin.py', 'validate_game.py',
                     'preflight_engine.py', 'pe_evidence.py']:
            write(self.source / 'scripts' / name, '# synthetic launcher input\n')
        self.identity['files'] = package_demo.validate_game.inventory(self.source)
        self.identity['working_files_sha256'] = hashlib.sha256(
            json.dumps(self.identity['files'], sort_keys=True).encode()).hexdigest()
        write(self.game / 'evidence/game-validation.json', json.dumps(self.receipt))

    def test_changed_source_bytes_are_rejected_even_when_the_source_is_restored(self):
        source_file = self.source / 'Resources/live_demo.html'
        original = source_file.read_bytes()
        copytree = package_demo.shutil.copytree

        def verify(_installer):
            write(source_file, 'different bytes copied under the original source identity')
            return signature()

        def copy_then_restore(source, target, **kwargs):
            result = copytree(source, target, **kwargs)
            if source == self.source / 'Resources':
                source_file.write_bytes(original)
            return result

        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', side_effect=verify), \
                patch.object(package_demo.shutil, 'copytree', side_effect=copy_then_restore):
            with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'Copied source'):
                package_demo.package_demo(self.game, self.output)
        self.assertEqual(package_demo.validate_game.inventory(self.source), self.identity['files'])
        self.assertFalse(self.output.with_name(self.output.name + '.zip').exists())

    def test_new_source_resource_after_the_initial_inventory_is_rejected(self):
        def verify(_installer):
            write(self.source / 'Resources/late.js', 'not present in the accepted source inventory')
            return signature()

        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', side_effect=verify):
            with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'Copied source'):
                package_demo.package_demo(self.game, self.output)
        self.assertFalse(self.output.with_name(self.output.name + '.zip').exists())

    def test_replaced_game_receipt_cannot_become_the_bundle_verification_digest(self):
        receipt_path = self.game / 'evidence/game-validation.json'
        original = receipt_path.read_bytes()
        for name, replacement in [('failed', json.dumps(dict(self.receipt, status='failed')).encode()),
                                  ('same-json-new-bytes', original + b'\n')]:
            with self.subTest(replacement=name):
                receipt_path.write_bytes(original)
                output = self.root / ('Bundle-' + name)

                def verify(_installer):
                    receipt_path.write_bytes(replacement)
                    return signature()

                with patch.object(package_demo, 'ROOT', self.source), \
                        patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                        patch.object(package_demo, 'microsoft_installer', side_effect=verify):
                    with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'Game receipt changed'):
                        package_demo.package_demo(self.game, output)
                self.assertFalse(output.with_name(output.name + '.zip').exists())

    def test_mutation_during_zip_creation_removes_the_owned_archive(self):
        receipt_path = self.game / 'evidence/game-validation.json'
        original = receipt_path.read_bytes()
        zip_write = package_demo.zipfile.ZipFile.write
        for mutation in ['source', 'receipt']:
            with self.subTest(mutation=mutation):
                receipt_path.write_bytes(original)
                output = self.root / ('Bundle-zip-' + mutation)

                def mutate(bundle, filename, *args, **kwargs):
                    if filename == output / 'Resources/live_demo.html':
                        if mutation == 'source':
                            write(filename, 'changed after the copied source inventory')
                        else:
                            receipt_path.write_bytes(original + b'\n')
                    return zip_write(bundle, filename, *args, **kwargs)

                with patch.object(package_demo, 'ROOT', self.source), \
                        patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                        patch.object(package_demo, 'microsoft_installer', return_value=signature()), \
                        patch.object(package_demo.zipfile.ZipFile, 'write', new=mutate):
                    with self.assertRaisesRegex(package_demo.build_plugin.BuildError,
                                                'Copied source ZIP' if mutation == 'source' else 'Game receipt changed'):
                        package_demo.package_demo(self.game, output)
                self.assertFalse(output.with_name(output.name + '.zip').exists())
                self.assertTrue((output / 'Resources/live_demo.html').is_file())
                self.assertEqual(package_demo.validate_game.inventory(self.source), self.identity['files'])

    def test_zip_creation_collision_preserves_the_other_archive(self):
        zip_path = self.output.with_name(self.output.name + '.zip')
        existing = b'another invocation owns these bytes'

        def verify(_installer):
            zip_path.write_bytes(existing)
            return signature()

        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', side_effect=verify):
            with self.assertRaises(FileExistsError):
                package_demo.package_demo(self.game, self.output)
        self.assertEqual(zip_path.read_bytes(), existing)

    def test_interrupted_zip_write_removes_owned_archive_and_preserves_inputs(self):
        interrupted = KeyboardInterrupt('synthetic archive cancellation')
        receipt_path = self.game / 'evidence/game-validation.json'
        receipt_bytes = receipt_path.read_bytes()
        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', return_value=signature()), \
                patch.object(package_demo.zipfile.ZipFile, 'write', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt) as caught:
                package_demo.package_demo(self.game, self.output)
        self.assertIs(caught.exception, interrupted)
        self.assertEqual(package_demo.validate_game.inventory(self.source), self.identity['files'])
        self.assertEqual(receipt_path.read_bytes(), receipt_bytes)
        self.assertTrue((self.output / 'Resources/live_demo.html').is_file())
        zip_path = self.output.with_name(self.output.name + '.zip')
        self.assertFalse(zip_path.exists(),
                         f'Canceled archive retained an owned ZIP of {zip_path.stat().st_size if zip_path.exists() else 0} bytes')

    def test_source_copy_ignores_python_cache_using_the_same_inventory_filter(self):
        for name in ['__pycache__/cached.pyc', 'module.pyc', 'cache.pyc/nested.js']:
            write(self.source / 'Resources' / name, 'ignored source cache')
        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', return_value=signature()):
            result = package_demo.package_demo(self.game, self.output)
        with zipfile.ZipFile(result['archive']) as bundle:
            self.assertFalse(any('__pycache__' in name or '.pyc' in name for name in bundle.namelist()))

    def test_bundle_hash_binds_manual_microsoft_prerequisite(self):
        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', return_value=signature()):
            result = package_demo.package_demo(self.game, self.output)
        manifest = json.loads((self.output / 'demo-package.json').read_text(encoding='utf-8'))
        prerequisite = manifest['prerequisites']['vc_redist_x64']
        self.assertEqual(prerequisite['path'], 'Prerequisites/vc_redist.x64.exe')
        self.assertEqual(prerequisite['sha256'], manifest['files_sha256'][prerequisite['path']])
        self.assertEqual(prerequisite['minimum_msvc_family'], '14.44')
        self.assertEqual(prerequisite['observed_toolchain_versions'], ['14.44.35225'])
        self.assertIs(prerequisite['automatic_install'], False)
        self.assertEqual(manifest['verification']['game_receipt_sha256'],
                         hashlib.sha256((self.game / 'evidence/game-validation.json').read_bytes()).hexdigest())
        self.assertTrue(Path(result['archive']).is_file())
        with zipfile.ZipFile(result['archive']) as bundle:
            for name, digest in self.identity['files'].items():
                copied = bundle.read(self.output.name + '/' + name)
                self.assertEqual(hashlib.sha256(copied).hexdigest(), digest)
        readme = (self.output / 'README.txt').read_text(encoding='utf-8')
        self.assertIn(prerequisite['path'], readme)
        self.assertIn('never installs prerequisites automatically', readme)

    def test_changed_installed_engine_is_rejected_before_signature_check(self):
        write(self.engine / 'Engine/Build/Build.version', '{}')
        with patch.object(package_demo, 'microsoft_installer') as verify:
            with self.assertRaises((ValueError, package_demo.build_plugin.BuildError)):
                package_demo.prerequisite(self.receipt)
            verify.assert_not_called()

    def test_dotted_bundle_name_matches_archive_and_rejects_existing_zip(self):
        self.output = self.root / 'AuroraView-UE5.7-fixture-Demo'
        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', return_value=signature()):
            result = package_demo.package_demo(self.game, self.output)
        expected = self.root / 'AuroraView-UE5.7-fixture-Demo.zip'
        self.assertEqual(Path(result['archive']), expected)
        self.assertTrue(expected.is_file())
        with zipfile.ZipFile(expected) as bundle:
            self.assertTrue(all(name.startswith(self.output.name + '/') for name in bundle.namelist()))
        # The exact archive collision must fail before creating a new directory.
        other = self.root / 'AuroraView-UE5.8-fixture-Demo'
        existing = other.with_name(other.name + '.zip')
        existing.write_bytes(b'existing artifact')
        with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'new demo bundle'):
            package_demo.package_demo(self.game, other)
        self.assertEqual(existing.read_bytes(), b'existing artifact')
        self.assertFalse(other.exists())

    def test_public_bundle_excludes_debug_symbols_and_private_provenance(self):
        archive = self.game / 'PackagedGame'
        native = {
            'Windows/AuroraViewGameFixture/Binaries/Win64/AuroraViewGameFixture.exe': 'native game',
            'Windows/AuroraViewGameFixture/Binaries/Win64/Runtime.dll': 'native runtime',
            'Windows/Engine/Binaries/ThirdParty/CEF3/Win64/libcef.dll': 'browser runtime',
            'Windows/Engine/Binaries/ThirdParty/CEF3/Win64/Resources/resources.pak': 'browser resources',
            'Windows/AuroraViewGameFixture/Content/Paks/Demo.pak': 'cooked game',
        }
        private = {
            'Windows/AuroraViewGameFixture/Binaries/Win64/AuroraViewGameFixture.PDB': 'private-symbol-user-path',
            'Windows/Engine/Binaries/Win64/Runtime.pdb': 'private-symbol-toolchain-path',
            'Windows/Manifest_DebugFiles_Win64.txt': 'private-debug-index',
        }
        for name, content in dict(native, **private).items():
            write(archive / name, content)
        self.identity['root'] = str(self.source / 'private-user-root')
        self.receipt['compiler_toolchains'][0]['toolchain_path'] = 'private-compiler-path'
        self.receipt['runtime']['private_token'] = 'private-runtime-token'
        self.receipt['stage']['files_sha256'] = package_demo.validate_game.inventory(archive)
        receipt_path = self.game / 'evidence/game-validation.json'
        write(receipt_path, json.dumps(self.receipt))
        with patch.object(package_demo, 'ROOT', self.source), \
                patch.object(package_demo.build_plugin, 'git_identity', return_value=self.identity), \
                patch.object(package_demo, 'microsoft_installer', return_value=signature()):
            result = package_demo.package_demo(self.game, self.output)
        manifest = json.loads((self.output / 'demo-package.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['engine_build_id'], 'fixture-build')
        self.assertEqual(manifest['engine_build_version_sha256'], self.receipt['engine']['version_sha256'])
        self.assertEqual(manifest['verification']['game_receipt_sha256'], package_demo.build_plugin.sha256(receipt_path))
        with zipfile.ZipFile(result['archive']) as bundle:
            entries = {name.partition('/')[2]: bundle.read(name) for name in bundle.namelist()}
        for name in private:
            self.assertNotIn('Game/' + name, entries)
            self.assertNotIn('Game/' + name, manifest['files_sha256'])
            self.assertTrue((archive / name).is_file())
        for name, content in native.items():
            self.assertEqual(entries['Game/' + name], content.encode('utf-8'))
            self.assertEqual(manifest['files_sha256']['Game/' + name], package_demo.build_plugin.sha256(archive / name))
        public_metadata = entries['demo-package.json'] + entries['README.txt']
        for forbidden in [str(self.source), str(self.engine), str(receipt_path), 'private-compiler-path',
                          'private-runtime-token', 'private-symbol-user-path', 'private-debug-index']:
            self.assertNotIn(forbidden.encode('utf-8'), public_metadata)
        self.assertFalse(any(Path(name).name == 'game-validation.json' for name in entries))

    def test_older_runtime_family_is_rejected(self):
        with patch.object(package_demo, 'microsoft_installer', return_value=signature(version='14.43.99999.0')):
            with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'older than'):
                package_demo.prerequisite(self.receipt)

    def test_mutation_during_signature_check_is_rejected(self):
        def replace_file(path):
            write(path, 'replaced after signature verification')
            return signature()
        with patch.object(package_demo, 'microsoft_installer', side_effect=replace_file):
            with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'changed during signature'):
                package_demo.prerequisite(self.receipt)

    def test_missing_actual_toolchain_evidence_is_rejected(self):
        del self.receipt['compiler_toolchains']
        with patch.object(package_demo, 'microsoft_installer') as verify:
            with self.assertRaisesRegex(package_demo.build_plugin.BuildError, 'Actual Game MSVC'):
                package_demo.prerequisite(self.receipt)
            verify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
