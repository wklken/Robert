"""Controller-owned Git views for the model-writable maintenance checkout."""

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile

from robert_agent.common import run_command

_SHA = re.compile(r'[0-9a-f]{40}')
_BRANCH = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9/_-]{0,190}')
_REPO = re.compile(r'[a-zA-Z0-9_-]+/[a-zA-Z0-9_][a-zA-Z0-9_.-]*')
_HEX_DIRECTORY = re.compile(r'[0-9a-f]{2}')
_LOOSE_OBJECT = re.compile(r'[0-9a-f]{38}')
_PACK_FILE = re.compile(r'pack-[0-9a-f]{40}\.(?:pack|idx)')
_MAX_OBJECT_BYTES = 128 * 1024 * 1024
_MAX_OBJECT_FILES = 20_000
_GIT = shutil.which('git', path=os.defpath) or '/usr/bin/git'


def canonical_remote(repo):
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo.endswith('.git'):
        raise OSError('Invalid maintenance repository')
    return f'https://github.com/{repo}.git'


def binding(scope):
    """Read the trusted attempt binding and reject any incomplete or stale scope."""
    value = scope.get('repair_git')
    if not isinstance(value, dict):
        raise OSError('Maintenance Git binding unavailable')
    tree, branch, base, remote, private = (
        value.get(key) for key in ('worktree', 'branch', 'base_sha', 'remote', 'private_root')
    )
    if (not isinstance(tree, str) or not Path(tree).is_absolute()
            or tree != scope.get('worktree_path')
            or not isinstance(branch, str) or not _BRANCH.fullmatch(branch)
            or branch != scope.get('branch_name')
            or not isinstance(base, str) or not _SHA.fullmatch(base)
            or remote != canonical_remote(scope.get('repo'))
            or not isinstance(private, str) or not Path(private).is_absolute()
            or Path(private).is_relative_to(tree) or Path(tree).is_relative_to(private)):
        raise OSError('Maintenance Git binding invalid')
    return value


def _git_environment(protocol=''):
    # A controller invocation must never discover configuration from either
    # the agent environment or the agent's checkout.
    return {
        'PATH': os.defpath,
        'LANG': 'C',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull,
        'GIT_NO_REPLACE_OBJECTS': '1',
        'GIT_OPTIONAL_LOCKS': '0',
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_ALLOW_PROTOCOL': protocol,
    }


def _run(directory, args, *, runner=subprocess.run, protocol='', auth=False, index=None,
         timeout=15):
    env = _git_environment(protocol)
    options = [
        '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
        '-c', 'core.attributesFile=' + os.devnull, '-c', 'core.excludesFile=' + os.devnull,
        '-c', 'core.filemode=true', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
        '-c', 'http.followRedirects=false', '-c', 'credential.helper=',
    ]
    if auth and (helper := shutil.which('gh')):
        for name in ('HOME', 'GH_CONFIG_DIR', 'GH_TOKEN', 'GITHUB_TOKEN'):
            if name in os.environ:
                env[name] = os.environ[name]
        options.extend(('-c', 'credential.helper=!' + shlex.quote(os.path.abspath(helper)) + ' auth git-credential'))
    if index is not None:
        env['GIT_INDEX_FILE'] = str(index)
    return run_command([_GIT, *options, *args], runner=runner, cwd=directory, env=env,
                       capture_output=True, text=True, timeout=timeout)


def _checked(result):
    if result.returncode:
        raise OSError('Maintenance Git inspection failed')
    return result.stdout.strip()


@contextmanager
def _directory(name, *, parent=None):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _regular(parent, name, maximum):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise OSError('Invalid maintenance Git metadata file')
        yield stream, before.st_size
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError('Maintenance Git metadata changed during inspection')


def _read(parent, name, limit):
    with _regular(parent, name, limit) as (stream, size):
        value = stream.read(size + 1)
        if len(value) != size:
            raise OSError('Maintenance Git metadata changed during inspection')
        try:
            return value.decode('ascii').strip()
        except UnicodeError as exc:
            raise OSError('Invalid maintenance Git metadata encoding') from exc


