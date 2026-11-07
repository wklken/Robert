"""Offline repair acceptance with real Git, processes, and optional OS isolation."""
from robert_agent.delivery.application import effects as publication_effects

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from unittest.mock import patch
from zipfile import ZipFile

from robert_agent.delivery.application import reconcile as publication_reconcile
from robert_agent.adapters.processes import runtime_identity
from robert_agent.supervision import diagnosis as supervision_diagnose
from robert_agent.workflows.maintenance import repair as supervision_repair
from robert_agent.supervision import store as supervision_store
from robert_agent.work.application import commands as work_items
from tests.test_supervision_repair_worker import BrokerFixture, broker_result, stop_fixture, submission_source
from robert_agent.delivery.application import result_audit
from robert_agent.execution.application import evidence


def _diagnose(fixture, scope_type, scope_id):
    with closing(sqlite3.connect(fixture.db)) as conn, conn:
        conn.row_factory = sqlite3.Row
        changes = supervision_store.apply_observations(conn, [{
            'scope_type': scope_type, 'scope_id': scope_id, 'rule_id': 'repair-fixture',
            'classification': 'confirmed', 'lineage_key': 'lineage:' + scope_id,
            'reason_code': 'fixture_consumer_failure', 'severity': 'error',
            'evidence': {'phase': 'fixture', 'observation': 'Consumer never received the committed result'},
            'expected_progress': {'kind': 'consumer_receipt'},
        }], fixture.now)
        incident = changes[0]['incident']
        target = supervision_diagnose._target(incident, 'incident')
        bundle = supervision_diagnose._bundle(supervision_diagnose._snapshot(conn, target), target, 'fixture-bundle')
        answer = {key: bundle[key] for key in ('bundle_id', 'bundle_sha256')}
        answer.update(diagnosis_class='product_defect', evidence_ids=['e1'], recommended_action='repair_task',
                      expected_progress='Observe the original consumer receipt')
        supervision_store.update_checkpoint(conn, 'diagnosis', incident['incident_id'], 'analysis', now=fixture.now,
            metadata={'status': 'succeeded', 'diagnosis': answer, 'bundle': bundle,
                      'input_generation': bundle['input_generation']})
    return incident['incident_id']


def _backlog_and_recursion(root):
    fixture = BrokerFixture(root, 'raise AssertionError("must remain backlog")')
    fixture.config['supervision']['repair']['mode'] = 'backlog'
    incident_id = _diagnose(fixture, 'controller', 'fixture-local')
    first = supervision_repair.propose(fixture.config, incident_id)
    again = supervision_repair.propose(fixture.config, incident_id)
    assert first['status'] == again['status'] == 'backlog', (first, again)
    assert first['work_item_id'] == again['work_item_id']
    assert first['task_id'] is None
    assert fixture.counts() == (0, 0)
    # An explicit human start in a fixture represents a failed maintenance task.
    # Its subsequent incident links back to the maintenance item and cannot recurse.
    context = work_items.CommandContext('operator', 'fixture-operator', frozenset({'repo'}), frozenset())
    active = work_items.execute_command(fixture.db, first['work_item_id'], context=context,
        command='start', expected_version=1, idempotency_key='fixture-start')
    failure = _diagnose(fixture, 'task', active['task_id'])
    rejected = supervision_repair.propose(fixture.config, failure)
    assert rejected['status'] == 'repair_recursion_blocked', rejected
    assert rejected['work_item_id'] == first['work_item_id']
    with closing(sqlite3.connect(fixture.db)) as conn:
        assert conn.execute('SELECT count(*) FROM work_items').fetchone()[0] == 1
    return fixture


