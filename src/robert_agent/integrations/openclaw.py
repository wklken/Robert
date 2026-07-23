import json
from pathlib import Path
import shutil
import subprocess

from robert_agent.paths import default_data_dir


PLUGIN_ID = "robert-openclaw"
REQUIRED_COMMANDS = [
    "robert-status",
    "robert-task",
    "robert-run",
    "robert-artifact",
]
DEFAULT_PLUGIN_DIR = (
    default_data_dir()
    / "openclaw-plugin"
    / PLUGIN_ID
)


def _run(command, timeout=180):
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _combined_output(completed):
    return "\n".join(
        part
        for part in [
            completed.stdout.strip(),
            completed.stderr.strip(),
        ]
        if part
    )


def preflight_openclaw(dry_run=False):
    command = ["openclaw", "--version"]
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "command": command,
        }
    try:
        completed = _run(command, timeout=30)
    except FileNotFoundError:
        return {
            "ok": False,
            "status": "missing",
            "command": command,
            "safe_error": (
                "OpenClaw CLI is not available on PATH. "
                "Install or repair OpenClaw before running "
                "robert openclaw install."
            ),
        }
    return {
        "ok": completed.returncode == 0,
        "status": "ready" if completed.returncode == 0 else "failed",
        "command": command,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }


def _plugin_source():
    return '''import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

async function runRobert(args) {
  try {
    const { stdout } = await execFileAsync(
      "robert",
      args,
      {
        timeout: 30000,
        maxBuffer: 1024 * 1024,
      },
    );
    const parsed = JSON.parse(stdout);
    if (parsed?.ok === true) {
      return { text: JSON.stringify(parsed, null, 2) };
    }
    return {
      text: String(parsed?.safe_error ?? "Robert command failed."),
      isError: true,
    };
  } catch (error) {
    return {
      text: `Robert command failed: ${error?.message ?? String(error)}`,
      isError: true,
    };
  }
}

function splitArgs(args) {
  return String(args ?? "").trim().split(/\\s+/).filter(Boolean);
}

export default definePluginEntry({
  id: "robert-openclaw",
  name: "Robert OpenClaw Commands",
  description: "Read-only Robert status and artifact commands.",
  register(api) {
    api.registerCommand({
      name: "robert-status",
      description: "Show Robert status.",
      acceptsArgs: false,
      requireAuth: true,
      handler: async () => runRobert(["status", "--output", "json"]),
    });
    api.registerCommand({
      name: "robert-task",
      description: "Show Robert task details.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        const [taskId] = splitArgs(ctx.args);
        if (!taskId) {
          return { text: "Usage: /robert-task <task-id>", isError: true };
        }
        return runRobert(["task", "show", taskId, "--output", "json"]);
      },
    });
    api.registerCommand({
      name: "robert-run",
      description: "Show Robert run details.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        const [runId] = splitArgs(ctx.args);
        if (!runId) {
          return { text: "Usage: /robert-run <run-id>", isError: true };
        }
        return runRobert(["run", "show", runId, "--output", "json"]);
      },
    });
    api.registerCommand({
      name: "robert-artifact",
      description: "Show one registered Robert artifact.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        const [taskId, artifactType] = splitArgs(ctx.args);
        if (!taskId || !artifactType) {
          return {
            text: "Usage: /robert-artifact <task-id> <artifact-type>",
            isError: true,
          };
        }
        return runRobert([
          "artifact",
          "show",
          taskId,
          artifactType,
          "--output",
          "json",
        ]);
      },
    });
  },
});
'''


