import sys, json, tempfile, sqlite3, tracemalloc, time, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
from robert_agent import storage, review_store, review_files
from robert_agent.worker import heartbeat, snapshot
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp); db=root/'db'; storage.init_database(db)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO repos(repo_id,full_name,github_account,default_base_branch,repo_root,worktree_root) VALUES ('r','a/b','bot','main','/repo','/worktrees')")
        c.execute("INSERT INTO workstreams(workstream_id,repo_id,lifecycle,created_at,updated_at) VALUES ('w','r','active','now','now')")
        c.execute("INSERT INTO tasks(task_id,workstream_id,lifecycle,created_at,updated_at) VALUES ('t','w','running','now','now')")
        c.execute("INSERT INTO attempts(attempt_id,task_id,attempt_no,status) VALUES ('a','t',1,'running')")
    start=time.monotonic()
    command=heartbeat.run_command_with_heartbeat(db,'t','a','verify',[sys.executable,'-c','import time; time.sleep(.02)'],heartbeat_interval_seconds=1.5,timeout_seconds=3)
    command_elapsed=time.monotonic()-start
    for _ in range(3): snapshot.record_snapshot(db,'t','a','analyze','running','Working','Wait')
    with sqlite3.connect(db) as c:
        phases=c.execute("SELECT count(*) FROM worker_phases WHERE phase='analyze'").fetchone()[0]
    path=root/'large.bin'; path.write_bytes(b'x'*(16*1024*1024))
    tracemalloc.start(); digest=review_files.sha256_file(path); _,peak=tracemalloc.get_traced_memory(); tracemalloc.stop()
    with sqlite3.connect(db) as c:
        c.executemany("""INSERT INTO review_runs(run_id,round_id,stage,worker_id,attempt_no,owner_attempt_id,status,output_dir,prompt_sha256,created_at,updated_at)
        VALUES (?,'round','review','worker',?,'a','completed',?,'sha','now','now')""",((str(i),i,str(i)) for i in range(100000)))
        sql='SELECT count(*) FROM review_runs WHERE slot_reserved=1'
        plan=[r[3] for r in c.execute('EXPLAIN QUERY PLAN '+sql)]
        c.execute(sql).fetchone(); start=time.perf_counter()
        for _ in range(50): c.execute(sql).fetchone()
        query_ms=(time.perf_counter()-start)*1000/50
    print(json.dumps({'source':review_files.__file__,'command_ok':command['ok'],'heartbeat_interval_seconds':1.5,'short_command_elapsed_seconds':round(command_elapsed,4),'identical_phase_samples':3,'stored_phase_rows':phases,'hash_file_bytes':path.stat().st_size,'hash_peak_bytes':peak,'hash_digest':digest,'history_rows':100000,'active_query_plan':plan,'active_query_average_ms':round(query_ms,4)}))
