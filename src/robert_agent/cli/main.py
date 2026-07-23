import argparse
from collections.abc import Sequence
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys

from robert_agent import __version__
from robert_agent import daemon
from robert_agent import diagnostics
from robert_agent import migrate
from robert_agent import run_once
from robert_agent import status
from robert_agent import service
from robert_agent import validate_config
from robert_agent import web
from robert_agent.cli import exit_codes
from robert_agent.cli.output import emit_result
from robert_agent.doctor import doctor
from robert_agent.init_config import init_config
from robert_agent.integrations import openclaw
from robert_agent.paths import default_config_path, default_data_dir
from robert_agent.resource_files import resource


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="Robert configuration path.",
    )


def _add_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="robert",
        description="Robert — Your Repo Teammate",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"robert {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init",
        help="Create a Robert configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  robert init --non-interactive "
            "--repo example/backend --repo-path /srv/repos/backend "
            "--worker codex --github-account robert-bot "
            "--trusted-actor maintainer"
        ),
    )
    _add_config_argument(init_parser)
    init_parser.add_argument("--non-interactive", action="store_true")
    init_parser.add_argument("--repo")
    init_parser.add_argument("--repo-path")
    init_parser.add_argument("--worker")
    init_parser.add_argument("--github-account")
    init_parser.add_argument("--trusted-actor")
    init_parser.add_argument("--force", action="store_true")
    _add_output_argument(init_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Validate runtime readiness.",
    )
    _add_config_argument(doctor_parser)
    doctor_parser.add_argument("--skip-external", action="store_true")
    _add_output_argument(doctor_parser)

    status_parser = subparsers.add_parser(
        "status",
        help="Show control-plane status.",
    )
    _add_config_argument(status_parser)
    _add_output_argument(status_parser)

    run_parser = subparsers.add_parser("run", help="Run or inspect cycles.")
    run_subparsers = run_parser.add_subparsers(
        dest="run_command",
        required=True,
    )
    run_once_parser = run_subparsers.add_parser(
        "once",
        help="Execute one bounded Robert cycle.",
    )
    _add_config_argument(run_once_parser)
    run_once_parser.add_argument("--dry-run", action="store_true")
    run_once_parser.add_argument("--skip-external", action="store_true")
    run_once_parser.add_argument("--skip-publish", action="store_true")
    run_show_parser = run_subparsers.add_parser(
        "show",
        help="Show one recorded run.",
    )
    _add_config_argument(run_show_parser)
    _add_output_argument(run_show_parser)
    run_show_parser.add_argument("run_id")

    task_parser = subparsers.add_parser("task", help="Inspect tasks.")
    task_show_parser = task_parser.add_subparsers(
        dest="task_command",
        required=True,
    ).add_parser("show")
    _add_config_argument(task_show_parser)
    _add_output_argument(task_show_parser)
    task_show_parser.add_argument("task_id")

    artifact_parser = subparsers.add_parser(
        "artifact",
        help="Inspect registered artifacts.",
    )
    artifact_show_parser = artifact_parser.add_subparsers(
        dest="artifact_command",
        required=True,
    ).add_parser("show")
    _add_config_argument(artifact_show_parser)
    _add_output_argument(artifact_show_parser)
    artifact_show_parser.add_argument("task_id")
    artifact_show_parser.add_argument("artifact_type")

    config_parser = subparsers.add_parser(
        "config",
        help="Inspect configuration.",
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_command",
        required=True,
    )
    for name in ["validate", "show", "path"]:
        command_parser = config_subparsers.add_parser(name)
        _add_config_argument(command_parser)
        _add_output_argument(command_parser)

    daemon_parser = subparsers.add_parser("daemon", help="Run the daemon.")
    daemon_run_parser = daemon_parser.add_subparsers(
        dest="daemon_command",
        required=True,
    ).add_parser("run")
    _add_config_argument(daemon_run_parser)
    daemon_run_parser.add_argument("--dry-run", action="store_true")
    daemon_run_parser.add_argument("--once", action="store_true")

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Migrate legacy Robert state.",
    )
    migrate_command = migrate_parser.add_subparsers(
        dest="migrate_command",
        required=True,
    ).add_parser("dd-github-agent")
    migrate_command.add_argument(
        "--source",
        default="~/.agents/data/dd-github-agent",
    )
    migrate_command.add_argument(
        "--target",
        default=str(default_data_dir()),
    )
    migrate_command.add_argument("--dry-run", action="store_true")
    _add_output_argument(migrate_command)

    service_parser = subparsers.add_parser(
        "service",
        help="Manage the Robert user service.",
    )
    service_subparsers = service_parser.add_subparsers(
        dest="service_command",
        required=True,
    )
    for name in [
        "install",
        "uninstall",
        "start",
        "stop",
        "restart",
        "status",
    ]:
        command_parser = service_subparsers.add_parser(name)
        command_parser.add_argument("--dry-run", action="store_true")
        _add_output_argument(command_parser)
        if name == "install":
            _add_config_argument(command_parser)

    diagnostics_parser = subparsers.add_parser(
        "diagnostics",
        help="Export safe diagnostics.",
    )
    diagnostics_export = diagnostics_parser.add_subparsers(
        dest="diagnostics_command",
        required=True,
    ).add_parser("export")
    _add_config_argument(diagnostics_export)
    diagnostics_export.add_argument(
        "--output",
        dest="output_path",
        default="robert-diagnostics.zip",
    )

    web_parser = subparsers.add_parser(
        "web",
        help="Run the local Robert web UI.",
    )
    web_run = web_parser.add_subparsers(
        dest="web_command",
        required=True,
    ).add_parser("run")
    _add_config_argument(web_run)
    web_run.add_argument("--db")
    web_run.add_argument("--host", default="127.0.0.1")
    web_run.add_argument("--port", type=int, default=8765)
    web_run.add_argument("--operator", default="local-operator")
    web_run.add_argument("--writable", action="store_true")
    web_run.add_argument("--allow-remote", action="store_true")
    web_run.add_argument("--json", action="store_true")

    openclaw_parser = subparsers.add_parser(
        "openclaw",
        help="Manage the optional OpenClaw integration.",
    )
    openclaw_subparsers = openclaw_parser.add_subparsers(
        dest="openclaw_command",
        required=True,
    )
    openclaw_install = openclaw_subparsers.add_parser("install")
    openclaw_install.add_argument(
        "--plugin-dir",
        default=str(openclaw.DEFAULT_PLUGIN_DIR),
    )
    openclaw_install.add_argument("--dry-run", action="store_true")
    openclaw_install.add_argument("--force", action="store_true")
    openclaw_install.add_argument("--skip-restart", action="store_true")
    _add_output_argument(openclaw_install)
    for name in ["uninstall", "status"]:
        command_parser = openclaw_subparsers.add_parser(name)
        command_parser.add_argument("--dry-run", action="store_true")
        _add_output_argument(command_parser)

    return parser


