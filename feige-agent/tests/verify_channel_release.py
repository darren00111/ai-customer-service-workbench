"""Verify a fresh empty-store release and its ZIP, without live platform access."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--package',required=True,type=Path)
    parser.add_argument('--zip',required=True,type=Path)
    parser.add_argument('--extract',required=True,type=Path)
    parser.add_argument('--report',required=True,type=Path)
    args=parser.parse_args()
    package=args.package.resolve()
    archive=args.zip.resolve()
    extracted=args.extract.resolve()
    for target in (package,archive,extracted,args.report.resolve()):
        assert target.is_relative_to(ROOT), 'Release paths must stay in the workspace'
    assert package.is_dir() and not archive.exists() and not extracted.exists()
    regression=json.loads((ROOT/'docs/verification/channel-runtime-regression.json').read_text(encoding='utf-8'))
    assert regression['passed']
    for relative,expected in regression['source_sha256'].items():
        assert sha(ROOT/relative)==expected, 'Regression sources changed: '+relative
    assert json.loads((package/'data/stores.json').read_text(encoding='utf-8'))=={'version':1,'stores':[]}
    assert {p.relative_to(package).as_posix() for p in (package/'data').rglob('*') if p.is_file()}=={'data/stores.json'}
    assert {p.relative_to(package).as_posix() for p in (package/'knowledge').rglob('*') if p.is_file()}=={'knowledge/merchant-template.yaml'}
    for directory in ('ui','assets'):
        for source in (ROOT/'app'/directory).rglob('*'):
            if source.is_file():
                assert sha(source)==sha(package/'_internal/app'/source.relative_to(ROOT/'app')), str(source)
    assert sha(ROOT/'app/service/fixtures.json')==sha(package/'_internal/app/service/fixtures.json')
    for source,target in (('install_desktop.cmd','安装到桌面.cmd'),('docs/桌面版使用说明.txt','使用说明.txt')):
        assert sha(ROOT/source)==sha(package/target)
    files=sorted(p for p in package.rglob('*') if p.is_file())
    credential=re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_.-]{24,}')
    for path in files:
        raw=path.read_bytes()
        assert not credential.search(raw.decode('latin1')), 'Plaintext credential pattern in '+str(path.relative_to(package))
        assert not credential.search(raw.decode('utf-16-le',errors='ignore')), 'UTF16 credential pattern in '+str(path.relative_to(package))
    archive.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in files:z.write(path,path.relative_to(package.parent).as_posix())
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for entry in z.infolist():
            assert (extracted/entry.filename).resolve().is_relative_to(extracted)
        z.extractall(extracted)
    for path in files:
        assert sha(path)==sha(extracted/package.name/path.relative_to(package))
    report={'verified_at':datetime.now(timezone.utc).isoformat(),
        'version':(ROOT/'VERSION').read_text(encoding='utf-8').strip(),
        'files':len(files),'bytes':sum(p.stat().st_size for p in files),
        'exe_sha256':sha(package/'FeigeCustomerService.exe'),
        'empty_store_registry':True,'runtime_allowlist_passed':True,
        'no_plaintext_credentials_detected':True,'source_fingerprint_matches':True,
        'regression_tests':sum(s['tests'] for s in regression['suites']),
        'real_shop_acceptance':False,'zip_bytes':archive.stat().st_size,
        'zip_sha256':sha(archive),'zip_crc_passed':True,'extracted_files_match':True}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
