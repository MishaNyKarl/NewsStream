"""Scan tracked files (or every historical blob) without printing matching values."""
import re
import subprocess
import sys

PATTERNS = [
    re.compile(rb'sk-or-v1-[0-9a-f]{50,}'),
    re.compile(rb'\b[0-9]{8,12}:AA[A-Za-z0-9_-]{30,}'),
    re.compile(rb'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'),
    re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'),
]


def main():
    history = '--history' in sys.argv
    if history:
        rows = subprocess.check_output(['git', 'rev-list', '--objects', '--all'], text=True).splitlines()
        objects = [line.split(' ', 1)[0] for line in rows]
        kinds = subprocess.check_output(['git', 'cat-file', '--batch-check=%(objectname) %(objecttype)'],
            input=('\n'.join(objects) + '\n').encode()).decode().splitlines()
        candidates = [(line.split()[0], line.split()[0]) for line in kinds if line.endswith(' blob')]
    else:
        names = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
        candidates = [('HEAD:' + name, name) for name in names if name]
    findings = []
    for ref, label in candidates:
        if history:
            data = subprocess.check_output(['git', 'cat-file', 'blob', ref])
        else:
            from pathlib import Path
            file = Path(label)
            if not file.is_file():
                continue
            data = file.read_bytes()
        if any(pattern.search(data) for pattern in PATTERNS):
            findings.append(label)
    if findings:
        print('Potential credentials detected; values withheld:', ', '.join(findings))
        return 1
    print('Credential pattern scan passed:', len(candidates), 'files/blobs')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
