"""Package exactly the checked-out commit, never local settings or scratch files."""
import gzip
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path


def package(destination):
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Invalid revision')
    archive = subprocess.check_output(['git', 'archive', '--format=tar', revision])
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.GzipFile(filename=str(output), mode='wb', mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w|') as target:
            with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
                for member in source:
                    target.addfile(member, source.extractfile(member) if member.isfile() else None)
            payload = (revision + '\n').encode('ascii')
            info = tarfile.TarInfo('REVISION')
            info.size = len(payload)
            info.mode = 0o644
            target.addfile(info, io.BytesIO(payload))
    print('Release packaged:', revision)


if __name__ == '__main__':
    package(sys.argv[1])
