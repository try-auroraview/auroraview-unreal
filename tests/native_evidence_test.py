import copy
import importlib.util
from pathlib import Path
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

if __name__=='__main__':unittest.main()
