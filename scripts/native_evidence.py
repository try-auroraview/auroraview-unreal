#!/usr/bin/env python3
"""Seed/check native evidence; does not run Unreal or certify honest observation.

A pass requires separate trusted run inputs, the exact source file manifest, and
real hashed artifacts. A seed or a self-edited record cannot supply that trust.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import struct
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
POLICY_FIELDS = ('id', 'stage', 'required', 'implementation', 'steps', 'expected_host_state', 'automation')
CLOCK_TOLERANCE_S = 0.25

def read_json(path):
    def reject(value):
        raise ValueError('nonfinite JSON number: '+value)
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('duplicate JSON key: '+key)
            result[key]=value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=reject, object_pairs_hook=unique)

def registry():
    cases = read_json(ROOT/'acceptance/cases.json')['cases']
    return {c['id']: c for c in cases}

def git_identity():
    def git(*args):
        try:
            return subprocess.check_output(['git', '-C', str(ROOT), *args], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    top = git('rev-parse', '--show-toplevel')
    own_repo = bool(top) and Path(top).resolve() == ROOT
    return {'commit': git('rev-parse', 'HEAD') if own_repo else None,
            'tree': git('rev-parse', 'HEAD^{tree}') if own_repo else None,
            'dirty': bool(git('status', '--porcelain')) if own_repo else None}

def seed():
    source = git_identity()
    source['core_commit'] = read_json(ROOT/'ThirdParty/AuroraViewCore/manifest.json')['commit']
    return {'schema_version': 2, 'run_id': str(uuid.uuid4()),
            'created_utc': datetime.now(timezone.utc).isoformat(), 'source': source,
            'engine': {'version': None, 'build_id': None, 'platform': 'Win64'},
            'build': {'status': 'pending', 'dll': None, 'log': None, 'receipt': None, 'automation_report': None},
            'recording': {'started_utc': None, 'host_seconds_at_video_zero': None},
            'cases': copy.deepcopy(list(registry().values()))}

def strict_equal(actual, expected):
    if type(actual) is not type(expected): return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(strict_equal(actual[k], v) for k,v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(strict_equal(a,b) for a,b in zip(actual,expected))
    return actual == expected

def is_finite_number(value):
    if type(value) not in (int,float): return False
    try: return math.isfinite(value)
    except OverflowError: return False

def finite(value): return is_finite_number(value) and value >= 0

def utc(value):
    if not isinstance(value, str): return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo and result.utcoffset().total_seconds() == 0 else None
    except ValueError: return None

def safe_path(value):
    if not isinstance(value,str) or not value or '\\' in value or re.match(r'^[A-Za-z]:',value): return None
    p = Path(value)
    return p if not p.is_absolute() and '..' not in p.parts and str(p) != '.' else None

def validate_structure(value, schema, path='$'):
    """Execute the small JSON-Schema subset used by the checked-in schema.

    No network/schema fetching and no optional dependency. Unsupported keywords
    fail closed so an expanded schema cannot silently become documentation-only.
    """
    supported={'$schema','title','description','type','required','properties','items','minItems','minLength','minimum','pattern','enum','const'}
    errors=[path+': unsupported schema keyword '+k for k in schema if k not in supported]
    types={'object':dict,'array':list,'string':str,'integer':int,'boolean':bool,'null':type(None)}
    kinds=schema.get('type',[]);kinds=[kinds] if isinstance(kinds,str) else kinds
    def matches(kind):return type(value) in (int,float) if kind=='number' else type(value) is types.get(kind)
    if kinds and not any(matches(k) for k in kinds):return errors+[path+': invalid structural type']
    if 'const' in schema and not strict_equal(value,schema['const']):errors.append(path+': incorrect constant')
    if 'enum' in schema and not any(strict_equal(value,v) for v in schema['enum']):errors.append(path+': invalid enum')
    if isinstance(value,dict):
        errors += [path+': missing '+k for k in schema.get('required',[]) if k not in value]
        for key,sub in schema.get('properties',{}).items():
            if key in value:errors += validate_structure(value[key],sub,path+'.'+key)
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0):errors.append(path+': too few items')
        for i,item in enumerate(value):errors += validate_structure(item,schema.get('items',{}),path+'['+str(i)+']')
    if isinstance(value,str):
        if len(value)<schema.get('minLength',0):errors.append(path+': string too short')
        if 'pattern' in schema and not re.search(schema['pattern'],value):errors.append(path+': pattern mismatch')
    if type(value) in (int,float) and (not is_finite_number(value) or value<schema.get('minimum',-math.inf)):
        errors.append(path+': invalid finite numeric range')
    return errors

def validate(data, evidence_root=None, require_complete=False, trusted_inputs=None):
    errors=[]
    def require(ok, message):
        if not ok: errors.append(message)
        return bool(ok)
    def reject_nonfinite(value):
        if isinstance(value,float) and not math.isfinite(value): return False
        if isinstance(value,dict): return all(reject_nonfinite(v) for v in value.values())
        if isinstance(value,list): return all(reject_nonfinite(v) for v in value)
        return True
    def artifact(value, label):
        if not require(isinstance(value,dict), label+': actual artifact descriptor required'): return None
        path=safe_path(value.get('file'))
        require(path is not None, label+': uses relative evidence path')
        require(bool(re.fullmatch(r'[0-9a-f]{64}', str(value.get('sha256','')))), label+': SHA-256 required')
        if not require(evidence_root is not None, label+': evidence root required') or path is None: return None
        root=Path(evidence_root).resolve(); file=root/path
        if not require(file.resolve().is_relative_to(root), label+': artifact escapes evidence root'): return None
        if not require(file.is_file(), label+': missing actual file '+str(path)): return None
        require(hashlib.sha256(file.read_bytes()).hexdigest() == value.get('sha256'), label+': hash mismatch')
        return file
    if not isinstance(data,dict): return ['record must be an object']
    structure_errors=validate_structure(data,read_json(ROOT/'acceptance/evidence.schema.json'))
    # Continue semantic checks only when shape permits safe field access.
    if structure_errors: return structure_errors
    require(reject_nonfinite(data), 'nonfinite JSON numbers are forbidden')
    require(type(data.get('schema_version')) is int and data['schema_version'] == 2, 'schema_version must be 2')
    try: uuid.UUID(data.get('run_id',''))
    except (ValueError,TypeError,AttributeError): errors.append('run_id must be a UUID')
    created=utc(data.get('created_utc')); require(created is not None,'created_utc must be timezone-aware UTC')
    for key in ('source','engine','build','recording'):
        if not isinstance(data.get(key),dict): errors.append(key+' must be an object')
    if any(not isinstance(data.get(k),dict) for k in ('source','engine','build','recording')): return errors
    canonical=registry()
    cases=data.get('cases')
    if not isinstance(cases,list): return errors+['cases must be an array']
    ids=set(); passing=[]
    for case in cases:
        if not isinstance(case,dict): errors.append('case must be an object'); continue
        cid=case.get('id')
        if not isinstance(cid,str): errors.append('case ID required'); continue
        require(cid not in ids,'duplicate case '+cid); ids.add(cid)
        policy=canonical.get(cid)
        if not require(policy is not None, 'unknown case '+cid): continue
        for field in POLICY_FIELDS:
            require(field in case and strict_equal(case[field],policy[field]),cid+': registry policy mismatch: '+field)
        status=case.get('status')
        require(status in ('pending','blocked','pass','fail','unimplemented'),cid+': invalid status')
        if policy['implementation'] == 'unimplemented':
            require(status == 'unimplemented',cid+': missing code must be labeled unimplemented')
        else: require(status != 'unimplemented',cid+': implemented source needs a measured/pending state')
        if require_complete and policy['required']: require(status == 'pass',cid+': required native case has not passed')
        if status == 'pass': passing.append((case,policy))
    require(ids == set(canonical),'evidence must cover every registered case; missing cases cannot silently disappear')
    if not passing and not require_complete: return errors
    source=data['source']; engine=data['engine']; build=data['build']; recording=data['recording']
    require(source.get('dirty') is False,'pass cannot cite a dirty/unrecorded source')
    for field in ('commit','tree','core_commit'):
        require(bool(re.fullmatch(r'[0-9a-f]{40}',str(source.get(field,'')))),'source '+field+' must be recorded')
    require(source.get('core_commit') == read_json(ROOT/'ThirdParty/AuroraViewCore/manifest.json')['commit'],'Core identity does not match pinned manifest')
    require(isinstance(engine.get('version'),str) and bool(re.match(r'^5\.7\.\d+(?:[-+].*)?$',engine['version'])),'only recorded UE 5.7 builds are permitted')
    require(engine.get('platform') == 'Win64','only Win64 is permitted')
    require(isinstance(engine.get('build_id'),str) and bool(engine['build_id']),'engine build_id required')
    require(build.get('status') == 'pass','actual native build must have passed')
    trusted=trusted_inputs if isinstance(trusted_inputs,dict) else {}
    require(bool(trusted),'separate trusted run inputs required; run record is not a trust anchor')
    for field,value in (('run_id',data.get('run_id')),('source',source),('engine',engine),('build',build)):
        require(strict_equal(trusted.get(field),value),'trusted run '+field+' mismatch')
    # Verify the unpacked tree itself, not an unrelated enclosing repository.
    files=trusted.get('source_files')
    if require(isinstance(files,dict) and bool(files),'trusted source file manifest required'):
        actual_files={p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*')
                      if p.is_file() and '.git' not in p.relative_to(ROOT).parts
                      and '__pycache__' not in p.relative_to(ROOT).parts and p.suffix != '.pyc'}
        require(set(files) == actual_files,'source file coverage mismatch')
        for name,digest in files.items():
            path=safe_path(name)
            if not require(path is not None,'unsafe source manifest path'): continue
            file=ROOT/path
            require(file.resolve().is_relative_to(ROOT.resolve()) and not file.is_symlink() and file.is_file()
                    and hashlib.sha256(file.read_bytes()).hexdigest()==digest,'source file hash mismatch: '+name)
    current=git_identity()
    if current['commit'] is not None:
        require(all(source.get(k)==current[k] for k in ('commit','tree','dirty')),'actual checkout identity mismatch')
    else:
        # Archived sources additionally need a separately pinned delivery receipt.
        receipt_file=artifact(trusted.get('delivery_receipt'),'delivery receipt')
        if receipt_file:
            try:
                receipt=read_json(receipt_file)
                require(strict_equal(receipt.get('source'),source),'delivery receipt identity mismatch')
                require(strict_equal(receipt.get('source_files'),files),'delivery receipt source manifest mismatch')
                archive=artifact(receipt.get('archive'),'delivery archive')
                if archive:
                    with tarfile.open(archive) as tar:
                        members=tar.getmembers()
                        require(all(m.isfile() or m.isdir() for m in members),'archive contains special entries')
                        manifest={}
                        for member in members:
                            if not member.isfile(): continue
                            path=safe_path(member.name)
                            if not require(path is not None,'unsafe archive member'): continue
                            require(str(path) not in manifest,'duplicate archive member')
                            manifest[str(path)]=hashlib.sha256(tar.extractfile(member).read()).hexdigest()
                        require(manifest==files,'archive contents differ from trusted source files')
            except (ValueError,OSError,KeyError,TypeError,tarfile.TarError,EOFError) as e: errors.append('invalid delivery receipt/archive: '+str(e))
    dll_file=artifact(build.get('dll'),'native DLL')
    if dll_file:
        raw=dll_file.read_bytes()
        offset=struct.unpack_from('<I',raw,0x3c)[0] if len(raw)>=64 else -1
        require(offset>=64 and len(raw)>=offset+26 and raw[:2]==b'MZ'
                and raw[offset:offset+4]==b'PE\0\0'
                and struct.unpack_from('<H',raw,offset+4)[0]==0x8664
                and struct.unpack_from('<H',raw,offset+22)[0]&0x2000 != 0
                and struct.unpack_from('<H',raw,offset+24)[0]==0x20b,
                'native DLL is not an AMD64 PE32+ DLL')
    artifact(build.get('log'),'native build log')
    receipt_file=artifact(build.get('receipt'),'native build receipt')
    if receipt_file:
        try:
            receipt=read_json(receipt_file)
            for field,value in (('run_id',data.get('run_id')),('source',source),('engine',engine),('dll',build.get('dll')),('log',build.get('log'))):
                require(strict_equal(receipt.get(field),value),'build receipt '+field+' mismatch')
            require(type(receipt.get('exit_code')) is int and receipt['exit_code']==0,'build receipt must record successful process exit')
            require(receipt.get('target')=='UnrealEditor Win64 Development','build target mismatch')
            compiler=receipt.get('compiler',{})
            require(isinstance(compiler,dict) and all(isinstance(compiler.get(k),str) and compiler[k] for k in ('name','version','command')),'compiler identity and command required')
        except (ValueError,OSError,TypeError) as e: errors.append('invalid build receipt: '+str(e))
    report_file=artifact(build.get('automation_report'),'native Automation report')
    results={}
    if report_file:
        try:
            report=read_json(report_file)
            # Fail closed on other report shapes. Confirm the installed 5.7 export
            # schema before adapting; never infer success from Editor exit alone.
            tests=report.get('tests')
            if require(isinstance(tests,list),'Automation report tests array required'):
                for test in tests:
                    if not isinstance(test,dict): errors.append('invalid Automation result'); continue
                    name=test.get('fullTestPath')
                    if not isinstance(name,str): errors.append('Automation fullTestPath required'); continue
                    require(name not in results,'duplicate Automation result '+name)
                    results[name]=test.get('state')
            require(report.get('failed') == 0 and type(report.get('failed')) is int,'Automation report has missing/failed count')
        except (ValueError,OSError,TypeError) as e: errors.append('invalid Automation report: '+str(e))
    start_record=utc(recording.get('started_utc')); origin=recording.get('host_seconds_at_video_zero')
    require(start_record is not None,'recording UTC clock origin required')
    require(finite(origin),'recording finite nonnegative host clock origin required')
    if created and start_record: require(created<=start_record,'record created after recording start')
    for case,policy in passing:
        cid=case['id']; actual=case.get('actual_host_state'); expected=policy['expected_host_state']
        require(isinstance(actual,dict),cid+': actual host state required')
        assertions=case.get('assertions',[])
        require(isinstance(assertions,list) and bool(assertions),cid+': actual host assertions required')
        by_name={}
        for assertion in assertions if isinstance(assertions,list) else []:
            if not isinstance(assertion,dict): errors.append(cid+': invalid assertion'); continue
            name=assertion.get('name')
            if not isinstance(name,str): errors.append(cid+': assertion name required'); continue
            require(name not in by_name,cid+': duplicate assertion '+name); by_name[name]=assertion
            require(assertion.get('observation') in ('ue_automation','editor_state'),cid+': HTML/source-only assertions cannot prove native behavior')
            require(name in expected,cid+': unrelated assertion '+name)
            # Registry currently defines exact categorical/integer criteria only.
            # Approximate numeric acceptance requires an explicit canonical policy
            # change; run records cannot choose their own tolerance.
            require(assertion.get('operator','equal')=='equal' and 'tolerance' not in assertion,cid+': no registry-approved finite tolerance for this criterion')
            require(strict_equal(assertion.get('actual'),assertion.get('expected')),cid+': assertion did not pass: '+name)
        for name,value in expected.items():
            require(isinstance(actual,dict) and name in actual and strict_equal(actual[name],value),cid+': required actual criterion mismatch: '+name)
            assertion=by_name.get(name,{})
            require(name in by_name and strict_equal(assertion.get('expected'),value) and isinstance(actual,dict)
                    and strict_equal(assertion.get('actual'),actual.get(name)),cid+': required criterion assertion missing/mismatch: '+name)
        for name in (policy.get('automation') or '').split(';'):
            if name.strip(): require(results.get(name.strip())=='Success',cid+': required native Automation case did not pass: '+name.strip())
        started=utc(case.get('started_utc')); finished=utc(case.get('finished_utc')); host=case.get('host_time_s')
        require(started is not None and finished is not None,cid+': measured timezone-aware UTC timestamps required')
        require(finite(host),cid+': finite nonnegative case host time required')
        if started and finished:
            require(started<=finished,cid+': reversed case timestamps')
            if start_record: require(start_record<=started,cid+': case precedes recording origin')
        def aligned(host_time):
            if not (finite(host_time) and finite(origin) and start_record and started and finished): return False
            elapsed=host_time-origin
            return (started-start_record).total_seconds()-CLOCK_TOLERANCE_S <= elapsed <= (finished-start_record).total_seconds()+CLOCK_TOLERANCE_S
        require(aligned(host),cid+': case host clock is outside measured UTC interval')
        media=case.get('media')
        require(isinstance(media,list) and bool(media),cid+': screenshot/video timepoints required')
        for item in media if isinstance(media,list) else []:
            if not isinstance(item,dict): errors.append(cid+': invalid media item'); continue
            require(item.get('kind') in ('screenshot','video'),cid+': unsupported evidence media')
            require(finite(item.get('host_time_s')) and aligned(item.get('host_time_s')),cid+': media host clock outside case interval')
            if item.get('kind')=='video':
                video=item.get('video_time_s'); media_host=item.get('host_time_s')
                require(finite(video),cid+': finite nonnegative video timestamp required')
                require(finite(video) and finite(origin) and finite(media_host) and abs(origin+video-media_host)<=CLOCK_TOLERANCE_S,cid+': video/host clock alignment exceeds 0.25 seconds')
            artifact(item,cid+' media')
    return errors

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--seed',type=Path); group.add_argument('--validate',type=Path)
    parser.add_argument('--require-complete',action='store_true')
    parser.add_argument('--trusted-run',type=Path,help='independently reviewed immutable run-input manifest')
    parser.add_argument('--trusted-run-sha256',help='separately verified trusted manifest digest')
    args=parser.parse_args()
    if args.seed:
        with args.seed.open('x',encoding='utf-8') as f: json.dump(seed(),f,indent=2,allow_nan=False); f.write('\n')
        print('Created pending evidence seed; no native test or media is claimed')
    else:
        try:
            data=read_json(args.validate); trusted=None
            if args.trusted_run:
                if hashlib.sha256(args.trusted_run.read_bytes()).hexdigest()!=args.trusted_run_sha256:
                    raise ValueError('trusted run manifest digest missing or mismatched')
                trusted=read_json(args.trusted_run)
            errors=validate(data,args.validate.parent,args.require_complete,trusted)
            complete=not validate(data,args.validate.parent,True,trusted)
            print(json.dumps({'record_valid':not errors,'native_acceptance_complete':complete,'errors':errors},indent=2))
            raise SystemExit(bool(errors))
        except (ValueError,OSError) as e: parser.exit(1,str(e)+'\n')
if __name__=='__main__': main()
