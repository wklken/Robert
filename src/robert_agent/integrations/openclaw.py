import json
import os
import tempfile
from pathlib import Path
import subprocess

from robert_agent.paths import default_data_dir


PLUGIN_ID = "robert-openclaw"
REQUIRED_COMMANDS = [
    "robert-status",
    "robert-task",
    "robert-run",
    "robert-artifact",
    "robert-inbox",
    "robert-respond",
    "robert-messaging-context",
]
DEFAULT_PLUGIN_DIR = default_data_dir() / "openclaw-plugin" / PLUGIN_ID


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
    return """import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

async function runRobert(args, input, rawResult = false) {
  try {
    const serialized = input === undefined ? "" : JSON.stringify(input);
    if (Buffer.byteLength(serialized, "utf8") > 65536) {
      if (rawResult) return { ok: false, safe_error: "Message exceeds the input limit." };
      return { text: "Message exceeds the input limit.", isError: true };
    }
    const pending = execFileAsync(
      "robert",
      args,
      {
        timeout: 30000,
        maxBuffer: 1024 * 1024,
      },
    );
    let inputError;
    pending.child.stdin.on("error", (error) => { inputError = error; });
    pending.child.stdin.end(serialized);
    const { stdout } = await pending;
    if (inputError) return { text: "Robert could not receive the request.", isError: true };
    const parsed = JSON.parse(stdout);
    if (rawResult) return parsed;
    if (parsed?.ok === true) {
      return { text: JSON.stringify(parsed, null, 2) };
    }
    return {
      text: String(parsed?.safe_error ?? "Robert command failed."),
      isError: true,
    };
  } catch (error) {
    try {
      const parsed = JSON.parse(error.stdout);
      if (rawResult && typeof parsed?.safe_error === "string") return parsed;
      if (typeof parsed?.safe_error === "string") return { text: parsed.safe_error, isError: true };
    } catch {}
    if (rawResult) return { ok: false, safe_error: "Robert command failed; inspect local diagnostics." };
    return {
      text: "Robert command failed; inspect local diagnostics.",
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
  description: "Robert queries and opt-in authenticated operator messaging.",
  register(api) {
    function identity(ctx) {
      return {
        protocol: "robert.message.v1", provider: "openclaw",
        channel: ctx.channelId ?? ctx.channel,
        account: ctx.accountId ?? "", conversation: ctx.from ?? "",
        thread: String(ctx.messageThreadId ?? ""), sender_id: ctx.senderId ?? "",
      };
    }
    function runConfigured(args, input, rawResult = false) {
      if (api.pluginConfig?.robertConfig) args.push("--config", api.pluginConfig.robertConfig);
      return runRobert(args, input, rawResult);
    }
    if (api.pluginConfig?.contextualReplies === true) {
      api.on("reply_dispatch", async (event, host) => {
        const ctx = event.ctx;
        if (event.isTailDispatch || !ctx || ctx.CommandAuthorized !== true) return;
        const body = String(ctx.RawBody ?? ctx.CommandBody ?? "").trim();
        if (!body || body.startsWith("/") || event.sendPolicy === "deny"
            || event.suppressUserDelivery || ctx.MediaPaths?.length || ctx.MediaPath) return;
        const channel = ctx.Surface ?? ctx.Provider;
        const identity = {
          protocol: "robert.message.v1", provider: "openclaw", channel,
          account: ctx.AccountId ?? "", conversation: ctx.From ?? "",
          thread: String(ctx.MessageThreadId ?? ""), sender_id: ctx.SenderId ?? "",
        };
        const result = await runConfigured(["messages", "reply", "--output", "json"], {
          ...identity, message_id: ctx.MessageSidFull ?? ctx.MessageSid ?? "", body,
          quoted: Boolean(ctx.ReplyToIsQuote || ctx.ReplyToIdFull || ctx.ReplyToId || ctx.ReplyToBody),
          quote_message_id: ctx.ReplyToIdFull ?? ctx.ReplyToId ?? "",
          quote_body: ctx.ReplyToBody ?? "",
        }, true);
        if (result.status === "ignored") return;
        const text = result.ok && result.status === "accepted"
          ? `Robert 已收到答复。\\n关联消息: ${result.notification_id}\\n任务: ${result.task_id ?? "查看 /robert-inbox"}`
          : result.ok && result.status === "no_question"
            ? "Robert 当前没有已送达的待回复问题，请使用 /robert-inbox 查看。"
            : `Robert 未提交答复: ${result.safe_error ?? "请查看本地诊断。"}`;
        const queuedFinal = host.dispatcher.sendFinalReply({ text });
        host.recordProcessed("completed", { reason: "robert_contextual_reply" });
        host.markIdle("message_completed");
        return { handled: true, queuedFinal, counts: host.dispatcher.getQueuedCounts() };
      });
    }
    api.registerCommand({
      name: "robert-messaging-context",
      description: "Show your messaging identity for Robert configuration.",
      acceptsArgs: false, requireAuth: true,
      handler: async (ctx) => ctx.isAuthorizedSender
        ? { text: JSON.stringify(identity(ctx), null, 2) }
        : { text: "Unauthorized sender.", isError: true },
    });
    api.registerCommand({
      name: "robert-inbox",
      description: "Show pending messages or draft and submit an Issue request.",
      acceptsArgs: true, requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.isAuthorizedSender) return { text: "Unauthorized sender.", isError: true };
        const args = String(ctx.args ?? "").trim();
        if (!args) return runConfigured(["messages", "inbox", "--output", "json"], identity(ctx));
        const draft = args.match(/^new\\s+(\\S+)\\s+([\\s\\S]+)$/);
        const command = args.match(/^(submit|status|retry)\\s+(dispatch-[a-f0-9]{32})$/);
        if (draft) return runConfigured(["messages", "dispatch", "--output", "json"],
          { ...identity(ctx), action: "new", repo: draft[1], body: draft[2] });
        if (command) return runConfigured(["messages", "dispatch", "--output", "json"],
          { ...identity(ctx), action: command[1], request_id: command[2] });
        return { text: "Usage: /robert-inbox new <owner/repo> <request>, or /robert-inbox <submit|status|retry> <request-id>", isError: true };
      },
    });
    api.registerCommand({
      name: "robert-respond",
      description: "Respond to a specific Robert question or notification.",
      acceptsArgs: true, requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.isAuthorizedSender) return { text: "Unauthorized sender.", isError: true };
        const match = String(ctx.args ?? "").trim().match(/^(\\S+)\\s+(\\d+)\\s+(reply|approve|retry|retry_publish|cancel|ack)(?:\\s+([\\s\\S]*))?$/);
        if (!match) return { text: "Usage: /robert-respond <message-id> <version> <action> [answer]", isError: true };
        const [, notification_id, rawVersion, action, body = ""] = match;
        const version = Number(rawVersion);
        if (!Number.isSafeInteger(version)) return { text: "Invalid version.", isError: true };
        return runConfigured(["messages", "respond", "--output", "json"], { ...identity(ctx), notification_id, version, action, body });
      },
    });
    api.registerCommand({
      name: "robert-status",
      description: "Show Robert status.",
      acceptsArgs: false,
      requireAuth: true,
      handler: async (ctx) => ctx.isAuthorizedSender
        ? runConfigured(["status", "--output", "json"])
        : { text: "Unauthorized sender.", isError: true },
    });
    api.registerCommand({
      name: "robert-task",
      description: "Show Robert task details.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.isAuthorizedSender) return { text: "Unauthorized sender.", isError: true };
        const [taskId] = splitArgs(ctx.args);
        if (!taskId) {
          return { text: "Usage: /robert-task <task-id>", isError: true };
        }
        return runConfigured(["task", "show", taskId, "--output", "json"]);
      },
    });
    api.registerCommand({
      name: "robert-run",
      description: "Show Robert run details.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.isAuthorizedSender) return { text: "Unauthorized sender.", isError: true };
        const [runId] = splitArgs(ctx.args);
        if (!runId) {
          return { text: "Usage: /robert-run <run-id>", isError: true };
        }
        return runConfigured(["run", "show", runId, "--output", "json"]);
      },
    });
    api.registerCommand({
      name: "robert-artifact",
      description: "Show one registered Robert artifact.",
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        if (!ctx.isAuthorizedSender) return { text: "Unauthorized sender.", isError: true };
        const [taskId, artifactType] = splitArgs(ctx.args);
        if (!taskId || !artifactType) {
          return {
            text: "Usage: /robert-artifact <task-id> <artifact-type>",
            isError: true,
          };
        }
        return runConfigured([
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
"""


