"""runtime scan responsibilities."""
import argparse
from robert_agent.foundation.commands import emit

from robert_agent.delivery.application import lane as delivery_lane
from robert_agent.work.application import synchronization as synchronization
from robert_agent.operations import scan_batches as scan_batch_operations


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--publish', action='store_true')
    action.add_argument('--list-batches', action='store_true')
    action.add_argument('--retry-batch')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--refresh-context', action='store_true')
    args = parser.parse_args(argv)
    if args.list_batches or args.retry_batch:
        result = scan_batch_operations.manage_batches(args.config, retry=args.retry_batch, dry_run=args.dry_run)
    else:
        result = delivery_lane.publish(args.config, dry_run=args.dry_run) if args.publish else synchronization.scan(args.config, refresh_context=args.refresh_context)
    return emit(result, 0 if result.get('ok') else 3)



if __name__ == '__main__':
    raise SystemExit(main())
