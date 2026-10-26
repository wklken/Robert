from unittest import mock
import subprocess,json,time,os,tempfile,pathlib
from robert_agent import daemon,worktree
p=mock.Mock(pid=43214321)
p.communicate.side_effect=subprocess.TimeoutExpired(['control'],.1)
with mock.patch.object(daemon.subprocess,'Popen',return_value=p), mock.patch.object(daemon.os,'killpg',side_effect=ProcessLookupError('group already exited')):
 try:
  outcome=daemon.run_child_json(['control'],timeout_seconds=.1)
  print(json.dumps({'timeout_race_outcome':outcome}))
 except Exception as exc:
  print(json.dumps({'timeout_race_exception':type(exc).__name__,'message':str(exc)}))
commands=[]
def runner(args,**kwargs):
 commands.append({'argv':args,'timeout':kwargs.get('timeout'),'deadline_expired':time.monotonic()>float(os.environ['_ROBERT_CONTROL_DEADLINE'])})
 return subprocess.CompletedProcess(args,0,'head' if 'pr-1-head' in args[-1] else 'base','')
with tempfile.TemporaryDirectory() as tmp, mock.patch.object(worktree,'_existing_worktree_for_branch',return_value=None), mock.patch.dict(os.environ,{'_ROBERT_CONTROL_DEADLINE':str(time.monotonic()-1)}):
 result=worktree.prepare_existing_pr_worktree(tmp,pathlib.Path(tmp)/'worktrees',source_number=1,head_branch='test',head_sha='head',base_branch='main',base_sha='base',dry_run=False,runner=runner)
print(json.dumps({'deadline_commands':commands,'deadline_outcome':result['status']}))
