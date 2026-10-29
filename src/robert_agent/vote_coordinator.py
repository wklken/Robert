"""Native preparation, independent ballots and synthesis; no direct GitHub writes."""
import argparse
import json
from pathlib import Path
import signal

from robert_agent import vote_files, vote_store, vote_tally, vote_workspace
from robert_agent.model_execution import StageCoordinator, ReviewStopped, ReviewSuperseded, _active, terminate_children
from robert_agent.worker import result as worker_result


class Coordinator(StageCoordinator):
    store = vote_store
    workspace = vote_workspace
    voter_stage = 'vote'
    handoff_files = {'prepare': ('prepared-request.json',), 'vote': ('ballot.json',), 'synthesis': ('synthesis.json',)}
    artifact_kinds = ('prepared', 'ballot', 'synthesis')
    workflow_name = 'vote'

    def __init__(self, *args, source_reader=vote_workspace.read_source, **kwargs):
        super().__init__(*args, **kwargs)
        self.source_reader = source_reader

    def read_source(self):
        return self.source_reader(self.session['repo'], self.session['number'], self.session['source_type'])

    def check_sources(self):
        if not vote_workspace.same_sources(self.manifest, self.read_source()):
            raise ReviewSuperseded('vote sources or PR revision changed')

    def prepare(self):
        self.phase('preparing')
        self.reconcile_running()
        if not self.manifest:
            self.manifest = vote_workspace.prepare(self.request['repo'], self.data_dir, self.round_id,
                                                  self.request['event'], self.request['workers'], self.read_source())
            self.save_manifest()
        vote_workspace.verify_snapshot(self.manifest)
        self.check_sources()
        for _ in range(2):
            if self.manifest.get('frozen'):
                return True
            prompt = self.prompt('prepare-' + self.manifest['snapshot_id'] + '.md', vote_files.prepare_prompt(self.manifest))
            def validate(output, model):
                path = output / 'prepared-request.json'
                vote_files.read_prepared(path, self.manifest)
                return {'prepared_path': str(path), 'prepared_sha256': vote_files.digest(path)}
            result = self.run_phase('prepare', self.request['workers'][0], prompt, validate)
            prepared = vote_files.read_prepared(result['prepared_path'], self.manifest)
            if prepared['status'] == 'needs_code':
                if self.manifest.get('source_worktree'):
                    raise ValueError('source already provided; preparation must identify a concrete ambiguity')
                vote_workspace.add_issue_code(self.manifest, self.request['repo'])
                self.save_manifest()
                continue
            if prepared['status'] == 'needs_clarification':
                target = self.target_url()
                body = prepared['question']
                payload = worker_result.build_result(self.task_id, self.attempt_id, 'waiting_for_user',
                    [self.session['trigger_fingerprint']], [], prepared['question'],
                    [{'type': 'comment', 'target_url': target, 'body': body}], used_skills=[],
                    operator_question={'kind': 'clarification', 'summary': prepared['question']})
                self.phase('waiting_for_user')
                self.record(payload)
                return False
            self.manifest = vote_workspace.freeze(self.manifest, prepared)
            self.save_manifest()
            self.check_sources()
            return True
        raise ValueError('preparation did not produce a voting question')

    def target_url(self):
        kind = 'pull' if self.session['source_type'] == 'pull_request' else 'issues'
        return f"https://github.com/{self.session['repo']}/{kind}/{self.session['number']}"

    def vote(self):
        self.phase('voting')
        def vote_one(worker):
            def validate(output, model):
                path = output / 'ballot.json'
                vote_files.read_ballot(path, self.manifest)
                return {'ballot_path': str(path), 'ballot_sha256': vote_files.digest(path), 'adapter': worker['agent']}
            try:
                return self.run_phase('vote', worker, self.manifest['prompt_path'], validate), None
            except ReviewStopped:
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                return None, f"{worker['name']}: {exc}"
        outcomes = self.parallel(self.request['workers'], vote_one)
        failures = [error for _, error in outcomes if error]
        if failures:
            raise RuntimeError('; '.join(failures))
        return [record for record, _ in outcomes]

    def synthesize(self, records):
        self.phase('synthesizing')
        ballots = vote_files.selected_ballots(self.manifest, records)
        tally = vote_tally.tally(self.manifest['spec'], ballots)
        root = Path(self.manifest['artifact_root']) / 'synthesis'
        root.mkdir(exist_ok=True)
        inputs_value = {'round_id': self.round_id, 'snapshot_id': self.manifest['snapshot_id'],
                        'records': records, 'tally': tally}
        encoded = json.dumps(inputs_value, sort_keys=True).encode()
        import hashlib
        inputs_hash = hashlib.sha256(encoded).hexdigest()
        inputs = root / f'inputs-{inputs_hash}.json'
        # Hash the bytes actually consumed by synthesis, not an alternate serialization.
        if not inputs.exists():
            inputs.write_bytes(encoded)
        if vote_files.digest(inputs) != inputs_hash:
            raise ValueError('synthesis inputs changed')
        prompt = self.prompt(f'synthesis-{inputs_hash}.md',
                             vote_files.synthesis_prompt(self.manifest, inputs, inputs_hash, records))
        def validate(output, model):
            if vote_files.digest(inputs) != inputs_hash:
                raise ValueError('synthesis inputs changed')
            path = output / 'synthesis.json'
            vote_files.read_synthesis(path, self.manifest, records, inputs_hash)
            return {'synthesis_path': str(path), 'synthesis_sha256': vote_files.digest(path)}
        result = self.run_phase('synthesis', self.request['workers'][0], prompt, validate, input_hash=inputs_hash)
        ballots = vote_files.selected_ballots(self.manifest, records)
        summary = vote_files.read_synthesis(result['synthesis_path'], self.manifest, records, inputs_hash)
        body = vote_files.render(self.manifest, records, ballots, tally, summary,
                                 task_id=self.task_id, attempt_id=self.attempt_id)
        report = root / 'vote-report.md'
        report.write_text(body)
        self.check_sources()
        self.check_active()
        vote_workspace.verify_snapshot(self.manifest)
        with vote_store.connect(self.db_path) as conn:
            vote_store.update(conn, self.round_id, status='ready', report_path=str(report), report_sha256=vote_files.digest(report))
        action = {'type': 'comment', 'target_url': self.target_url(), 'body': body}
        payload = worker_result.build_result(self.task_id, self.attempt_id, 'multi_agent_vote_report',
                    [self.session['trigger_fingerprint']], [], f'Vote report: {report}', [action], used_skills=[])
        self.record(payload)
        return report

    def record(self, payload):
        result = worker_result.record_result(self.db_path, payload)
        if not result.get('ok'):
            raise RuntimeError('vote result rejected: ' + result.get('status', 'unknown'))

    def run(self):
        try:
            if not self.prepare():
                return None
            return self.synthesize(self.vote())
        except Exception as exc:
            with vote_store.connect(self.db_path) as conn:
                status = 'superseded' if isinstance(exc, ReviewSuperseded) else 'failed'
                if isinstance(exc, ReviewStopped):
                    status = 'canceled' if not _active(conn, self.task_id, self.attempt_id) else 'attention'
                vote_store.update(conn, self.round_id, status=status, error=str(exc))
            with vote_store.connect(self.db_path) as conn:
                terminate_children(conn, self.task_id, store=vote_store)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ('db', 'task-id', 'attempt-id'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    coordinator = Coordinator(args.db, args.task_id, args.attempt_id)
    signal.signal(signal.SIGTERM, lambda *_: coordinator.stopping.set())
    coordinator.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