def write_plugin(plugin_dir, force=False):
    path = Path(plugin_dir).expanduser()
    if path.exists():
        if not force:
            return {
                "ok": False,
                "status": "exists",
                "plugin_dir": str(path),
                "safe_error": (
                    f"plugin directory already exists: {path}. "
                    "Re-run with --force to rewrite it, or use "
                    "robert openclaw status to inspect the installed plugin."
                ),
            }
        shutil.rmtree(path)
    path.mkdir(parents=True)
    package = {
        "name": "openclaw-robert-commands-local",
        "version": "0.1.0",
        "type": "module",
        "private": True,
        "openclaw": {"extensions": ["./index.js"]},
    }
    manifest = {
        "id": PLUGIN_ID,
        "name": "Robert OpenClaw Commands",
        "description": "Read-only Robert status and artifact commands.",
        "activation": {"onStartup": True},
        "configSchema": {
            "type": "object",
            "additionalProperties": False,
        },
    }
    files = {
        "package.json": json.dumps(
            package,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        "openclaw.plugin.json": json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        "index.js": _plugin_source(),
    }
    for name, content in files.items():
        (path / name).write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "status": "written",
        "plugin_dir": str(path),
        "files": sorted(files),
    }


def install_plugin(plugin_dir, dry_run=False):
    command = ["openclaw", "plugins", "install", str(plugin_dir)]
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "command": command,
        }
    completed = _run(command)
    return {
        "ok": completed.returncode == 0,
        "status": (
            "installed"
            if completed.returncode == 0
            else "failed"
        ),
        "command": command,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }


def restart_gateway(dry_run=False):
    command = ["openclaw", "gateway", "restart"]
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "command": command,
        }
    completed = _run(command, timeout=60)
    return {
        "ok": completed.returncode == 0,
        "status": (
            "restarted"
            if completed.returncode == 0
            else "failed"
        ),
        "command": command,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }


def verify_gateway_commands(dry_run=False):
    command = [
        "openclaw",
        "gateway",
        "call",
        "commands.list",
        "--json",
        "--timeout",
        "10000",
    ]
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "command": command,
            "required_commands": REQUIRED_COMMANDS,
        }
    completed = _run(command, timeout=30)
    if completed.returncode != 0:
        detail = _combined_output(completed)
        return {
            "ok": False,
            "status": "verify_failed",
            "command": command,
            "stdout": completed.stdout.strip(),
            "safe_error": (
                "Robert plugin was installed, but the live OpenClaw "
                "gateway command list could not be verified. Restart "
                "the OpenClaw gateway and run robert openclaw status."
                + (f" OpenClaw output: {detail}" if detail else "")
            ),
        }
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return {
            "ok": False,
            "status": "invalid_commands_json",
            "command": command,
            "stdout": completed.stdout.strip(),
            "safe_error": (
                "OpenClaw gateway returned invalid command-list JSON. "
                "Restart the OpenClaw gateway and try again."
            ),
        }
    command_names = {
        str(item.get("name"))
        for item in payload.get("commands", [])
        if isinstance(item, dict)
    }
    missing = [
        required
        for required in REQUIRED_COMMANDS
        if required not in command_names
    ]
    if missing:
        return {
            "ok": False,
            "status": "missing_commands",
            "command": command,
            "missing_commands": missing,
            "safe_error": (
                "Robert plugin is installed, but the live OpenClaw "
                "gateway command list is missing: "
                f"{', '.join(missing)}. Restart the OpenClaw gateway "
                "and run /commands again."
            ),
        }
    return {
        "ok": True,
        "status": "verified",
        "command": command,
        "commands": REQUIRED_COMMANDS,
    }


def uninstall_plugin(dry_run=False):
    command = [
        "openclaw",
        "plugins",
        "uninstall",
        PLUGIN_ID,
        "--force",
    ]
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "command": command,
        }
    completed = _run(command)
    return {
        "ok": completed.returncode == 0,
        "status": (
            "uninstalled"
            if completed.returncode == 0
            else "failed"
        ),
        "command": command,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }


def plugin_status(dry_run=False):
    command = [
        "openclaw",
        "plugins",
        "inspect",
        PLUGIN_ID,
        "--runtime",
        "--json",
    ]
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "command": command,
        }
    completed = _run(command)
    return {
        "ok": completed.returncode == 0,
        "status": (
            "ready"
            if completed.returncode == 0
            else "failed"
        ),
        "command": command,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }
