"""Package only this community checkout and its clean built distribution."""
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'releases/20261103-single-shop'
APP=ROOT/'dist/FeigeCustomerService'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    registry=json.loads((APP/'data/stores.json').read_text(encoding='utf-8'))
    assert registry=={'version':1,'stores':[]}
    assert not list((APP/'knowledge/stores').iterdir())
    assert [p.relative_to(APP/'data').as_posix() for p in (APP/'data').rglob('*') if p.is_file()]==['stores.json']
    source=[]
    allowed_trees={'feige-agent/app','feige-agent/tests','feige-agent/templates','feige-fashion-customer-service/scripts','scripts','docs'}
    top={'README.md','LICENSE','.gitignore','feige-agent/community_main.py','feige-agent/desktop_main.py',
         'feige-agent/requirements.txt','feige-agent/install_desktop.ps1','feige-agent/install_desktop.cmd','feige-agent/VERSION'}
    for path in ROOT.rglob('*'):
        if not path.is_file() or path.is_symlink():continue
        relative=path.relative_to(ROOT).as_posix()
        if '__pycache__' in path.parts or '.local.' in path.name:continue
        if relative not in top and not any(relative.startswith(tree+'/') for tree in allowed_trees):continue
        if path.suffix not in {'.py','.js','.html','.css','.json','.yaml','.svg','.png','.ico','.txt','.md','.ps1','.cmd',''} and relative not in top:continue
        if any(p in {'data','knowledge','profiles','backups','build','dist','releases'} for p in path.relative_to(ROOT).parts):continue
        if path.suffix in {'.py','.js','.html','.json','.yaml','.txt','.md','.ps1','.cmd'}:
            content=path.read_text(encoding='utf-8-sig')
            for match in re.findall(r'sk-(?:proj-)?[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA )?PRIVATE KEY-----',content):
                if not ('/tests/' in relative and any(word in match.lower() for word in ('fake','test','offline','not-real'))):
                    raise ValueError('疑似密钥，停止打包：'+relative)
        source.append(path)
    manifest={'version':'1.12.0-community','max_stores':1,'platforms':['feige'],'archives':[]}
    for name,files,prefix in [
        ('抖店智能客服社区版-1.12.0-单店抖店版.zip',sorted(p for p in APP.rglob('*') if p.is_file()),APP),
        ('抖店智能客服社区版-1.12.0-源码.zip',sorted(source),ROOT)]:
        destination=OUT/name
        if destination.exists():raise ValueError('发行文件已存在，禁止覆盖')
        with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as bundle:
            for path in files:
                relative=path.relative_to(prefix).as_posix()
                assert '..' not in Path(relative).parts and not path.is_symlink()
                bundle.write(path,('抖店智能客服社区版/' if prefix==APP else 'community-single-shop/')+relative)
        with zipfile.ZipFile(destination) as bundle:
            assert bundle.testzip() is None
        manifest['archives'].append({'filename':name,'sha256':digest(destination),'bytes':destination.stat().st_size,'entries':len(files)})
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
