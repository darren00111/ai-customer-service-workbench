"""Allowlisted source export and credential scan. Never prints credential values."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / '.github-upload'
SKIP = {'.git', '.venv', 'node_modules', '__pycache__', 'data', 'knowledge',
        'backups', 'build', 'dist', 'releases', '_tmp', 'verification'}
PATTERNS = {
    'provider_key': rb'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}',
    'github_token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})',
    'google_key': rb'\bAIza[A-Za-z0-9_-]{30,}',
    'aws_key': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'private_key': rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'webhook_credential': rb'https://(?:qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=|oapi\.dingtalk\.com/robot/send\?access_token=)[A-Za-z0-9_-]{20,}',
}
PATTERNS = {name: re.compile(value) for name, value in PATTERNS.items()}
SUFFIXES = {'.py', '.js', '.html', '.css', '.json', '.yaml', '.yml', '.md',
            '.txt', '.ps1', '.cmd', '.bat', '.sh', '.svg', '.png', '.ico'}


def source_files():
    result = {}
    for name in ('.gitignore', 'README.md', 'SECURITY.md'):
        result[name] = (ROOT / name).read_bytes()
    for base in ('feige-agent', 'feige-fashion-customer-service', 'tools'):
        paths = []
        for folder, directories, names in os.walk(ROOT / base):
            directories[:] = sorted(name for name in directories if name not in SKIP)
            paths.extend(Path(folder) / name for name in sorted(names))
        for path in paths:
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(ROOT)
            parts = relative.parts
            if any(part in SKIP for part in parts[1:-1]):
                continue
            if path.suffix not in SUFFIXES and path.name not in ('VERSION', '.gitignore'):
                continue
            # Old rendered reports may contain real account screenshots/content.
            if base == 'feige-agent' and len(parts) > 2 and parts[1] == 'docs' and path.suffix not in ('.md', '.txt'):
                continue
            if base == 'feige-fashion-customer-service' and len(parts) > 2 and parts[1] == 'docs':
                continue
            if path.suffix in ('.png', '.ico') and parts[:3] != ('feige-agent', 'app', 'assets'):
                continue
            if path.name in ('qwen.json', 'credentials.json', 'secrets.json') or path.name.startswith('.env'):
                continue
            result[relative.as_posix()] = path.read_bytes()
    # Only explicit synthetic fixtures are allowed from the legacy data tree.
    legacy = ROOT / 'feige-fashion-customer-service/data'
    for path in sorted((legacy / 'samples').glob('*.json')):
        result[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    shop = legacy / 'stores/shop_001'
    for path in sorted(shop.rglob('*.yaml')):
        if path.parent.name == 'products' and b'sample_data: true' not in path.read_bytes():
            raise ValueError('Non-synthetic legacy product fixture: ' + path.name)
        result[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in (legacy / 'README.md', legacy / 'conversations/.gitkeep'):
        result[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    return result


def inspect(files):
    findings = []
    for name, content in files.items():
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append({'path': name, 'type': label})
        if name.endswith('.json'):
            try:
                data = json.loads(content)
            except (ValueError, UnicodeError):
                continue
            def walk(value, field=''):
                if isinstance(value, dict):
                    for key, item in value.items():
                        child = field + '.' + key if field else key
                        if re.fullmatch(r'(?i)(?:api_?key|access_?token|password|secret|encrypted_?key|protected_?key)', key) and item:
                            findings.append({'path': name, 'type': 'credential_field', 'field': child})
                        walk(item, child)
                elif isinstance(value, list):
                    for item in value:
                        walk(item, field + '[]')
            walk(data)
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--export', action='store_true')
    parser.add_argument('--payload-chunk', type=int)
    args = parser.parse_args()
    if args.payload_chunk is not None:
        encoded = (EXPORT / 'payload.base64').read_text(encoding='ascii')
        start = args.payload_chunk * 18000
        print(json.dumps({'index': args.payload_chunk, 'chunk': encoded[start:start+18000]}))
        return 0
    files = source_files()
    findings = inspect(files)
    report = {'files': len(files), 'bytes': sum(map(len, files.values())),
              'passed': not findings, 'findings': findings,
              'runtime_data_included': False, 'original_history_included': False,
              'runtime_defaults_tracked': False,
              'sha256': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    if findings:
        print(json.dumps(report, ensure_ascii=True))
        return 1
    if args.export:
        EXPORT.mkdir(exist_ok=True)
        source_root = EXPORT / 'source'
        # Remove only stale generated files inside this dedicated export folder.
        for stale in source_root.rglob('*'):
            if stale.is_file() and stale.relative_to(source_root).as_posix() not in files:
                if not stale.resolve().is_relative_to(source_root.resolve()):
                    raise ValueError('Export path escaped source folder')
                stale.unlink()
        for name, raw in files.items():
            target = EXPORT / 'source' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        payload = []
        for name, raw in files.items():
            binary = name.endswith(('.png', '.ico'))
            payload.append({'path': name, 'mode': '100644', 'type': 'blob', 'binary': binary,
                            'content': base64.b64encode(raw).decode() if binary else raw.decode('utf-8')})
        encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
        (EXPORT / 'payload.base64').write_text(encoded, encoding='ascii')
        report['payload_chunks'] = (len(encoded)+17999)//18000
        (EXPORT / 'audit.json').write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'sha256'}, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
