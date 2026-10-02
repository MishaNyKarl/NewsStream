import io
import os
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


@pytest.mark.skipif(os.name != 'posix', reason='POSIX modes are preserved by Docker COPY')
def test_extracted_directories_readable_for_nonroot_container(tmp_path):
    path = archive(tmp_path, [('migrations/versions/0001.py', 'revision="0001"')])
    old_mask = os.umask(0o077)
    try:
        remote.extract_release(path, tmp_path / 'release', SHA)
    finally:
        os.umask(old_mask)
    assert (tmp_path / 'release/migrations/versions').stat().st_mode & 0o777 == 0o755
    assert (tmp_path / 'release/migrations/versions/0001.py').stat().st_mode & 0o777 == 0o644


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


def test_admin_compose_uses_closed_config_and_exact_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'STATE', tmp_path / 'state')
    (remote.STATE / SHA).mkdir(parents=True)
    args = remote.admin_compose(tmp_path, 'newswatch-admin:' + SHA, SHA, 'up', '-d')
    assert str(remote.ADMIN_COMPOSE_ENV) in args
    assert 'newswatch-admin' in args
    overrides = list((remote.STATE / SHA).glob('admin-*.yaml'))
    assert len(overrides) == 1 and SHA in overrides[0].read_text()
    assert 'PASSWORD' not in overrides[0].read_text()


def test_rollback_restores_existing_admin_image(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'ROOT', tmp_path)
    monkeypatch.setattr(remote, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(remote, 'RELEASES', tmp_path / 'releases')
    (remote.STATE / SHA).mkdir(parents=True)
    previous_source = remote.RELEASES / ('b' * 40)
    calls, pointers = [], []
    monkeypatch.setattr(remote, 'command', lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(remote, 'switch_pointer', lambda source: pointers.append(source))
    remote.restore({'revision': SHA, 'previous': {'source': str(previous_source), 'image': 'newswatch:old'},
        'admin_started': True, 'admin_previous': {'source': str(previous_source),
            'image': 'newswatch-admin:old', 'revision': 'b' * 40}})
    assert len(calls) == 2 and calls[1][-1] == 'admin'
    assert '--no-build' in calls[1]
    assert pointers == [previous_source]
    assert 'newswatch-admin:old' in next((remote.STATE / SHA).glob('admin-*.yaml')).read_text()


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


@pytest.mark.skipif(os.name != 'posix', reason='Production transaction uses Linux file locks and symlinks')
@pytest.mark.parametrize('failure', ['build', 'switch'])
def test_failed_release_restores_previous_and_preserves_settings(tmp_path, monkeypatch, failure):
    root = tmp_path / 'project'
    root.mkdir()
    (root / '.env').write_text('EXAMPLE=preserved\n')
    monkeypatch.setattr(remote, 'ROOT', root)
    monkeypatch.setattr(remote, 'STATE', root / 'deploy-state')
    monkeypatch.setattr(remote, 'RELEASES', root / 'releases')
    job = remote.STATE / SHA
    job.mkdir(parents=True)
    archive(tmp_path).replace(job / 'source.tar.gz')
    remote.save_state({'revision': SHA, 'status': 'queued'})
    calls = []
    failed = False

    def fake_command(args, **kwargs):
        nonlocal failed
        calls.append(args)
        if args[:2] == ['docker', 'inspect']:
            return 'legacy' if 'Labels' in args[3] else 'sha256:previous'
        if args[:2] == ['docker', 'exec']:
            return '0'
        should_fail = 'build' in args if failure == 'build' else 'up' in args
        if should_fail and not failed:
            failed = True
            raise remote.DeployError('simulated_failure')

    def fake_backup(args, stdout, **kwargs):
        stdout.write(b'backup' * 100)

    monkeypatch.setattr(remote, 'command', fake_command)
    monkeypatch.setattr(remote.subprocess, 'run', fake_backup)
    with pytest.raises(remote.DeployError, match='deployment_failed'):
        remote.run(SHA)
    state = remote.load_state(SHA)
    assert failed and state['status'] == 'rolled_back', state
    assert state['previous']['source'] == str(root)
    assert (root / '.env').read_text() == 'EXAMPLE=preserved\n'
    assert not (root / 'current').exists()
    assert '--no-build' in calls[-1] and calls[-1][-2:] == ['bot', 'worker']


@pytest.mark.skipif(os.name != 'posix', reason='Production enqueue uses Linux file locks')
def test_enqueue_does_not_wait_for_oneshot_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, 'STATE', tmp_path / 'state')
    monkeypatch.setattr(remote, 'INCOMING', tmp_path / 'incoming')
    remote.INCOMING.mkdir()
    archive(tmp_path).replace(remote.INCOMING / (SHA + '.tar.gz'))
    calls = []
    monkeypatch.setattr(remote, 'command', lambda args, **kwargs: calls.append(args))
    assert remote.enqueue(SHA)['status'] == 'queued'
    assert '--no-block' in calls[0]
    assert '--property=TimeoutStartSec=1800' in calls[0]
    assert any(arg.startswith('--property=ExecStopPost=') for arg in calls[0])