def _branch_head(metadata, expected_branch=None):
    head = _read(metadata, 'HEAD', 256)
    if expected_branch is None and _SHA.fullmatch(head):
        return head
    prefix = 'ref: refs/heads/'
    if not head.startswith(prefix):
        raise OSError('Invalid maintenance HEAD')
    branch = head[len(prefix):]
    if not _BRANCH.fullmatch(branch) or (expected_branch is not None and branch != expected_branch):
        raise OSError('Maintenance branch changed')
    parts = ['refs', 'heads', *branch.split('/')]
    try:
        with ExitStack() as stack:
            parent = metadata
            for part in parts[:-1]:
                parent = stack.enter_context(_directory(part, parent=parent))
            revision = _read(parent, parts[-1], 64)
    except FileNotFoundError:
        ref = head.removeprefix('ref: ')
        packed_refs = _read(metadata, 'packed-refs', 1024 * 1024)
        revision = next((line[:40] for line in packed_refs.splitlines() if line[40:] == ' ' + ref), '')
    if not _SHA.fullmatch(revision):
        raise OSError('Invalid maintenance revision')
    return revision


def _copy_file(parent, name, target, maximum):
    with _regular(parent, name, maximum) as (source, size), target.open('xb') as output:
        remaining = size
        while remaining:
            data = source.read(min(remaining, 64 * 1024))
            if not data:
                raise OSError('Maintenance Git metadata changed during inspection')
            output.write(data)
            remaining -= len(data)
    return size


def _copy_objects(metadata, destination):
    remaining = _MAX_OBJECT_BYTES
    count = 0
    with _directory('objects', parent=metadata) as objects, os.scandir(objects) as entries:
        for entry in entries:
            count += 1
            if count > _MAX_OBJECT_FILES:
                raise OSError('Maintenance Git metadata entry count exceeded')
            name = entry.name
            if name == 'pack':
                pattern = _PACK_FILE
            elif _HEX_DIRECTORY.fullmatch(name):
                pattern = _LOOSE_OBJECT
            else:
                # Includes objects/info/alternates. These are never imported.
                continue
            directory = destination / name
            directory.mkdir(exist_ok=True)
            with _directory(name, parent=objects) as source, os.scandir(source) as files:
                for item in files:
                    count += 1
                    if count > _MAX_OBJECT_FILES:
                        raise OSError('Maintenance Git metadata entry count exceeded')
                    if not pattern.fullmatch(item.name):
                        continue
                    remaining -= _copy_file(source, item.name, directory / item.name, remaining)


def require_standalone(checkout):
    path = Path(checkout)
    if not path.is_absolute() or path.resolve() != path:
        raise OSError('Invalid maintenance checkout')
    with _directory(checkout) as worktree, _directory('.git', parent=worktree):
        pass


@contextmanager
def _view(checkout, private_root, *, branch=None, runner=subprocess.run, copy_index=False):
    # The cwd and the entire Git directory are private. Model data enter only
    # through regular Git object files, a validated ref, and an optional index.
    checkout = Path(checkout)
    if not checkout.is_absolute() or checkout.resolve() != checkout:
        raise OSError('Invalid maintenance checkout')
    with tempfile.TemporaryDirectory(prefix='.robert-git-view-', dir=private_root) as temp:
        private = Path(temp)
        _checked(_run(private, ['init', '--bare', '--template=', '--object-format=sha1', '.'], runner=runner))
        with _directory(checkout) as worktree, _directory('.git', parent=worktree) as metadata:
            revision = _branch_head(metadata, branch)
            _copy_objects(metadata, private / 'objects')
            if copy_index:
                _copy_file(metadata, 'index', private / 'model-index', 8 * 1024 * 1024)
        object_type = _checked(_run(private, ['cat-file', '-t', revision], runner=runner))
        if object_type != 'commit':
            raise OSError('Maintenance revision is not a commit')
        _checked(_run(private, ['fsck', '--strict', '--no-reflogs', '--no-dangling', revision], runner=runner))
        (private / 'HEAD').write_text(revision + '\n', encoding='ascii')
        yield private, revision


def _status(private, checkout, revision, runner):
    entries = _checked(_run(private, ['ls-tree', '-r', '-z', revision], runner=runner))
    if any(entry.startswith('160000 ') for entry in entries.split('\0')):
        raise OSError('Maintenance checkout contains submodules')
    common = ['--work-tree=' + str(checkout)]
    _checked(_run(private, [*common, 'read-tree', revision], runner=runner, index=private / 'fresh-index'))
    unstaged = _checked(_run(private, [*common, 'status', '--porcelain=v1', '--untracked-files=normal',
                                      '--ignore-submodules=all'], runner=runner, index=private / 'fresh-index'))
    staged = _checked(_run(private, [*common, 'diff', '--cached', '--name-only', '--no-ext-diff', '--no-textconv',
                                    revision, '--'], runner=runner, index=private / 'model-index'))
    return 'dirty' if unstaged or staged else 'clean'


