from contextlib import closing
from datetime import datetime,timezone
import json,os,sqlite3,subprocess,time
from unittest import mock
from tests.test_run_once import RunOnceDryRunTests
from robert_agent import run_once

def scenario(expired):
 f=RunOnceDryRunTests(); f.setUp()
 try:
  with mock.patch.object(run_once,'_prepare_worktree',return_value=None):
   seed=run_once.run_once(f.config_path,fixture_path=f.fixture_path,dry_run=True,skip_external=True)
  db=f.data_dir/'dd.sqlite3'; now=datetime.now(timezone.utc).isoformat()
  with closing(sqlite3.connect(db)) as c,c:
   task,attempt,repo,ws=c.execute('SELECT t.task_id,a.attempt_id,w.repo_id,w.workstream_id FROM attempts a JOIN tasks t ON t.task_id=a.task_id JOIN workstreams w ON w.workstream_id=t.workstream_id').fetchone()
   c.execute("UPDATE attempts SET status='running',started_at=?,heartbeat_at=?,worktree_path=?,metadata_json='{}'",(now,now,str(f.repo_root)))
   c.execute("UPDATE tasks SET lifecycle='running'")
  def git(command,**kwargs):
   return subprocess.CompletedProcess(command,0,' M saved-progress.py' if command[1:]==['status','--short'] else '', '')
  environment=dict(os.environ)
  environment.pop('_ROBERT_CONTROL_DEADLINE',None)
  if expired: environment['_ROBERT_CONTROL_DEADLINE']=str(time.monotonic()-1)
  with mock.patch.dict(os.environ,environment,clear=True),mock.patch.object(run_once.subprocess,'run',side_effect=git),mock.patch.object(run_once,'_dispatch_process_status',return_value={'status':'dead'}),mock.patch.object(run_once,'_terminate_owned_attempt',return_value={'status':'already_dead'}):
   with closing(sqlite3.connect(db)) as c,c:
    n=run_once._supervise_running_attempts(c,repo,{},now,data_dir=f.data_dir,db_path=db,repo={'default_base_branch':'master'})
    return {'deadline_expired':expired,'supervised':n,'attempts':c.execute('SELECT status FROM attempts ORDER BY attempt_no').fetchall(),'task':c.execute('SELECT lifecycle FROM tasks').fetchone()[0],'owner':c.execute('SELECT binding_status FROM execution_subjects').fetchone()[0]}
 finally: f.doCleanups()
print(json.dumps([scenario(False),scenario(True)]))