def _build_evidence(root, fixture):
    package = fixture.repo / 'src' / 'robert_agent'
    package.mkdir(parents=True)
    original = b'VERSION = "unchanged-version"\nFIXED = True\n'
    (package / '__init__.py').write_bytes(original)
    fixture.git('add', '.')
    fixture.git('commit', '-qm', 'commit the simulated Robert source')
    wheel = root / 'fixture_robert-1.0-py3-none-any.whl'
    with ZipFile(wheel, 'w') as artifact:
        artifact.writestr('robert_agent/__init__.py', original)
        artifact.writestr('fixture_robert-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: fixture-robert\nVersion: 1.0\n')
        artifact.writestr('fixture_robert-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
    manifest = runtime_identity.build_manifest(fixture.repo, wheel)
    assert manifest['source_revision'] == fixture.git('rev-parse', 'HEAD')
    sidecar = root / 'build-manifest.json'
    sidecar.write_text(json.dumps(manifest))
    sidecar.chmod(0o600)
    installed = root / 'installed' / 'robert_agent'
    installed.mkdir(parents=True)
    with ZipFile(wheel) as artifact:
        (installed / '__init__.py').write_bytes(artifact.read('robert_agent/__init__.py'))
    verified = runtime_identity.runtime_snapshot(run_id='fixture-runtime', started_at=fixture.now,
                                                  manifest_path=sidecar, package_root=installed)
    assert verified['build_status'] == 'verified' and verified['build_id'] == manifest['build_id'], verified
    assert verified['process_identity']['pid'] == os.getpid()
    (installed / '__init__.py').write_bytes(original.replace(b'True', b'False'))
    mismatched = runtime_identity.runtime_snapshot(run_id='fixture-runtime-mismatch', started_at=fixture.now,
                                                    manifest_path=sidecar, package_root=installed)
    assert mismatched['build_status'] == 'unverified', mismatched
    assert mismatched['reason_code'] == 'running_package_digest_mismatch', mismatched
    return {'source_revision': manifest['source_revision'], 'mismatch_reason': mismatched['reason_code']}


_CHECK_DENIAL = '''
import errno, os, pathlib, sys
for target in sys.argv[1:]:
    for mode in (os.O_RDONLY, os.O_RDWR):
        try:
            fd = os.open(target, mode)
        except OSError as error:
            assert error.errno in {errno.EPERM, errno.EACCES, errno.ENOENT, errno.EROFS}
        else:
            os.close(fd)
            raise AssertionError('protected host data was accessible')
'''


_LAUNCHER = '''
import json, os, sys
from pathlib import Path
binary, data, config, credentials = sys.argv[1:5]
write_paths = ' '.join('(subpath ' + json.dumps(path) + ')' for path in
                       [str(Path.cwd()), os.environ['HOME']])
read_denials = ' '.join('(subpath ' + json.dumps(path) + ')' for path in [data, *json.loads(credentials)])
profile = ('(version 1)(allow default)(deny network*)(deny file-write*)'
           '(allow file-write* (literal "/dev/null") ' + write_paths + ')'
           '(deny file-read* file-write* ' + read_denials + ' (literal ' + json.dumps(config) + '))')
os.execv(binary, [binary, '-p', profile, '--', *sys.argv[6:]])
'''


_ATTACK_GIT = '''
import shlex, subprocess
escaped = pathlib.Path(ESCAPE_MARKER)
try:
    escaped.write_text('escaped directly')
except PermissionError:
    pass
else:
    raise AssertionError('restricted worker could write outside its checkout')

def git(*args):
    return subprocess.check_output(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                                    *args], text=True).strip()

pathlib.Path('fixture.txt').write_text('fixed inside restricted launcher\\n')
pathlib.Path('.gitattributes').write_text('fixture.txt filter=escape diff=escape\\n')
pathlib.Path('.gitignore').write_text('override.py\\n')
git('add', '.')
git('commit', '-qm', 'Fix inside real restricted launcher')
pathlib.Path('override.py').write_text('would influence verification')
hook = pathlib.Path('.git/hooks/pre-push')
hook.parent.mkdir(exist_ok=True)
hook.write_text('#!/bin/sh\\nprintf escaped > ' + shlex.quote(str(escaped)) + '\\n')
hook.chmod(0o700)
config = pathlib.Path('.git/hostile-config')
config.write_text('[core]\\n    fsmonitor = ' + str(hook.resolve()) + '\\n'
                  '[filter "escape"]\\n    clean = ' + str(hook.resolve()) + '\\n'
                  '[diff "escape"]\\n    textconv = ' + str(hook.resolve()) + '\\n')
git('config', 'include.path', str(config.resolve()))
git('config', 'url.ext::' + str(hook.resolve()) + '.insteadOf', 'https://github.com/')
git('remote', 'set-url', 'origin', 'ext::' + str(hook.resolve()))
'''


def _audit_and_push(fixture, escape_marker):
    destination = fixture.root / 'local-publish.git'
    fixture.git('init', '-q', '--bare', str(destination))
    fixed_remote = fixture.git_binding['remote']
    seen = []

    def run_git_locally(command, **kwargs):
        # Redirect only the fixture's fixed canonical destination to an offline
        # bare repository. The Git process, audit and reconciliation are real.
        assert Path(kwargs['cwd']).parent == fixture.data
        assert kwargs['env']['GIT_CONFIG_GLOBAL'] == os.devnull
        if fixed_remote in command:
            command = [destination.as_uri() if item == fixed_remote else item for item in command]
            kwargs['env'] = {**kwargs['env'], 'GIT_ALLOW_PROTOCOL': 'file'}
            seen.append('ls-remote' if 'ls-remote' in command else 'push')
        else:
            assert not {'push', 'ls-remote'}.intersection(command)
        return subprocess.run(command, **kwargs)

    with closing(sqlite3.connect(fixture.db)) as conn, conn:
        conn.row_factory = sqlite3.Row
        assert result_audit.audit_completed_results(conn, 'repo', fixture.data, fixture.db,
            fixture.config['repos'][0], 'fixture-run', fixture.now, False, [], 0) == 1
        metadata = json.loads(conn.execute('SELECT metadata_json FROM worker_results').fetchone()[0])
        audit = metadata['audit']
        assert audit['status'] == 'accepted', audit
        scope = audit['action_scope']
        assert scope['repair_git'] == fixture.git_binding
        assert scope['worktree_status'] == 'clean'
        recovery = evidence.inspect_attempt_recovery(conn, fixture.attempt, fixture.task, str(fixture.tree))
        assert recovery['git'] == {}, recovery
        action = dict(conn.execute("SELECT * FROM github_actions WHERE action_type='push_existing_pr'").fetchone())
        before = publication_reconcile.classify_remote_state(action, scope, run_git_locally)
        assert before['status'] == 'confirmed_absent', before

        def authorized():
            return bool(supervision_repair.authorized_repair(
                conn, fixture.task, fixture.config, 'repo', 'push_existing_pr'))

        def revoke_after_fsck(command, **kwargs):
            response = run_git_locally(command, **kwargs)
            if 'fsck' in command:
                fixture.config['supervision']['repair']['mode'] = 'backlog'
            return response

        rejected = publication_effects.publish_action(action, revoke_after_fsck, conn=conn, recovery_guard=authorized)
        assert rejected['status'] == 'supervision_fence_rejected' and seen == ['ls-remote'], rejected
        fixture.config['supervision']['repair']['mode'] = 'auto'
        result = publication_effects.publish_action(action, run_git_locally, conn=conn, recovery_guard=authorized)
        assert result['status'] == 'published', result
        after = publication_reconcile.classify_remote_state(action, scope, run_git_locally)
        assert after['status'] == 'confirmed_effect', after
        actual = fixture.git('rev-parse', 'refs/heads/' + fixture.git_binding['branch'], cwd=destination)
        assert actual == result['result_head_sha'] == scope['result_head_sha']
        assert seen == ['ls-remote', 'push', 'ls-remote'], seen
        assert not escape_marker.exists()
    return actual


def _sandbox(root):
    binary = shutil.which('sandbox-exec') if sys.platform == 'darwin' else None
    if not binary:
        return {'status': 'blocked', 'reason': 'No supported local OS launcher is available for the isolation fixture.'}
    root = root.resolve()
    data = root / 'data'
    cfg = root / 'trusted-config.json'
    root.mkdir(parents=True, exist_ok=True)
    launcher_path = root / 'launcher.py'
    launcher_path.write_text(_LAUNCHER)
    launcher_path.chmod(0o600)
    controller_home = root / 'controller-home'
    gh_config = root / 'controller-gh'
    controller_env = {'HOME': str(controller_home), 'GH_CONFIG_DIR': str(gh_config), 'XDG_CONFIG_HOME': ''}
    credentials = [gh_config / 'hosts.yml', controller_home / '.git-credentials',
                   controller_home / '.ssh' / 'fixture_identity']
    for path in credentials:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture canary')
    credential_denials = list(map(str, (gh_config, credentials[1], credentials[2].parent)))
    launcher = [sys.executable, str(launcher_path), binary, str(data), str(cfg), json.dumps(credential_denials)]
    canary = data / 'runtime-marker.json'
    escape_marker = data / 'git-escape-marker'
    denied_files = list(map(str, [data / 'state.sqlite3', canary, cfg, *credentials]))
    model = (_CHECK_DENIAL.replace('sys.argv[1:]', repr(denied_files))
        + '''
import json
request = json.load(sys.stdin)
assert request['result_transport'] == 'stdout_compact_submission'
assert pathlib.Path('fixture.txt').read_text() == 'after\\n'
assert not any(key.startswith(('GH_', 'GITHUB_', 'ROBERT_', 'OPENCLAW_', 'XDG_')) for key in os.environ)
marker = pathlib.Path('.fixture-worktree-probe')
marker.write_text('workspace is writable')
marker.unlink()
'''
        + _ATTACK_GIT.replace('ESCAPE_MARKER', repr(str(escape_marker)))
        + submission_source())
    fixture = BrokerFixture(root, model, launcher=launcher)
    fixture.config['supervision']['repair']['verification_commands'] = [[sys.executable, '-c',
        'from pathlib import Path; assert Path("fixture.txt").read_text() == "fixed inside restricted launcher\\n"; '
        'assert not Path("override.py").exists()']]
    fixture.save_config()
    canary.write_text('{"fixture":"protected"}')
    # First establish that this environment actually enforces the profile.
    capability = subprocess.run([*launcher, '--', sys.executable, '-I', '-c', _CHECK_DENIAL, *denied_files],
                                cwd=fixture.tree,
                                env={**os.environ, 'HOME': str(fixture.tree)},
                                capture_output=True, text=True, timeout=8)
    if capability.returncode:
        return {'status': 'blocked', 'reason': 'The available OS launcher did not enforce the fixture isolation profile.',
                'launcher_returncode': capability.returncode}
    incident = _diagnose(fixture, 'controller', 'isolated-local')
    policy = supervision_repair.normalize_repair(fixture.config['supervision']['repair'])
    weak_policy = {**policy, 'launcher': [*launcher[:-1], '[]']}
    with patch.dict(os.environ, controller_env):
        weak = supervision_repair.probe_restricted_launcher(fixture.config, fixture.tree,
            authorization={'policy': weak_policy, 'repo': fixture.config['repos'][0],
                           'worker': {'environment_allowlist': []}})
        assert weak == {'ok': False, 'status': 'restricted_launcher_unverified'}, weak
        proposal = supervision_repair.propose(fixture.config, incident)
        assert proposal['status'] == 'activated' and proposal['auto_status'] == 'verified', proposal
        fixture.bind(task_id=proposal['task_id'])
        process = fixture.launch(real_probe=True)
    try:
        receipt = broker_result(process)
        assert receipt['ok'] and fixture.counts() == (1, 2), receipt
        assert canary.read_text() == '{"fixture":"protected"}'
        revision = _audit_and_push(fixture, escape_marker)
        with patch.dict(os.environ, controller_env):
            proof = supervision_repair._code_proof(fixture.config, fixture.tree, {'revision': revision}, policy,
                                                   fixture.config['repos'][0])
        assert proof['status'] == 'verified', proof
        assert not escape_marker.exists()
    finally:
        stop_fixture(process)
    return {'status': 'passed', 'backend': 'sandbox-exec', 'pushed_revision': revision, 'checks': [
        'host_database_read_and_write_denied', 'host_runtime_marker_read_and_write_denied',
        'trusted_config_read_and_write_denied', 'workspace_read_and_write_permitted',
        'launcher_with_readable_controller_credentials_rejected', 'controller_credential_files_denied',
        'actual_repair_probe_passed', 'actual_restricted_model_submitted_through_broker',
        'model_committed_inside_restricted_launcher', 'malicious_git_config_and_hook_inert',
        'final_audit_and_failed_worker_inspection_safe', 'fixed_remote_local_bare_push_and_reconciliation',
        'late_policy_revocation_blocks_push',
        'isolated_exact_revision_verification_excludes_ignored_overlay',
    ]}


def run(root):
    scope = Path(root) / 'repair-scenarios'
    scope.mkdir()
    fixture = _backlog_and_recursion(scope / 'backlog')
    build = _build_evidence(scope, fixture)
    isolation = _sandbox(scope / 'isolation')
    return {'scenario': 'engineering_repair_provenance_and_isolation', 'passed': isolation['status'] == 'passed',
            'checks': ['idempotent_repair_backlog', 'repair_failure_does_not_recurse',
                       'clean_git_source_matches_fixture_wheel', 'runtime_build_byte_mismatch_rejected'],
            'build': build, 'restricted_launcher': isolation}
