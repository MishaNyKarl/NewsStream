import io
import tarfile
from pathlib import Path

import pytest

from deploy import remote

SHA = 'a' * 40


def archive(tmp_path, extra=(), manifest=SHA):
    path = tmp_path / 'release.tar.gz'
    with tarfile.open(path, 'w:gz') as stream:
        for name, content in [('REVISION', manifest), ('Dockerfile', 'FROM scratch'),
                              ('compose.yaml', 'services: {}'), ('app/main.py', '')] + list(extra):
            item = tarfile.TarInfo(name)
            if content is None:
                item.type = tarfile.SYMTYPE
                item.linkname = '/etc/passwd'
                stream.addfile(item)
            else:
                data = content.encode()
                item.size = len(data)
                stream.addfile(item, io.BytesIO(data))
    return path


@pytest.mark.parametrize('name', ['../escape', '/etc/file', 'app/../../escape',
    'app\\escape', '.env', 'app/.env.production', 'work/private', 'secret.key',
    'outputs/access.txt', '.git/config', 'current/file'])
def test_archive_rejects_private_and_unsafe_paths(tmp_path, name):
    with pytest.raises(remote.DeployError):
        remote.validate_archive(archive(tmp_path, [(name, 'x')]), SHA)


def test_archive_rejects_links_duplicates_and_wrong_commit(tmp_path):
    for extra, manifest in [([('link', None)], SHA), ([('Dockerfile', 'again')], SHA),
                            ([], 'b' * 40)]:
        with pytest.raises(remote.DeployError):
            remote.validate_archive(archive(tmp_path, extra, manifest), SHA)


def test_extract_valid_release_preserves_examples(tmp_path):
    path = archive(tmp_path, [('.env.example', 'TOKEN=placeholder')])
    destination = tmp_path / 'release'
    remote.extract_release(path, destination, SHA)
    assert (destination / 'REVISION').read_text() == SHA
    assert (destination / '.env.example').read_text() == 'TOKEN=placeholder'


def test_archive_expansion_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'MAX_EXPANDED', 20)
    with pytest.raises(remote.DeployError, match='expanded_archive_too_large'):
        remote.validate_archive(archive(tmp_path), SHA)


@pytest.mark.parametrize('text', ['bash', 'status ../etc/passwd', 'deploy ' + SHA + '; id',
                                 'upload ' + SHA + ' extra', 'rm ' + SHA])
def test_ssh_rejects_arbitrary_commands(text):
    with pytest.raises(remote.DeployError):
        remote.parse_ssh(text)


def test_public_state_excludes_internal_details():
    assert remote.public_state({'revision': SHA, 'status': 'running',
                                'previous': {'image': 'secret'}, 'password': 'hidden'}) == {
                                    'revision': SHA, 'status': 'running'}


def test_rollback_uses_previous_image_without_build(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'ROOT', tmp_path)
    monkeypatch.setattr(remote, 'STATE', tmp_path / 'state')
    (remote.STATE / SHA).mkdir(parents=True)
    calls = []
    monkeypatch.setattr(remote, 'command', lambda args, **kwargs: calls.append(args))
    remote.restore({'revision': SHA, 'previous': {'source': str(tmp_path),
                    'image': 'newswatch:previous', 'revision': 'b' * 40}})
    assert len(calls) == 1
    assert '--no-build' in calls[0] and '--wait' in calls[0]
    assert calls[0][-2:] == ['bot', 'worker']
    override = Path(calls[0][calls[0].index('-f') + 3])
    assert 'newswatch:previous' in override.read_text()
    assert 'b' * 40 in override.read_text()


def test_rollback_rejects_source_outside_project(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'ROOT', tmp_path / 'project')
    monkeypatch.setattr(remote, 'RELEASES', tmp_path / 'project/releases')
    with pytest.raises(remote.DeployError, match='invalid_previous_release'):
        remote.restore({'revision': SHA, 'previous': {'source': str(tmp_path), 'image': 'old'}})