def _validate_base(private, base, revision, runner):
    _checked(_run(private, ['merge-base', '--is-ancestor', base, revision], runner=runner))


def observe(scope, *, runner=subprocess.run):
    value = binding(scope)
    checkout = value['worktree']
    with _view(checkout, value['private_root'], branch=value['branch'], runner=runner,
               copy_index=True) as (private, revision):
        _validate_base(private, value['base_sha'], revision, runner)
        return revision, _status(private, checkout, revision, runner)


def clean_revision(checkout, revision, private_root):
    with _view(checkout, private_root, copy_index=True) as (private, head):
        return head == revision and _status(private, checkout, head, subprocess.run) == 'clean'


@contextmanager
def materialized_checkout(checkout, revision, private_root, worktree_root):
    """Check out only the audited commit for a restricted verification command."""
    with _view(checkout, private_root) as (private, head):
        if head != revision:
            raise OSError('Maintenance checkout moved before verification')
        branch = 'robert-verify'
        _checked(_run(private, ['update-ref', 'refs/heads/' + branch, revision]))
        with tempfile.TemporaryDirectory(prefix='.robert-repair-verify-', dir=worktree_root) as directory:
            isolated = Path(directory)
            _checked(_run(private, ['clone', '--no-local', '--no-tags', '--template=',
                                    '--single-branch', '--branch', branch, '--',
                                    str(private), str(isolated)], protocol='file', timeout=60))
            _checked(_run(isolated, ['remote', 'remove', 'origin']))
            yield isolated


def _remote_call(private, args, runner):
    return _run(private, args, runner=runner, protocol='https', auth=True, timeout=30)


def push(scope, revision, *, before_push, runner=subprocess.run):
    value = binding(scope)
    checkout, branch, remote = (value[key] for key in ('worktree', 'branch', 'remote'))
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise OSError('Missing audited maintenance revision')
    with _view(checkout, value['private_root'], branch=branch, runner=runner) as (private, head):
        if head != revision:
            raise OSError('Maintenance checkout moved after audit')
        _validate_base(private, value['base_sha'], revision, runner)
        if not before_push():
            return None
        return _remote_call(private, ['push', '--no-verify', remote, revision + ':refs/heads/' + branch], runner)


def remote_branch(scope, *, runner=subprocess.run):
    value = binding(scope)
    with tempfile.TemporaryDirectory(prefix='.robert-git-remote-', dir=value['private_root']) as temp:
        remote, branch = value['remote'], value['branch']
        return _remote_call(Path(temp), ['ls-remote', '--heads', remote, 'refs/heads/' + branch], runner)


def source_git_directory(source):
    raw = _checked(_run(source, ['rev-parse', '--path-format=absolute', '--git-common-dir']))
    return Path(raw).resolve(strict=True)


def prepare(repo, task_id, private_root, *, dry_run=False):
    source = Path(repo['repo_root']).resolve()
    root = Path(repo['worktree_root']).resolve()
    private = Path(private_root).resolve()
    branch = 'robert/maintenance-' + task_id.removeprefix('task-')
    if (not _BRANCH.fullmatch(branch) or root == source or root.is_relative_to(source)
            or root.is_relative_to(private) or private.is_relative_to(root)):
        raise OSError('Invalid maintenance checkout location')
    remote = canonical_remote(repo['full_name'])
    if dry_run:
        return {'ok': True, 'branch_name': branch, 'worktree_path': str(root / branch.replace('/', '-'))}
    root.mkdir(parents=True, exist_ok=True)
    # Never reuse a pre-existing model-controlled Git directory. The actual
    # checkout is an independent clone, not a linked worktree.
    checkout = Path(tempfile.mkdtemp(prefix=branch.replace('/', '-') + '-', dir=root))
    try:
        base = repo['default_base_branch']
        _checked(_run(root, ['clone', '--no-local', '--no-tags', '--template=', '--single-branch',
                             '--branch', base, '--', str(source), str(checkout)], protocol='file', timeout=60))
        _checked(_run(checkout, ['switch', '-c', branch]))
        _checked(_run(checkout, ['remote', 'set-url', 'origin', remote]))
        revision = _checked(_run(checkout, ['rev-parse', 'HEAD']))
        value = {'worktree': str(checkout), 'branch': branch, 'base_sha': revision,
                 'remote': remote, 'private_root': str(private)}
        return {'ok': True, 'branch_name': branch, 'worktree_path': str(checkout), 'repair_git': value}
    except (OSError, subprocess.SubprocessError):
        shutil.rmtree(checkout, ignore_errors=True)
        raise
