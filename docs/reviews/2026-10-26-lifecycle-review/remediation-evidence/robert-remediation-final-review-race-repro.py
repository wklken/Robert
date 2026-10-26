from contextlib import closing
from datetime import datetime,timezone
import sqlite3,json
from unittest import mock
from tests.test_reliability import DurableResultTests
from robert_agent import run_once,execution_store
from robert_agent.worker import result
f=DurableResultTests(); f.setUp()
try:
 now=datetime.now(timezone.utc).isoformat()
 with closing(sqlite3.connect(f.db)) as c,c:
  c.execute("UPDATE attempts SET status='running',started_at=?,heartbeat_at=?",(now,now))
  task,repo=c.execute('SELECT t.task_id,w.repo_id FROM tasks t JOIN workstreams w ON w.workstream_id=t.workstream_id').fetchone()
  execution_store.ensure_subject(c,'task',task,'human_request',now)
 receipts=[]
 def inspect(*args,**kwargs):
  receipts.append(result.record_result(f.db,f.payload))
  return {'status':'no_progress','commands':[],'worktree_path':'','progress_signals':[],'error_class':None}
 with closing(sqlite3.connect(f.db)) as c,c:
  with mock.patch.object(run_once,'_dispatch_process_status',return_value={'status':'dead'}), mock.patch.object(run_once,'_inspect_attempt_recovery_with_metadata',side_effect=inspect), mock.patch.object(run_once,'_terminate_owned_attempt',return_value={'status':'already_dead'}):
   n=run_once._supervise_running_attempts(c,repo,{},now)
  print(json.dumps({'receipts':receipts,'supervised':n,'attempt':c.execute('SELECT status FROM attempts').fetchone(),'task':c.execute('SELECT lifecycle FROM tasks').fetchone(),'worker_results':c.execute('SELECT count(*) FROM worker_results').fetchone()}))
finally: f.doCleanups()
