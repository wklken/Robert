from contextlib import closing
from unittest import mock
import sqlite3,subprocess,os,json
from tests.test_operational_commands import OperationalCommandTests
from robert_agent import run_once,audit_result
f=OperationalCommandTests();f.setUp()
try:
 db=f.root/'dd.sqlite3';f._init_summary_db(db)
 def git(command,**kwargs):
  return subprocess.CompletedProcess(command,0,'a'*40 if command[1]=='rev-parse' else 'git@github.com:wklken/backend.git','')
 env=dict(os.environ);env.pop('_ROBERT_CONTROL_DEADLINE',None)
 results=[]
 for expired in (False,True):
  with mock.patch.dict(os.environ,{**env,**({'_ROBERT_CONTROL_DEADLINE':'0'} if expired else {})},clear=True),mock.patch.object(run_once.subprocess,'run',side_effect=git),closing(sqlite3.connect(db)) as c:
   try:
    scope=run_once._action_scope_for_result(c,'task-1','attempt-1','ws-1')
   except subprocess.TimeoutExpired as exc:
    results.append({'deadline_expired':expired,'exception':type(exc).__name__,'action_audit_status':c.execute('SELECT audit_status FROM github_actions').fetchone()[0]})
    continue
   action={'type':'open_pr','repo':scope['repo'],'base':scope['base_branch'],'head':'wklken:'+scope['branch_name']}
   results.append({'deadline_expired':expired,'head_owner':scope.get('head_owner'),'violation':audit_result.action_scope_violation([action],scope)})
 print(json.dumps(results))
finally:f.doCleanups()
