"""Native PR review stages using the shared supervised model executor."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import signal
import subprocess

from robert_agent import review_files, review_workspace, worker_identity  # noqa: F401
from robert_agent import review_store as store
from robert_agent.model_execution import (StageCoordinator, ReviewStopped, ReviewSuperseded,
    _active, _group_finished, _wait_for_group_exit, terminate_children)  # noqa: F401
from robert_agent.worker import result as worker_result


def read_pr(repo, number):
    completed = subprocess.run(['gh', 'api', f'repos/{repo}/pulls/{number}'],
                               capture_output=True, text=True, check=True, timeout=60)
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or value.get('number') != number:
        raise ValueError('GitHub returned invalid PR metadata')
    return value


def same_revision(manifest, pr):
    return (pr.get('state') == 'open' and pr.get('head', {}).get('sha') == manifest['head_sha']
            and pr.get('base', {}).get('sha') == manifest['base_sha']
            and pr.get('base', {}).get('ref') == manifest['base_ref'])


class Coordinator(StageCoordinator):
    handoff_files = {'prepare': ('context-summary.md', 'source-index.json'),
                     'review': ('result.json',), 'synthesis': ('evaluation.json',)}

    def prepare(self):
        self.phase('preparing')
        self.reconcile_running()
        repo = self.request['repo']
        if not self.manifest:
            # Fetch races are bounded; no models have started and no bundle is sealed.
            for retry in range(2):
                pr = self.fetch_pr(repo['full_name'], self.session['pr_number'])
                if pr.get('state') != 'open':
                    raise ReviewSuperseded('PR is closed')
                expected_author = self.request['event'].get('pr_author_login', '')
                if pr.get('user', {}).get('login', '').casefold() != expected_author.casefold():
                    raise ValueError('PR author does not match the authorized review request')
                try:
                    self.manifest = review_workspace.prepare_snapshot(
                        repo['repo_root'], repo['worktree_root'],
                        self.data_dir / 'artifacts' / 'reviews' / hashlib.sha256(repo['full_name'].encode()).hexdigest()[:16],
                        self.round_id, pr, [self.request['event']],
                    )
                    if not self.manifest.get('frozen'):
                        self.manifest['worker_config_sha256'] = hashlib.sha256(
                            json.dumps(self.request['workers'], sort_keys=True).encode()).hexdigest()
                        review_files.atomic_json(Path(self.manifest['context_dir']) / 'manifest.json', self.manifest)
                    break
                except ValueError as exc:
                    if retry or 'changed while fetching' not in str(exc):
                        raise
            self.save_manifest()
        review_workspace.verify_snapshot(self.manifest)
        if not self.manifest.get('frozen'):
            prompt = self.prompt('prepare-prompt.md', review_files.build_prepare_prompt(self.manifest))

            def validate(output, model):
                summary = review_files.safe_path(output / 'context-summary.md', output)
                index = review_files.safe_path(output / 'source-index.json', output)
                if not summary.read_text(encoding='utf-8').strip():
                    raise ValueError('primary context summary is empty')
                source_index = json.loads(index.read_text(encoding='utf-8'))
                if (not isinstance(source_index, dict) or not isinstance(source_index.get('files'), list)
                        or not isinstance(source_index.get('reading_notes'), str)
                        or not source_index['reading_notes'].strip()):
                    raise ValueError('invalid primary source index')
                tree = Path(self.manifest['worktree_path'])
                for item in source_index['files']:
                    if not isinstance(item, dict) or not isinstance(item.get('reason'), str) or not item['reason'].strip():
                        raise ValueError('source index entry requires a reason')
                    name = review_files._source_path(item.get('path'))
                    if not review_files.safe_path(tree / name, tree).is_file():
                        raise ValueError('source index entry references missing source')
                return {'context_summary_path': str(summary), 'context_summary_sha256': store.digest(summary),
                        'source_index_path': str(index), 'source_index_sha256': store.digest(index)}

            result = self.run_phase('prepare', self.request['workers'][0], prompt, validate)
            for kind, name in [('context_summary', 'context-summary.md'), ('source_index', 'source-index.json')]:
                shutil.copyfile(result[kind + '_path'], Path(self.manifest['context_dir']) / name)
            self.manifest = review_workspace.freeze_context(self.manifest)
            self.save_manifest()
        if not same_revision(self.manifest, self.fetch_pr(repo['full_name'], self.session['pr_number'])):
            raise ReviewSuperseded('PR head/base changed after context preparation')

    def review(self):
        self.phase('reviewing')
        def review_one(worker):
            def validate(output, model, worker=worker):
                result_path = output / 'result.json'
                result = review_files.read_result(result_path, self.manifest)
                report_path = review_files.safe_path(output / 'report.md', output)
                report = review_files.render_individual(result, worker['name'], model)
                if report_path.exists() and report_path.read_text(encoding='utf-8') != report:
                    raise ValueError('derived review report changed')
                report_path.write_text(report, encoding='utf-8')
                return {'model': model, 'result_path': str(result_path),
                        'result_sha256': store.digest(result_path), 'report_path': str(report_path),
                        'report_sha256': store.digest(report_path)}
            try:
                return self.run_phase('review', worker, self.manifest['prompt_path'], validate), None
            except ReviewStopped:
                raise
            except (ValueError, OSError, RuntimeError) as exc:
                return None, f"{worker['name']}: {exc}"
        results = self.parallel(self.request['workers'], review_one)
        records = [record for record, error in results if record]
        failures = [error for record, error in results if error]
        if failures:
            raise RuntimeError('; '.join(failures))
        return records

    def synthesize(self, records):
        self.phase('synthesizing')
        inputs = review_files.write_synthesis_inputs(self.manifest, records)
        prompt = self.prompt(f"synthesis-prompt-{inputs['sha256']}.md",
                             review_files.build_synthesis_prompt(self.manifest, inputs['path']))

        def validate(output, model):
            if store.digest(inputs['path']) != inputs['sha256']:
                raise ValueError('synthesis inputs changed')
            path = output / 'evaluation.json'
            review_files.read_evaluation(path, self.manifest, records)
            return {'evaluation_path': str(path), 'evaluation_sha256': store.digest(path)}

        result = self.run_phase('synthesis', self.request['workers'][0], prompt, validate,
                                input_hash=inputs['sha256'])
        evaluation = review_files.read_evaluation(result['evaluation_path'], self.manifest, records)
        body = review_files.render_merged(self.manifest, records, evaluation)
        body += f'\n<!-- robert-review round={self.round_id} -->\n'
        path = Path(self.manifest['artifact_root']) / 'synthesis' / 'merged-review.md'
        path.write_text(body, encoding='utf-8')
        review_workspace.verify_snapshot(self.manifest)
        self.check_active()
        with store.connect(self.db_path) as conn:
            store.update(conn, self.round_id, status='ready', report_path=str(path), report_sha256=store.digest(path))
            conn.execute('''INSERT OR REPLACE INTO artifacts(artifact_id,task_id,attempt_id,
                artifact_type,path,sha256,bytes,created_at) VALUES (?,?,?,?,?,?,?,?)''',
                (self.round_id + ':merged', self.task_id, self.attempt_id, 'review_report', str(path),
                 store.digest(path), path.stat().st_size, store.now()))
        action = {'type': 'submit_pr_review', 'target_url': f"https://github.com/{self.session['repo']}/pull/{self.session['pr_number']}",
                  'body': body, 'round_id': self.round_id, 'commit_id': self.manifest['head_sha'],
                  'base_sha': self.manifest['base_sha'], 'base_ref': self.manifest['base_ref'],
                  'report_path': str(path), 'report_sha256': store.digest(path)}
        payload = worker_result.build_result(self.task_id, self.attempt_id, 'multi_agent_review_report',
                    [self.session['trigger_fingerprint']], [], f'Review report: {path}', [action], used_skills=[])
        recorded = worker_result.record_result(self.db_path, payload)
        if not recorded.get('ok'):
            raise RuntimeError('review result was rejected: ' + recorded.get('status', 'unknown'))
        return path

    def run(self):
        try:
            self.prepare()
            return self.synthesize(self.review())
        except Exception as exc:
            with store.connect(self.db_path) as conn:
                status = 'superseded' if isinstance(exc, ReviewSuperseded) else 'failed'
                if isinstance(exc, ReviewStopped):
                    status = 'canceled' if not _active(conn, self.task_id, self.attempt_id) else 'attention'
                store.update(conn, self.round_id, status=status, error=str(exc))
            with store.connect(self.db_path) as conn:
                terminate_children(conn, self.task_id)
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--attempt-id', required=True)
    args = parser.parse_args(argv)

    coordinator = Coordinator(args.db, args.task_id, args.attempt_id)
    def stop(_signum, _frame):
        # Finish any in-flight spawn and persist its identity before cleanup.
        coordinator.stopping.set()
    signal.signal(signal.SIGTERM, stop)
    coordinator.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
