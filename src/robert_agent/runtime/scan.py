"""runtime scan responsibilities."""

import argparse
from robert_agent.foundation.commands import emit

from robert_agent.application import publication_cycle as delivery_lane
from robert_agent.work.application import synchronization as synchronization
from robert_agent.operations import scan_batches as scan_batch_operations
from robert_agent.runtime.native_sources import stop_signals


def main(argv=None, *, tapd_client_factory=None, tapd_observation_provider=None, tapd_identity_provider=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--publish", action="store_true")
    action.add_argument("--list-batches", action="store_true")
    action.add_argument("--retry-batch")
    parser.add_argument("--dry-run", action="store_true")
    publication_scope = parser.add_mutually_exclusive_group()
    publication_scope.add_argument("--source-only", action="store_true", help=argparse.SUPPRESS)
    publication_scope.add_argument("--native-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-reconcile-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--refresh-context", action="store_true")
    args = parser.parse_args(argv)
    if (args.source_only or args.native_only or args.source_reconcile_only) and not args.publish:
        parser.error("source publication modes require --publish")
    if args.list_batches or args.retry_batch:
        result = scan_batch_operations.manage_batches(args.config, retry=args.retry_batch, dry_run=args.dry_run)
    elif args.publish:
        with stop_signals() as stop:
            mode = {"source_reconcile_only": True} if args.source_reconcile_only else {}
            if args.native_only:
                mode["native_only"] = True
            injected = {
                key: value
                for key, value in {
                    "tapd_client_factory": tapd_client_factory,
                    "tapd_observation_provider": tapd_observation_provider,
                    "tapd_identity_provider": tapd_identity_provider,
                }.items()
                if value is not None
            }
            result = delivery_lane.publish(
                args.config, dry_run=args.dry_run, source_only=args.source_only, stop=stop, **mode, **injected
            )
    else:
        result = synchronization.scan(args.config, refresh_context=args.refresh_context)
    return emit(result, 0 if result.get("ok") else 3)


if __name__ == "__main__":
    raise SystemExit(main())
