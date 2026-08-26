"""``vaspilot agent ...`` — provider management and model-driven sessions."""

from __future__ import annotations

import argparse
import json
import sys

from ..core.config import ProviderEntry
from ..core.errors import ValidationError
from ..core.jsonio import emit
from ..providers import build_provider, default_provider, provider_by_id
from ..providers.base import ANALYSIS_ONLY, FULL

PROBE_CACHE_KEY = "provider_probes"
PROBE_FRESH_SECONDS = 24 * 3600


def register(sub) -> None:
    parser = sub.add_parser("agent", help="model providers and agent sessions")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, **kw):
        child = commands.add_parser(name, **kw)
        child.set_defaults(handler=handler)
        return child

    provider = commands.add_parser("provider", help="provider registry")
    provider_cmds = provider.add_subparsers(dest="provider_command",
                                            required=True)

    provider_cmds.add_parser("list").set_defaults(handler=cmd_provider_list)

    p = provider_cmds.add_parser("add")
    p.set_defaults(handler=cmd_provider_add)
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--protocol", required=True,
                   choices=["openai-chat-compatible", "openai-responses",
                            "codex-sdk"])
    p.add_argument("--base-url", dest="base_url", default="")
    p.add_argument("--model", default="")
    p.add_argument("--api-key-env", dest="api_key_env", default="",
                   help="environment variable holding the API key; the key "
                        "itself is never stored")

    p = provider_cmds.add_parser("remove")
    p.set_defaults(handler=cmd_provider_remove)
    p.add_argument("provider_id")

    p = provider_cmds.add_parser("probe")
    p.set_defaults(handler=cmd_provider_probe)
    p.add_argument("provider_id")
    p.add_argument("--offline", action="store_true",
                   help="locate backends without making model calls "
                        "(codex-sdk only); never certifies capabilities")

    p = provider_cmds.add_parser("set-default")
    p.set_defaults(handler=cmd_provider_set_default)
    p.add_argument("provider_id")

    p = command("chat", cmd_chat)
    p.add_argument("--provider", default=None)
    p.add_argument("--message", dest="message", default=None,
                   help="one-shot message; omit for interactive REPL")
    p.add_argument("--project-root", dest="project_root", default=None,
                   help="bind a local project root for transfer tools")

    p = command("run", cmd_run)
    p.add_argument("--provider", default=None)
    p.add_argument("--goal", required=True)
    p.add_argument("--project-root", dest="project_root", default=None)
    p.add_argument("--max-turns", dest="max_turns", type=int, default=12)


# ------------------------------------------------------------------ providers
def cmd_provider_list(app, args):
    providers = [p.to_dict() for p in app.config.load_providers()]
    cached = app.config.load_settings().get(PROBE_CACHE_KEY) or {}
    default = app.config.default_provider()
    for entry in providers:
        probe = cached.get(entry["id"])
        entry["is_default"] = entry["id"] == default
        entry["mode"] = (probe or {}).get("mode") if isinstance(probe, dict) else None
        entry["probed_at"] = (probe or {}).get("checked_at") if isinstance(probe, dict) else None
    return {"providers": providers, "default": default}


def cmd_provider_add(app, args):
    if args.protocol != "codex-sdk" and not args.base_url:
        raise ValidationError("--base-url is required for HTTP protocols")
    if not args.model:
        raise ValidationError("--model is required")
    entry = ProviderEntry(
        id=args.id, name=args.name, protocol=args.protocol,
        base_url=args.base_url, model=args.model,
        api_key_env=args.api_key_env)
    app.config.add_provider(entry)
    if not app.config.default_provider():
        app.config.set_default_provider(entry.id)
    return {"added": entry.to_dict(),
            "note": "API keys are read from the environment variable only"}


def cmd_provider_remove(app, args):
    app.config.remove_provider(args.provider_id)
    return {"removed": args.provider_id}


def cmd_provider_probe(app, args):
    _, provider = provider_by_id(app.config, args.provider_id)
    offline = bool(args.offline) and provider.protocol == "codex-sdk"
    try:
        report = provider.probe(offline=offline) \
            if provider.protocol == "codex-sdk" else provider.probe()
    except Exception as exc:
        return {"ok": False, "error": {"code": "provider_error",
                                       "message": str(exc)[:300]},
                "provider": args.provider_id}
    cache = app.config.load_settings().get(PROBE_CACHE_KEY) or {}
    cache[args.provider_id] = report.to_dict()
    app.config.update_settings(**{PROBE_CACHE_KEY: cache})
    payload = report.to_dict()
    if payload["mode"] == ANALYSIS_ONLY:
        payload["note"] = ("analysis_only: this provider may not call write "
                           "or scheduler tools until a full probe passes")
    return payload


def cmd_provider_set_default(app, args):
    app.config.set_default_provider(args.provider_id)
    return {"default": args.provider_id}


# ------------------------------------------------------------------ sessions
def _resolve_mode(app, provider_id: str | None):
    entry, provider = (provider_by_id(app.config, provider_id)
                       if provider_id else default_provider(app.config))
    cached = app.config.load_settings().get(PROBE_CACHE_KEY) or {}
    probe = cached.get(entry.id)
    if not isinstance(probe, dict) or not probe.get("checked_at"):
        # probe lazily on first use; degraded providers still work read-only
        report = provider.probe()
        cache = app.config.load_settings().get(PROBE_CACHE_KEY) or {}
        cache[entry.id] = report.to_dict()
        app.config.update_settings(**{PROBE_CACHE_KEY: cache})
        mode = report.mode
    else:
        mode = str(probe.get("mode", ANALYSIS_ONLY))
    return entry, provider, mode


def cmd_chat(app, args):
    from ..agents.runtime import AgentRuntime
    entry, provider, mode = _resolve_mode(app, args.provider)
    runtime = AgentRuntime(provider=provider,
                           registry=app.registry(project_root=args.project_root),
                           mode=mode,
                           audit=app.audit,
                           stream_cb=lambda fragment: print(fragment, end="",
                                                            flush=True))
    if args.message:
        result = runtime.chat(args.message)
        print()
        return result
    # interactive REPL, one JSON summary per turn
    history = []
    print(f"vaspilot agent chat — provider {entry.id} ({mode}); "
          "Ctrl-D/Ctrl-C to exit", file=sys.stderr)
    while True:
        try:
            line = input("you> ")
        except EOFError:
            break
        if not line.strip():
            continue
        if line.strip().lower() in {"exit", "quit"}:
            break
        result = runtime.chat(line, history)
        history.append({"role": "user", "content": line})
        history.append({"role": "assistant",
                        "content": result.get("answer", "")})
        print(f"\nagent> {result.get('answer', '')}\n")
    return {"ok": True, "provider": entry.id, "mode": mode,
            "turns": len(history) // 2}


def cmd_run(app, args):
    from ..agents.runtime import AgentRuntime
    entry, provider, mode = _resolve_mode(app, args.provider)
    runtime = AgentRuntime(provider=provider,
                           registry=app.registry(project_root=args.project_root),
                           mode=mode, audit=app.audit, max_turns=args.max_turns,
                           stream_cb=lambda fragment: print(fragment, end="",
                                                            flush=True))
    result = runtime.run(args.goal)
    print()
    result["provider"] = entry.id
    return result
