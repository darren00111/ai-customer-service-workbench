"""Build the reduced existing desktop client, with a fresh empty distribution."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'feige-agent'
SKILL=ROOT/'feige-fashion-customer-service'


if __name__=='__main__':
    output=ROOT/'dist/FeigeCustomerService'
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--windowed','--onedir','--name','FeigeCustomerService',
        '--icon',str(APP/'app/assets/app-icon.ico'),'--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build'),
        '--specpath',str(ROOT/'build'),'--paths',str(SKILL/'scripts'),
        '--hidden-import','classify','--hidden-import','classify_standard','--hidden-import','kb','--hidden-import','merchant',
        '--collect-all','webview','--add-data',str(APP/'app/ui')+';app/ui',
        '--add-data',str(APP/'app/assets')+';app/assets','--add-data',str(APP/'app/service/fixtures.json')+';app/service',
        str(APP/'community_main.py')],cwd=APP,check=True)
    certificate=Path(__import__('ssl').__file__).with_name('codex-rejected-cas.pem')
    if certificate.is_file():shutil.copy2(certificate,output/'_internal/codex-rejected-cas.pem')
    (output/'data').mkdir(exist_ok=True)
    (output/'data/stores.json').write_text('{"version":1,"stores":[]}',encoding='utf-8')
    (output/'knowledge/stores').mkdir(parents=True,exist_ok=True)
    for source,destination in [(APP/'install_desktop.ps1',output/'install_desktop.ps1'),(APP/'install_desktop.cmd',output/'安装到桌面.cmd'),
                               (ROOT/'README.md',output/'README.md'),(ROOT/'LICENSE',output/'LICENSE')]:
        shutil.copy2(source,destination)
    for source in (ROOT/'docs').rglob('*'):
        if source.is_file() and source.suffix in ('.md','.txt','.png') and '.local.' not in source.name:
            destination=output/'docs'/source.relative_to(ROOT/'docs')
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,destination)
    (output/'启动社区版.cmd').write_bytes('@echo off\r\nstart "" "%~dp0FeigeCustomerService.exe"\r\n'.encode('utf-8'))
    print(str(output))
