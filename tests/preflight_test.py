import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('preflight', Path(__file__).resolve().parents[1] / 'scripts/preflight_engine.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

class PreflightTests(unittest.TestCase):
    def test_no_root_does_not_claim_build(self):
        result = preflight.inspect(None)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['unreal_compile'], 'not_run')

    def test_missing_root_is_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIn('Build.version is absent', preflight.inspect(root)['blockers'][0])

    def test_malformed_version_is_blocked(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'Engine/Build/Build.version'
            path.parent.mkdir(parents=True)
            path.write_text('not json')
            self.assertIn('Cannot parse engine version', preflight.inspect(root)['blockers'][0])

    def test_inventory_never_means_compile_pass(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Path(root) / 'Engine'
            version = engine / 'Build/Build.version'
            version.parent.mkdir(parents=True)
            version.write_text(json.dumps({'MajorVersion':5, 'MinorVersion':7}))
            for relative in preflight.HEADERS + ['Build/BatchFiles/RunUAT.bat', 'Binaries/Win64/UnrealEditor.exe']:
                file = engine / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text('inventory fixture, not an engine')
            (engine / 'Binaries/ThirdParty/CEF3/Win64').mkdir(parents=True)
            result = preflight.inspect(root)
            self.assertEqual(result['status'], 'inventory_pass_compile_pending')
            self.assertEqual(result['unreal_compile'], 'not_run')
            self.assertEqual(result['unreal_ui'], 'not_run')
            for major, minor in [(4,18),(4,26),(5,5),(5,7),(5,8)]:
                with self.subTest(major=major, minor=minor):
                    version.write_text(json.dumps({'MajorVersion':major, 'MinorVersion':minor}))
                    target = 'UE4Editor' if major == 4 else 'UnrealEditor'
                    (engine / f'Binaries/Win64/{target}.exe').write_text('synthetic editor inventory')
                    result = preflight.inspect(root)
                    self.assertEqual(result['status'], 'inventory_pass_compile_pending')
                    self.assertEqual(result['unreal_game_compile'], 'not_run')
                    self.assertEqual(result['packaged_game'], 'not_run')
                    self.assertEqual('-StrictIncludes' in result['build_command_template'], (major,minor) != (4,18))
            for major, minor in [(4,19),(4,27),(5,3),(5,6),(5,9),(6,0)]:
                with self.subTest(major=major, minor=minor):
                    version.write_text(json.dumps({'MajorVersion':major, 'MinorVersion':minor}))
                    result = preflight.inspect(root)
                    self.assertEqual(result['status'], 'blocked')
                    self.assertIn('explicit Win64 build matrix', result['blockers'][0])

    def test_expected_version_cannot_silently_select_another_matrix_engine(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Path(root) / 'Engine'
            path = engine / 'Build/Build.version'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'MajorVersion':5, 'MinorVersion':7}))
            result = preflight.inspect(root, '5.8')
            self.assertEqual(result['status'], 'blocked')
            self.assertIn('does not match requested 5.8', result['blockers'][0])

if __name__ == '__main__':
    unittest.main()
