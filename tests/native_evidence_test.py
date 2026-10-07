import copy
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('native_evidence',ROOT/'scripts/native_evidence.py')
evidence=importlib.util.module_from_spec(spec);spec.loader.exec_module(evidence)
class NativeEvidenceTests(unittest.TestCase):
    def setUp(self): self.data=evidence.seed()
    def test_seed_is_pending_not_native_success(self):
        self.assertEqual(evidence.validate(self.data),[])
        self.assertTrue(evidence.validate(self.data,require_complete=True))
        self.assertEqual(self.data['build']['status'],'pending')
    def test_missing_implementation_cannot_be_not_run_or_pass(self):
        case=next(c for c in self.data['cases'] if c['implementation']=='unimplemented')
        for status in ('not_run','pass','blocked'):
            case['status']=status
            self.assertTrue(evidence.validate(self.data))
    def test_pass_without_actual_host_and_media_is_rejected(self):
        self.data['cases'][0]['status']='pass'
        errors=evidence.validate(self.data)
        self.assertTrue(any('actual host state' in e for e in errors))
        self.assertTrue(any('timepoints' in e for e in errors))
    def test_source_or_html_assertion_is_not_native_proof(self):
        case=self.data['cases'][0];case['status']='pass'
        case['assertions']=[{'observation':'source_scan','name':'fake','actual':True,'expected':True}]
        self.assertTrue(any('HTML/source-only' in e for e in evidence.validate(self.data)))
    def test_false_assertion_cannot_claim_pass(self):
        case=self.data['cases'][0];case['status']='pass'
        case['assertions']=[{'observation':'editor_state','name':'mismatch','actual':1,'expected':2}]
        self.assertTrue(any('assertion did not pass' in e for e in evidence.validate(self.data)))
    def test_missing_case_is_rejected(self):
        self.data['cases'].pop()
        self.assertTrue(any('every registered case' in e for e in evidence.validate(self.data)))
    def test_absolute_media_paths_are_rejected(self):
        case=self.data['cases'][0];case['status']='pass';case['media']=[{'file':'/private/capture.mp4'}]
        self.assertTrue(evidence.validate(self.data))

# Synthetic validator fixtures only. Never deliver these bytes as native evidence.
import hashlib
import json
import tempfile
from unittest.mock import patch

class EvidencePolicyTests(unittest.TestCase):
    def test_registry_policy_cannot_be_downgraded(self):
        for field,value in [('required',False),('implementation','unimplemented'),('stage',99),('expected_host_state',{'unrelated':True}),('automation',None),('steps',['skip'])]:
            data=evidence.seed();data['cases'][0][field]=value
            self.assertTrue(evidence.validate(data),field)
        data=evidence.seed()
        for case in data['cases']:case['required']=False
        self.assertTrue(evidence.validate(data,require_complete=True))
        del data['cases'][0]['required'];self.assertTrue(evidence.validate(data))
        data=evidence.seed();case=next(c for c in data['cases'] if c['implementation']=='unimplemented')
        case.update(implementation='source_candidate',status='pending');self.assertTrue(evidence.validate(data))
    def test_missing_duplicate_and_unknown_ids(self):
        for mode in ('missing','duplicate','unknown'):
            data=evidence.seed()
            if mode=='missing':data['cases'].pop()
            elif mode=='duplicate':data['cases'].append(copy.deepcopy(data['cases'][0]))
            else:data['cases'][0]['id']='not.registered'
            self.assertTrue(evidence.validate(data),mode)
    def test_nonfinite_json_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'not-evidence.json';path.write_text('{"bad": Infinity}')
            with self.assertRaises(ValueError):evidence.read_json(path)
            path.write_text('{"status":"fail","status":"pass"}')
            with self.assertRaises(ValueError):evidence.read_json(path)

class EvidenceRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'source';self.root.mkdir()
        self.output=Path(self.tmp.name)/'synthetic-only';self.output.mkdir()
        for name in ('acceptance/cases.json','acceptance/evidence.schema.json','ThirdParty/AuroraViewCore/manifest.json'):
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
        self.rootpatch=patch.object(evidence,'ROOT',self.root);self.rootpatch.start();self.addCleanup(self.rootpatch.stop)
        self.identity={'commit':'a'*40,'tree':'b'*40,'dirty':False}
        self.gitpatch=patch.object(evidence,'git_identity',return_value=self.identity.copy());self.gitpatch.start();self.addCleanup(self.gitpatch.stop)
        self.data=evidence.seed();self.data['created_utc']='2026-10-07T00:00:00Z'
        self.data['engine']={'version':'5.7.1','build_id':'SYNTHETIC-UNIT-TEST-NOT-NATIVE','platform':'Win64'}
        self.data['recording']={'started_utc':'2026-10-07T00:01:00Z','host_seconds_at_video_zero':100.0}
        def put(name,value):
            content=json.dumps(value).encode() if isinstance(value,dict) else value
            (self.output/name).write_bytes(content)
            return {'file':name,'sha256':hashlib.sha256(content).hexdigest()}
        self.put=put
        import struct
        fake_pe=bytearray(160);fake_pe[:2]=b'MZ';struct.pack_into('<I',fake_pe,0x3c,64)
        fake_pe[64:68]=b'PE\0\0';struct.pack_into('<H',fake_pe,68,0x8664)
        struct.pack_into('<H',fake_pe,86,0x2000);struct.pack_into('<H',fake_pe,88,0x20b)
        fake_pe[100:]=b'SYNTHETIC HEADER ONLY; NOT EXECUTABLE NATIVE EVIDENCE'
        dll=put('synthetic.dll',bytes(fake_pe))
        log=put('build.log',b'SYNTHETIC ONLY')
        results=[]
        for case in self.data['cases']:
            if case['implementation']=='unimplemented':continue
            case.update(status='pass',actual_host_state=copy.deepcopy(case['expected_host_state']),started_utc='2026-10-07T00:01:01Z',finished_utc='2026-10-07T00:01:03Z',host_time_s=102.0)
            case['assertions']=[{'name':k,'observation':'editor_state','operator':'equal','actual':v,'expected':v} for k,v in case['expected_host_state'].items()]
            case['media']=[{**put('synthetic.mp4',b'NOT MEDIA; validator unit test only'),'kind':'video','video_time_s':2.0,'host_time_s':102.0}]
            for name in (case['automation'] or '').split(';'):
                if name.strip():results.append({'fullTestPath':name.strip(),'state':'Success'})
        receipt={'run_id':self.data['run_id'],'source':self.data['source'],'engine':self.data['engine'],'dll':dll,'log':log,'exit_code':0,'target':'UnrealEditor Win64 Development','compiler':{'name':'synthetic','version':'synthetic','command':'not executed'}}
        self.data['build']={'status':'pass','dll':dll,'log':log,'receipt':put('build-receipt.json',receipt),'automation_report':put('automation.json',{'tests':results,'failed':0})}
        self.trusted={k:copy.deepcopy(self.data[k]) for k in ('run_id','source','engine','build')}
        self.trusted['source_files']={p.relative_to(self.root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
    def check(self):return evidence.validate(self.data,self.output,True,self.trusted)
    def test_complete_synthetic_fixture_exercises_validator_only(self):self.assertEqual(self.check(),[])
    def test_separate_trust_anchor_required(self):self.assertTrue(evidence.validate(self.data,self.output,True))
    def test_unrelated_identity_and_engine_rejected(self):
        for key,field,value in [('source','commit','c'*40),('source','tree','d'*40),('source','core_commit','e'*40),('engine','version','not UE'),('engine','platform','Mac')]:
            original=self.data[key][field];self.data[key][field]=value;self.assertTrue(self.check(),field);self.data[key][field]=original
    def test_wrong_actual_missing_criteria_and_bool_integer_coercion_rejected(self):
        case=self.data['cases'][0]
        case['actual_host_state']={'unrelated':True};self.assertTrue(self.check())
        case['actual_host_state']=copy.deepcopy(case['expected_host_state']);case['assertions']=[];self.assertTrue(self.check())
        self.assertFalse(evidence.strict_equal(True,1));self.assertFalse(evidence.strict_equal(1,1.0))
        case['assertions']=[{'name':'unrelated','observation':'editor_state','actual':True,'expected':1}];self.assertTrue(self.check())
    def test_arbitrary_tolerance_is_rejected(self):
        self.data['cases'][0]['assertions'][0].update(operator='near',tolerance=float('inf'));self.assertTrue(self.check())
    def test_bad_clocks_rejected(self):
        case=self.data['cases'][0]
        for key,value in [('started_utc','nonsense'),('finished_utc','2026-10-06T00:00:00Z'),('started_utc','2026-10-07T00:01:00'),('host_time_s',None),('host_time_s',float('inf')),('host_time_s',-1),('host_time_s',500),('host_time_s',10**1000)]:
            old=case[key];case[key]=value;self.assertTrue(self.check(),key);case[key]=old
        case['media'][0]['video_time_s']=float('inf');self.assertTrue(self.check())
        case['media'][0]['video_time_s']=3;self.assertTrue(self.check())
        case['media'][0]['video_time_s']=2;self.data['recording']['host_seconds_at_video_zero']=None;self.assertTrue(self.check())
    def test_missing_or_changed_actual_artifacts_rejected(self):
        for key in ('dll','log','receipt','automation_report'):
            file=self.output/self.data['build'][key]['file'];original=file.read_bytes();file.unlink();self.assertTrue(self.check(),key);file.write_bytes(original+b'changed');self.assertTrue(self.check(),key);file.write_bytes(original)
    def test_automation_names_results_and_compiler_metadata_required(self):
        self.data['build']['automation_report']=self.put('automation.json',{'tests':[],'failed':0})
        self.trusted['build']=copy.deepcopy(self.data['build']);self.assertTrue(self.check())
    def test_arbitrary_file_is_not_a_native_dll(self):
        self.data['build']['dll']=self.put('synthetic.dll',b'not a DLL')
        self.trusted['build']=copy.deepcopy(self.data['build'])
        self.assertTrue(any('PE32+' in e for e in self.check()))
    def test_schema_is_executed_and_unsupported_keywords_fail_closed(self):
        self.assertTrue(evidence.validate_structure({}, {'unsupported':True}))
        del self.data['cases'][0]['media'];self.assertTrue(self.check())
    def test_source_manifest_is_checked_against_actual_bytes(self):
        (self.root/'unexpected.txt').write_text('unexpected');self.assertTrue(self.check())
    def test_archive_without_verified_receipt_is_rejected(self):
        with patch.object(evidence,'git_identity',return_value={'commit':None,'tree':None,'dirty':None}):
            self.assertTrue(any('delivery receipt' in e for e in self.check()))

    def test_verified_archive_receipt_binds_extracted_source(self):
        import tarfile
        archive=self.output/'source.tar.gz'
        with tarfile.open(archive,'w:gz') as tar:
            for name in self.trusted['source_files']:tar.add(self.root/name,arcname=name)
        archive_ref={'file':archive.name,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()}
        self.trusted['delivery_receipt']=self.put('delivery-receipt.json',{'source':self.data['source'],'source_files':self.trusted['source_files'],'archive':archive_ref})
        with patch.object(evidence,'git_identity',return_value={'commit':None,'tree':None,'dirty':None}):
            self.assertEqual(self.check(),[])
            archive.write_bytes(b'changed archive');self.assertTrue(self.check())
    def test_build_receipt_requires_exact_identity_and_compiler(self):
        receipt=json.loads((self.output/'build-receipt.json').read_text());receipt['compiler']={}
        self.data['build']['receipt']=self.put('build-receipt.json',receipt);self.trusted['build']=copy.deepcopy(self.data['build'])
        self.assertTrue(any('compiler' in e for e in self.check()))
        receipt['source']['commit']='f'*40
        self.data['build']['receipt']=self.put('build-receipt.json',receipt);self.trusted['build']=copy.deepcopy(self.data['build'])
        self.assertTrue(any('build receipt source mismatch' in e for e in self.check()))
    def test_registry_criteria_use_strict_actual_types(self):
        case=self.data['cases'][0];name='b_survives_a_close'
        case['actual_host_state'][name]=1
        assertion=next(a for a in case['assertions'] if a['name']==name);assertion.update(actual=1,expected=1)
        self.assertTrue(any('criterion' in e for e in self.check()))

class EditorAutomationSchemaTests(unittest.TestCase):
    """Execute the actual PowerShell gates using synthetic engine report schemas."""

    TEST_NAMES = (
        'AuroraView.Editor.BridgeRoundTrip',
        'AuroraView.Editor.DockedLifecycle',
        'AuroraView.Editor.DockFactoryReentrancy',
        'AuroraView.Editor.FailedDockRecovery',
        'AuroraView.Editor.TypedInspectorGuards',
        'AuroraView.Showcase.FixtureBridgeRoundTrip',
        'AuroraView.Showcase.FixtureTransform',
        'AuroraView.Showcase.NativeInteractionGuards',
        'AuroraView.Runtime.ControlReflection',
    )

    @classmethod
    def setUpClass(cls):
        cls.powershell = shutil.which('pwsh') or shutil.which('powershell')
        if not cls.powershell:
            raise unittest.SkipTest('PowerShell is required to execute the Editor report gate')

    @classmethod
    def report(cls, version):
        # FAutomatedTestResult / FAutomatedTestPassResults in the installed
        # AutomationControllerManager.h: UE4 has no InProcess or test Duration;
        # UE4.18 uses Events, while 4.26 and UE5 use execution Entries.
        tests = []
        for index, name in enumerate(cls.TEST_NAMES):
            test = {'testDisplayName': name.rsplit('.', 1)[-1], 'fullTestPath': name,
                    'state': 'Success', 'warnings': int(index < 3), 'errors': 0,
                    'artifacts': [], 'events' if version == '4.18' else 'entries': []}
            if version.startswith('5.'):
                test.update(duration=0.25, deviceInstance=['synthetic-only'])
            tests.append(test)
        report = {'succeeded': 6, 'succeededWithWarnings': 3, 'failed': 0, 'notRun': 0,
                  'totalDuration': 2.25, 'comparisonExported': False,
                  'comparisonExportDirectory': '', 'tests': tests}
        if version == '4.26':
            report.update(clientDescriptor='synthetic-only', reportCreatedOn='2026.10.08-00.00.00')
        elif version.startswith('5.'):
            report.update(inProcess=0, devices=[], reportCreatedOn='2026.10.08-00.00.00')
        return report

    def run_powershell(self, root, body, arguments):
        harness = root / 'validator-harness.ps1'
        harness.write_text('''Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$parseTokens = $null
$parseErrors = $null
$validator = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$parseTokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$functions = $validator.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -in @('Get-ReportCounter', 'Assert-AutomationReport', 'Write-JsonFile', 'Invoke-FixtureEditor')
}, $true)
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
''' + body, encoding='utf-8')
        result = subprocess.run(
            [self.powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(harness), str(ROOT / 'scripts/validate_editor.ps1'), *arguments],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def assert_reports(self, cases):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = []
            for index, (name, version, report, accepted) in enumerate(cases):
                path = root / f'report-{index}.json'
                path.write_text(json.dumps(report), encoding='utf-8')
                inputs.append({'name': name, 'version': version, 'path': str(path)})
            cases_path = root / 'cases.json'
            cases_path.write_text(json.dumps(inputs), encoding='utf-8')
            outcomes = self.run_powershell(root, '''
$cases = Get-Content -LiteralPath $args[1] -Raw | ConvertFrom-Json
$outcomes = @(foreach ($case in $cases) {
    try {
        $tests = @(Assert-AutomationReport -Path $case.path -EngineVersion $case.version)
        [pscustomobject]@{ name = $case.name; accepted = $true; tests = $tests; error = $null }
    } catch {
        [pscustomobject]@{ name = $case.name; accepted = $false; tests = @(); error = $_.Exception.Message }
    }
})
ConvertTo-Json -InputObject $outcomes -Depth 12 -Compress
''', [str(cases_path)])
        self.assertEqual(len(outcomes), len(cases))
        for case, outcome in zip(cases, outcomes):
            with self.subTest(case=case[0]):
                self.assertEqual(outcome['accepted'], case[3], outcome['error'])
                if case[3]:
                    self.assertEqual({test['fullTestPath'] for test in outcome['tests']}, set(self.TEST_NAMES))
                    self.assertEqual(len(outcome['tests']), 9)
                    self.assertTrue(all(test['errors'] == 0 and test['state'] == 'Success'
                                        for test in outcome['tests']))

    def test_actual_ue4_schema_without_inprocess_or_test_duration_passes(self):
        self.assert_reports([(version, version, self.report(version), True) for version in ('4.18', '4.26')])

    def test_ue5_still_requires_its_inprocess_counter(self):
        cases = []
        for version in ('5.5', '5.7', '5.8'):
            report = self.report(version)
            cases.append((version + '-complete', version, copy.deepcopy(report), True))
            del report['inProcess']
            cases.append((version + '-missing-inProcess', version, report, False))
        self.assert_reports(cases)

    def test_ue4_validates_inprocess_whenever_present(self):
        cases = []
        for version in ('4.18', '4.26'):
            for value in (0, 1, None, False, '0', -1, 0.5):
                report = self.report(version)
                report['inProcess'] = value
                cases.append((f'{version}-{value!r}', version, report, type(value) is int and value == 0))
        self.assert_reports(cases)

    def test_failed_unrun_or_unfinished_tests_fail_even_with_forged_success_totals(self):
        cases = []
        for version in ('4.18', '4.26', '5.7'):
            for state in ('Fail', 'NotRun', 'InProcess', 'Skipped', None):
                report = self.report(version)
                report['tests'][0]['state'] = state
                cases.append((f'{version}-{state}', version, report, False))
        self.assert_reports(cases)

    def test_complete_nine_test_roster_cannot_be_missing_or_duplicated(self):
        cases = []
        for version in ('4.18', '4.26', '5.7'):
            report = self.report(version)
            report['tests'].pop()
            report['succeeded'] -= 1
            cases.append((version + '-missing-runtime', version, report, False))
            report = self.report(version)
            report['tests'][-1] = copy.deepcopy(report['tests'][0])
            cases.append((version + '-duplicate-replaces-runtime', version, report, False))
            report = self.report(version)
            report['tests'][-1]['fullTestPath'] = 'AuroraView.Unknown'
            cases.append((version + '-unknown-replaces-runtime', version, report, False))
        self.assert_reports(cases)

    def test_error_counters_and_pass_totals_remain_strict(self):
        cases = []
        for version in ('4.18', '4.26', '5.7'):
            for field in ('failed', 'notRun', 'succeeded', 'succeededWithWarnings'):
                report = self.report(version)
                del report[field]
                cases.append((f'{version}-missing-{field}', version, report, False))
                report = self.report(version)
                report[field] += 1
                cases.append((f'{version}-wrong-{field}', version, report, False))
            for value in (1, None, '0', False, -1, 0.5):
                report = self.report(version)
                report['tests'][0]['errors'] = value
                cases.append((f'{version}-errors-{value!r}', version, report, False))
            report = self.report(version)
            del report['tests'][0]['errors']
            cases.append((version + '-missing-errors', version, report, False))
        self.assert_reports(cases)

    @unittest.skipUnless(os.name == 'nt', 'The Editor process gate uses Windows Start-Process')
    def test_editor_process_must_exit_normally_with_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcomes = self.run_powershell(Path(tmp), '''
$evidenceDirectory = $PSScriptRoot
$outputPath = $PSScriptRoot
$TimeoutSeconds = 30
$outcomes = @(foreach ($code in @(0, 7)) {
    $phase = 'synthetic-exit-' + $code
    $accepted = $true
    try {
        Invoke-FixtureEditor -Executable $args[1] -Arguments ('-NoLogo -NoProfile -NonInteractive -Command "exit ' + $code + '"') -Phase $phase
    } catch { $accepted = $false }
    $receipt = Get-Content -LiteralPath (Join-Path $evidenceDirectory ($phase + '-process.json')) -Raw | ConvertFrom-Json
    [pscustomobject]@{ accepted = $accepted; exitCode = $receipt.exitCode; timedOut = $receipt.timedOut; pid = $receipt.pid }
})
ConvertTo-Json -InputObject $outcomes -Depth 4 -Compress
''', [self.powershell])
        self.assertEqual([item['accepted'] for item in outcomes], [True, False])
        self.assertEqual([item['exitCode'] for item in outcomes], [0, 7])
        self.assertTrue(all(not item['timedOut'] and item['pid'] > 0 for item in outcomes))


if __name__=='__main__':unittest.main()