def write_plugin(plugin_dir, force=False, dry_run=False):
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
    if path.is_symlink() or path.resolve() in {Path.home().resolve(), Path(path.anchor or "/")}:
        return {"ok": False, "status": "unsafe_target", "safe_error": "Plugin target must be a dedicated directory."}
    if path.exists():
        try:
            owned = (
                all(
                    (path / name).is_file() and not (path / name).is_symlink()
                    for name in ("package.json", "openclaw.plugin.json", "index.js")
                )
                and json.loads((path / "openclaw.plugin.json").read_text())["id"] == PLUGIN_ID
                and json.loads((path / "package.json").read_text())["name"] == "openclaw-robert-commands-local"
            )
        except (OSError, ValueError, KeyError, TypeError):
            owned = False
        if not owned:
            return {
                "ok": False,
                "status": "unsafe_target",
                "safe_error": "--force only updates an existing Robert plugin.",
            }
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
        "description": "Robert queries and opt-in authenticated operator messaging.",
        "activation": {"onStartup": True},
        "configSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "robertConfig": {"type": "string", "minLength": 1},
                "contextualReplies": {"type": "boolean", "default": False},
            },
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
    if dry_run:
        return {
            "ok": True,
            "status": "planned",
            "plugin_dir": str(path),
            "files": sorted(files),
        }
    path.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        # Replace the directory entry; never overwrite an existing inode that
        # could also be linked from an unrelated path.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path, delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
            os.replace(temporary, path / name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
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
        "status": ("installed" if completed.returncode == 0 else "failed"),
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
        "status": ("restarted" if completed.returncode == 0 else "failed"),
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
                "OpenClaw gateway returned invalid command-list JSON. Restart the OpenClaw gateway and try again."
            ),
        }
    command_names = {str(item.get("name")) for item in payload.get("commands", []) if isinstance(item, dict)}
    missing = [required for required in REQUIRED_COMMANDS if required not in command_names]
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
        "status": ("uninstalled" if completed.returncode == 0 else "failed"),
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
        "status": ("ready" if completed.returncode == 0 else "failed"),
        "command": command,
        "stdout": completed.stdout.strip(),
        "safe_error": completed.stderr.strip(),
    }