def _validated_config(args):
    result = validate_config.validate_config(
        args.config,
        skip_external=getattr(args, "skip_external", False),
    )
    if not result["ok"]:
        raise ValueError(result["safe_error"])
    return result


def _run_once_argv(args):
    argv = [
        "--config",
        str(args.config),
        "--workflow",
        str(resource("workflow.yml")),
    ]
    if args.dry_run:
        argv.append("--dry-run")
    if args.skip_external:
        argv.append("--skip-external")
    if args.skip_publish:
        argv.append("--skip-publish")
    return argv


def _daemon_argv(args):
    argv = [
        "--config",
        str(args.config),
        "--workflow",
        str(resource("workflow.yml")),
    ]
    if args.dry_run:
        argv.append("--dry-run")
    if args.once:
        argv.append("--once")
    return argv


def _status_argv(args, suffix):
    validated = _validated_config(args)
    return ["--db", validated["db_path"], *suffix]


def _init_values(args):
    fields = {
        "repo": args.repo,
        "repo_path": args.repo_path,
        "worker": args.worker,
        "github_account": args.github_account,
        "trusted_actor": args.trusted_actor,
    }
    if args.non_interactive:
        return {key: value for key, value in fields.items() if value}
    prompts = {
        "repo": "GitHub repository (owner/name): ",
        "repo_path": "Local repository checkout: ",
        "worker": "Worker command: ",
        "github_account": "GitHub bot account: ",
        "trusted_actor": "Trusted GitHub actor: ",
    }
    return {
        key: value or input(prompts[key]).strip()
        for key, value in fields.items()
    }


def _emit_and_code(result, output):
    emit_result(result, output=output)
    return exit_codes.SUCCESS if result.get("ok") else exit_codes.INVALID_INPUT


def _run_read_only_command(command, argv, output):
    captured = io.StringIO()
    with redirect_stdout(captured):
        code = command(argv)
    text = captured.getvalue().strip()
    try:
        result = json.loads(text)
    except ValueError:
        result = {
            "ok": False,
            "status": "failed",
            "safe_error": "read-only command returned invalid output",
        }
        code = exit_codes.STATE_FAILURE
    emit_result(result, output=output)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_config(
                args.config,
                _init_values(args),
                non_interactive=args.non_interactive,
                force=args.force,
            )
            return _emit_and_code(result, args.output)
        if args.command == "doctor":
            return _emit_and_code(
                doctor(args.config, skip_external=args.skip_external),
                args.output,
            )
        if (
            args.command == "migrate"
            and args.migrate_command == "dd-github-agent"
        ):
            return _emit_and_code(
                migrate.migrate_legacy(
                    args.source,
                    args.target,
                    dry_run=args.dry_run,
                ),
                args.output,
            )
        if args.command == "service":
            platform_name = (
                "darwin"
                if sys.platform == "darwin"
                else ("linux" if sys.platform.startswith("linux") else sys.platform)
            )
            if args.service_command == "install":
                executable = Path(
                    shutil.which("robert") or sys.argv[0]
                ).resolve()
                result = service.install_service(
                    platform_name,
                    executable,
                    args.config,
                    dry_run=args.dry_run,
                )
            elif args.service_command == "uninstall":
                result = service.uninstall_service(
                    platform_name,
                    dry_run=args.dry_run,
                )
            else:
                result = service.control_service(
                    platform_name,
                    args.service_command,
                    dry_run=args.dry_run,
                )
            return _emit_and_code(result, args.output)
        if (
            args.command == "diagnostics"
            and args.diagnostics_command == "export"
        ):
            result = diagnostics.export_diagnostics(
                args.config,
                args.output_path,
            )
            emit_result(result, output="text")
            return (
                exit_codes.SUCCESS
                if result.get("ok")
                else exit_codes.STATE_FAILURE
            )
        if args.command == "web" and args.web_command == "run":
            web_argv = [
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--operator",
                args.operator,
            ]
            if args.db:
                web_argv.extend(["--db", args.db])
            else:
                web_argv.extend(["--config", args.config])
            if args.writable:
                web_argv.append("--writable")
            if args.allow_remote:
                web_argv.append("--allow-remote")
            if args.json:
                web_argv.append("--json")
            return web.main(web_argv)
        if args.command == "openclaw":
            if args.openclaw_command == "install":
                preflight = openclaw.preflight_openclaw(
                    dry_run=args.dry_run,
                )
                if not preflight["ok"]:
                    return _emit_and_code(preflight, args.output)
                written = openclaw.write_plugin(
                    args.plugin_dir,
                    force=args.force,
                )
                if not written["ok"]:
                    return _emit_and_code(written, args.output)
                installed = openclaw.install_plugin(
                    args.plugin_dir,
                    dry_run=args.dry_run,
                )
                steps = [preflight, written, installed]
                if installed.get("ok") and not args.skip_restart:
                    steps.append(
                        openclaw.restart_gateway(
                            dry_run=args.dry_run,
                        )
                    )
                if installed.get("ok"):
                    steps.append(
                        openclaw.verify_gateway_commands(
                            dry_run=args.dry_run,
                        )
                    )
                ok = all(step.get("ok") for step in steps)
                failed_steps = [
                    step
                    for step in steps
                    if not step.get("ok")
                ]
                result = {
                    "ok": ok,
                    "status": (
                        "planned"
                        if args.dry_run
                        else ("installed" if ok else "failed")
                    ),
                    "plugin_dir": str(args.plugin_dir),
                    "steps": steps,
                }
                if failed_steps:
                    result["safe_error"] = failed_steps[0].get(
                        "safe_error",
                        "Robert OpenClaw install failed.",
                    )
            elif args.openclaw_command == "uninstall":
                result = openclaw.uninstall_plugin(
                    dry_run=args.dry_run,
                )
            else:
                result = openclaw.plugin_status(
                    dry_run=args.dry_run,
                )
            return _emit_and_code(result, args.output)
        if args.command == "config":
            if args.config_command == "path":
                return _emit_and_code(
                    {
                        "ok": True,
                        "status": "ready",
                        "config_path": str(args.config),
                    },
                    args.output,
                )
            validated = _validated_config(args)
            return _emit_and_code(validated, args.output)
        if args.command == "run" and args.run_command == "once":
            return run_once.main(_run_once_argv(args))
        if args.command == "daemon" and args.daemon_command == "run":
            return daemon.main(_daemon_argv(args))
        if args.command == "status":
            return _run_read_only_command(
                status.main,
                _status_argv(args, ["status"]),
                args.output,
            )
        if args.command == "task" and args.task_command == "show":
            return _run_read_only_command(
                status.main,
                _status_argv(args, ["task", args.task_id]),
                args.output,
            )
        if args.command == "run" and args.run_command == "show":
            return _run_read_only_command(
                status.main,
                _status_argv(args, ["run", args.run_id]),
                args.output,
            )
        if args.command == "artifact" and args.artifact_command == "show":
            return _run_read_only_command(
                status.main,
                _status_argv(
                    args,
                    ["artifact", args.task_id, args.artifact_type],
                ),
                args.output,
            )
        parser.error(f"command is not wired yet: {args.command}")
    except (OSError, TypeError, ValueError) as exc:
        output = getattr(args, "output", "text")
        emit_result(
            {
                "ok": False,
                "status": "failed",
                "safe_error": str(exc),
            },
            output=output,
        )
        return exit_codes.INVALID_INPUT
    return exit_codes.INVALID_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
