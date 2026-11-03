"""Check publishable community source without reading credentials or runtime data."""
import argparse
import json
import os
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
SKIP={'.git','.venv','__pycache__','data','knowledge','profiles','backups','build','dist','releases','node_modules'}
SUFFIX={'.py','.js','.html','.css','.json','.yaml','.yml','.md','.txt','.ps1','.cmd','.bat','.svg'}
PATTERNS=[re.compile(p) for p in (rb'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}',
    rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})',
    rb'\bAIza[A-Za-z0-9_-]{30,}',rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');parser.parse_args()
    findings=[];count=0
    for folder,dirs,names in os.walk(ROOT):
        dirs[:]=[name for name in dirs if name not in SKIP]
        for name in names:
            path=Path(folder)/name;relative=path.relative_to(ROOT).as_posix()
            if path.is_symlink() or path.suffix not in SUFFIX or '.local.' in name:continue
            if name.startswith('.env') and name!='.env.example':continue
            raw=path.read_bytes();count+=1
            if any(pattern.search(raw) for pattern in PATTERNS):findings.append(relative)
    print(json.dumps({'passed':not findings,'checked_files':count,'findings':findings,'runtime_data_included':False}))
    return 1 if findings else 0


if __name__=='__main__':raise SystemExit(main())
