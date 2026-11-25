"""siwah command line interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from ..adapt.promoter import HarnessRepository, PromotionError
from ..adapt.state import load_state, set_paused
from ..adapt.state import state_path as evolution_state_path
from ..auth import AuthError, AuthStore
from ..config import (
    EFFORT_VALUES,
    Config,
    ConfigError,
    ModelRef,
    add_default_providers,
    load_config,
    update_budget_config,
)
from ..core.storage import Store
from ..core.types import Message, ModelInfo, ProviderEvent
from ..harness.bootstrap import ensure_harness
from ..harness.memory import read_memory
from ..harness.skills import list_skills, read_skill
from ..kernel import repo_root
from ..kernel.audit import audit_event, read_events
from ..kernel.budget import BudgetGovernor
from ..kernel.manifest import manifest_path, verify_manifest, write_manifest
from ..kernel.snapshot import rollback_to_tag
from ..kernel.supervisor import read_runtime, runtime_endpoint, runtime_healthy
from ..maintenance import inspect_home, prune_orphans, rotate_daemon_log
from ..mcp.client import (
    McpError,
    McpManager,
    McpToolInfo,
    load_mcp_config,
    mcp_config_path,
    normalize_server,
    save_mcp_config,
    split_command,
)
from ..mcp.oauth import (
    McpAuthRequired,
    McpOAuthError,
    OAuthClient,
    delete_state,
    oauth_state_path,
    oauth_status,
)
from ..paths import (
    auth_path,
    config_path,
    ensure_home,
    harness_dir,
    runtime_path,
    siwah_home,
    state_path,
    write_private,
)
from ..plugins import (
    PluginError,
    install_plugin,
    list_plugins,
    plugin_mcp_servers,
    remove_plugin,
    set_enabled,
)
from ..providers.base import ProviderError
from ..providers.chatgpt_auth import ChatGPTAuth, ChatGPTSignInPending
from ..providers.openai_responses import supports_chatgpt_login
from ..providers.openrouter_auth import OpenRouterLogin, supports_browser_login
from ..providers.profiles import (
    ModelProfile,
    load_profile,
    merge_privacy,
    probe_model,
    save_profile,
    set_privacy,
)
from ..providers.registry import ProviderRegistry, RoleRouter
from ..update import apply_update, check_for_update
from ..version import __version__
from .plain_ui import JsonRenderer, PlainRenderer

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(AttributeError, ValueError):
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

app = typer.Typer(
    name="siwah",
    help="Self-improving coding agent harness.",
    no_args_is_help=False,
    add_completion=False,
)
auth_app = typer.Typer(help="Manage provider credentials.", no_args_is_help=True)
models_app = typer.Typer(help="List and probe models.", no_args_is_help=True)
skills_app = typer.Typer(help="List and view harness skills.", no_args_is_help=True)
memory_app = typer.Typer(help="Inspect layered memory.", no_args_is_help=True)
kernel_app = typer.Typer(help="Inspect and verify the protected kernel.", no_args_is_help=True)
mcp_app = typer.Typer(help="Manage MCP servers and tools.", no_args_is_help=True)
plugin_app = typer.Typer(help="Manage declarative plugin bundles.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(models_app, name="models")
app.add_typer(skills_app, name="skills")
app.add_typer(memory_app, name="memory")
app.add_typer(kernel_app, name="kernel")
app.add_typer(mcp_app, name="mcp")
app.add_typer(plugin_app, name="plugin")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"siwah {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Print version."
    ),
    ui: str = typer.Option("tui", "--ui", help="tui or plain."),
    allow_all: bool = typer.Option(
        False,
        "--allow-all",
        "--bypass",
        help="Start the TUI with auto-approved tools (same as /permissions yolo).",
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        if ui == "plain":
            typer.echo('Plain mode: use `siwah run "<prompt>" --ui plain`.')
            return
        if ui != "tui":
            typer.echo("--ui must be tui or plain", err=True)
            raise typer.Exit(2)
        launch_tui(allow_all=allow_all)


@app.command()
def run(
    prompt: str = typer.Argument(..., help="Task for the agent."),
    json_output: bool = typer.Option(False, "--json", help="Emit NDJSON events."),
    model: str | None = typer.Option(None, "--model", help="provider/model-id override."),
    permission: str | None = typer.Option(None, "--permission", help="ask | auto-edit | yolo"),
    auto: bool = typer.Option(
        False,
        "--auto",
        "--bypass",
        help="Auto-approve tool permissions (same as --permission yolo).",
    ),
    effort: str | None = typer.Option(
        None,
        "--effort",
        help="Reasoning effort: minimal | low | medium | high | xhigh | max.",
    ),
    cwd: Path | None = typer.Option(None, "--cwd", help="Workspace directory."),
    max_steps: int | None = typer.Option(
        None, "--max-steps", help="Hard model-step limit; 0 disables the cap."
    ),
    ultra: int | None = typer.Option(
        None, "--ultra", min=2, max=5, help="Run N best-of-N attempts in git worktrees."
    ),
    show_reasoning: bool = typer.Option(
        False, "--show-reasoning", help="Print reasoning deltas in plain mode."
    ),
    ui: str = typer.Option("plain", "--ui", help="plain or json."),
) -> None:
    """Run one task non-interactively."""
    from ..adapt.best_of_n import BestOfN, BestOfNResult
    from ..core.loop import AgentLoop, RunResult
    from ..tools.base import ToolRegistry
    from ..tools.runtime import default_tools

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if ui not in {"plain", "json"}:
        typer.echo("--ui must be plain or json", err=True)
        raise typer.Exit(2)
    if ui == "json":
        json_output = True
    if permission:
        if permission not in {"ask", "auto-edit", "yolo"}:
            typer.echo("--permission must be ask, auto-edit, or yolo", err=True)
            raise typer.Exit(2)
        config = config.model_copy(
            update={"permissions": config.permissions.model_copy(update={"mode": permission})}
        )
    if auto:
        config = config.model_copy(
            update={"permissions": config.permissions.model_copy(update={"mode": "yolo"})}
        )
    if effort is not None:
        normalized = effort.strip().lower()
        if normalized not in EFFORT_VALUES:
            typer.echo(f"--effort must be {', '.join(EFFORT_VALUES)}", err=True)
            raise typer.Exit(2)
        config = config.model_copy(
            update={"agent": config.agent.model_copy(update={"effort": normalized})}
        )
    workspace = cwd.expanduser().resolve() if cwd is not None else Path.cwd().resolve()
    if not workspace.is_dir():
        typer.echo(f"Workspace does not exist: {workspace}", err=True)
        raise typer.Exit(2)
    if model is None and config.roles.main is None:
        typer.echo(
            "No model configured. Pass --model provider/model-id or set [roles].main "
            f"in {config_path()}.",
            err=True,
        )
        raise typer.Exit(2)

    ensure_home()
    store = Store()
    registry = ProviderRegistry(config, AuthStore())
    router = RoleRouter(config, registry)
    agent = AgentLoop(
        config=config,
        store=store,
        router=router,
        tools=ToolRegistry(default_tools()),
    )
    session = store.create_session(
        cwd=str(workspace),
        model=model or config.roles.main,
        title=prompt[:80],
    )
    renderer: PlainRenderer | JsonRenderer = (
        JsonRenderer() if json_output else PlainRenderer(show_reasoning=show_reasoning)
    )

    async def execute():
        try:
            if ultra is not None:

                async def attempt(path: Path, index: int):
                    child = store.create_session(
                        cwd=str(path),
                        model=model or config.roles.main,
                        title=f"ultra {index + 1}: {prompt[:60]}",
                    )
                    result = await agent.run(
                        child,
                        prompt,
                        emit=renderer.emit,
                        permission_resolver=renderer.ask_permission,
                        ask_user=None if json_output else renderer.ask_user,  # type: ignore[union-attr]
                        model_override=model,
                        max_steps=max_steps,
                        extend_budget=max_steps is None or max_steps == 0,
                    )
                    return result.error is None

                return await BestOfN(workspace, runner=attempt, attempts=ultra).run()
            return await agent.run(
                session,
                prompt,
                emit=renderer.emit,
                permission_resolver=renderer.ask_permission,
                ask_user=None if json_output else renderer.ask_user,  # type: ignore[union-attr]
                model_override=model,
                max_steps=max_steps,
                extend_budget=max_steps is None or max_steps == 0,
            )
        finally:
            close_jobs = getattr(agent, "close_jobs", None)
            if close_jobs is not None:
                await close_jobs()
            await registry.aclose()
            store.close()

    try:
        result = asyncio.run(execute())
    except KeyboardInterrupt:
        typer.echo("Aborted.", err=True)
        raise typer.Exit(130) from None
    if ultra is not None:
        assert isinstance(result, BestOfNResult)
        if not json_output:
            typer.echo(f"best-of-n: {result.status} (winner {result.winner})")
            for attempt in result.attempts:
                typer.echo(f"  attempt {attempt.index}: {'pass' if attempt.passed else 'fail'}")
            if result.reason:
                typer.echo(result.reason)
        if result.status != "promoted":
            raise typer.Exit(1)
        return
    assert isinstance(result, RunResult)
    if not json_output:
        usage = result.usage
        typer.echo(
            f"[siwah] {result.steps} step(s), {usage.input_tokens} in / "
            f"{usage.output_tokens} out, allowance ${usage.allowance:.6f}"
        )
    if result.aborted:
        if not json_output:
            typer.echo("[siwah] run aborted", err=True)
        raise typer.Exit(130)
    if result.error:
        if not json_output:
            typer.echo(f"[siwah] {result.error}", err=True)
        raise typer.Exit(1)


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_free_port(host: str, preferred: int | None) -> tuple[int, bool]:
    """Return a usable port and whether a preferred port had to be replaced."""

    if preferred and preferred > 0 and _port_available(host, preferred):
        return preferred, False
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port = int(sock.getsockname()[1])
    return port, bool(preferred and preferred > 0)


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
) -> None:
    """Start the daemon and local HTTP + SSE API."""
    import uvicorn

    from ..server.app import create_app
    from ..server.runtime import DaemonRuntime

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    runtime = DaemonRuntime(config)
    token = os.environ.get("SIWAH_RUNTIME_TOKEN") or secrets.token_urlsafe(32)
    nonce = os.environ.get("SIWAH_SUPERVISOR_NONCE", "")
    bind_host = host or config.server.host
    requested_port = port if port is not None else config.server.port
    application = create_app(config, runtime=runtime, token=token, nonce=nonce)

    for attempt in range(3):
        bind_port, fallback = _pick_free_port(bind_host, requested_port if attempt == 0 else 0)
        if fallback and requested_port:
            typer.echo(
                f"port {requested_port} is busy; using free port {bind_port}",
                err=True,
            )
        write_private(
            runtime_path(),
            json.dumps(
                {
                    "host": bind_host,
                    "port": bind_port,
                    "token": token,
                    "pid": os.getpid(),
                },
                indent=2,
            )
            + "\n",
        )
        typer.echo(f"siwah daemon: http://{bind_host}:{bind_port}/v1")
        typer.echo(f"runtime credentials: {runtime_path()} (mode 600)")
        try:
            uvicorn.run(application, host=bind_host, port=bind_port, log_level="info")
            return
        except (OSError, SystemExit) as exc:
            if attempt == 2:
                with contextlib.suppress(OSError):
                    runtime_path().unlink(missing_ok=True)
                typer.echo(f"could not start siwah daemon: {exc}", err=True)
                raise typer.Exit(1) from exc
            typer.echo(f"daemon port {bind_port} became busy; retrying", err=True)


@app.command()
def acp() -> None:
    """Serve the Agent Client Protocol over stdio."""
    from ..acp import AcpServer

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    store = Store()
    server = AcpServer(config, store)
    try:
        asyncio.run(server.serve())
    finally:
        store.close()


@app.command("supervise", hidden=True)
def supervise() -> None:
    """Supervise the daemon: verify kernel, boot, and recover on failure."""
    from .supervise import run_supervisor

    raise typer.Exit(run_supervisor())


@app.command()
def rollback(
    tag: str | None = typer.Argument(None, help="Healthy tag or evo-N; default latest."),
) -> None:
    """Restore protected kernel paths or an evolution harness tag."""

    if tag and tag.startswith("evo-"):
        repo = HarnessRepository(harness_dir())
        try:
            commit = repo.rollback(tag)
        except Exception as exc:
            audit_event("evolve.rollback_manual", {"tag": tag, "ok": False, "detail": str(exc)})
            typer.echo(f"harness rollback failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        audit_event("evolve.rollback_manual", {"tag": tag, "ok": True, "commit": commit})
        typer.echo(f"harness rolled back to {tag} ({commit[:12]})")
        return

    result = rollback_to_tag(repo_root(), tag)
    audit_event(
        "rollback.manual",
        {"tag": result.tag, "ok": result.ok, "detail": result.detail},
    )
    if not result.ok:
        typer.echo(f"rollback failed: {result.detail}", err=True)
        raise typer.Exit(1)
    status = verify_manifest(repo_root())
    typer.echo(f"rolled back to {result.tag}")
    typer.echo(status.summary())
    if status.present and not status.ok:
        raise typer.Exit(1)


@kernel_app.command("verify")
def kernel_verify() -> None:
    """Verify the committed kernel manifest."""

    status = verify_manifest(repo_root())
    typer.echo(status.summary())
    if not status.present:
        raise typer.Exit(2)
    if not status.ok:
        raise typer.Exit(1)


@kernel_app.command("manifest")
def kernel_manifest(
    write: bool = typer.Option(False, "--write", help="Regenerate kernel.manifest.json."),
) -> None:
    """Show or regenerate the kernel manifest."""

    repo = repo_root()
    if write:
        target, data = write_manifest(repo)
        audit_event("manifest.written", {"path": str(target), "hash": data["hash"]})
        typer.echo(f"wrote {target}")
        typer.echo(f"hash {data['hash']}")
        return
    status = verify_manifest(repo)
    typer.echo(
        json.dumps({"path": str(manifest_path(repo)), "summary": status.summary()}, indent=2)
    )
    if status.present and not status.ok:
        raise typer.Exit(1)


@app.command()
def audit(limit: int = typer.Option(20, "--limit", min=1, max=500)) -> None:
    """Show the newest kernel audit events."""

    events = read_events(limit=limit)
    if not events:
        typer.echo("no audit events")
        return
    for event in events:
        typer.echo(json.dumps(event, ensure_ascii=False))


def _profile_max_output(model: str | None) -> int | None:
    """Return the provider-declared output ceiling for a model, if known."""

    if not model:
        return None
    provider, separator, model_id = model.partition("/")
    if not separator or not model_id:
        return None
    try:
        profile = load_profile(provider, model_id)
    except (OSError, ValueError):
        return None
    if profile.max_output is not None and profile.max_output > 0:
        return profile.max_output
    return None


@app.command()
def evolve(
    action: str | None = typer.Argument(None, help="Subcommand: log."),
    pause: bool = typer.Option(False, "--pause", help="Pause evolution cycles."),
    resume: bool = typer.Option(False, "--resume", help="Resume evolution cycles."),
    reset_cooldown: bool = typer.Option(
        False,
        "--reset-cooldown",
        help="Clear the cooldown after a failed preflight (privacy/config).",
    ),
    seed: bool = typer.Option(False, "--seed", help="Run the synthetic cold-start cycle."),
    live: bool = typer.Option(
        False,
        "--live",
        help=(
            "Probe with the real eval suite. Requires SIWAH_LIVE=1, "
            "a positive --max-allowance, and a model marked training=false."
        ),
    ),
    max_allowance: float | None = typer.Option(
        None, "--max-allowance", help="Per-probe allowance cap for --live."
    ),
    probe_limit: int | None = typer.Option(
        None, "--probe-limit", help="Limit tasks per live probe (smoke runs).", min=1
    ),
    model_option: str | None = typer.Option(
        None, "--model", help="provider/model-id for live reflection and probing."
    ),
    patch_file: Path | None = typer.Option(
        None, "--patch-file", help="Run the Level 2 gate for a unified diff."
    ),
    harness_dir_option: Path | None = typer.Option(
        None, "--harness-dir", help="Override the editable harness directory."
    ),
    weekly_allowance: float | None = typer.Option(
        None, "--weekly-allowance", help="Plan weekly limit in allowance units."
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=500),
) -> None:
    """Run one Level 0-1 evolution cycle, pause it, or show the audit log.

    Live mode uses the configured provider. In PowerShell set the environment
    variable first: `$env:SIWAH_LIVE="1"`. In bash use `export SIWAH_LIVE=1`.
    The selected model needs a privacy profile with `training=false`; create it
    with `siwah models probe <model>` and `siwah models privacy <model>
    --training false` only when the provider contract confirms it.

    Proposal output uses the model profile's `max_output` by default. Set
    `SIWAH_EVOLVE_MAX_TOKENS` to a positive integer for an explicit cap, or to
    `none`/`0` to omit `max_tokens` and use the provider default.
    """
    from ..adapt.engine import EvolutionEngine
    from ..adapt.level2 import Level2Evolver
    from ..adapt.models import Candidate
    from ..adapt.prober import EvalProber, ProbeError
    from ..adapt.promoter import Promoter
    from ..adapt.proposer import LLMProposer, ProposalError
    from ..adapt.reflector import PrivacyError, Reflector
    from ..adapt.synthetic import SeedProposer, SeedReflector, seed_prober

    if action == "log":
        state = load_state()
        typer.echo(f"tag {state.tag or 'evo-0'} · {state.status}")
        events = [
            event
            for event in read_events(limit=limit * 4)
            if str(event.get("event", "")).startswith("evolve.")
        ][-limit:]
        for event in events:
            typer.echo(json.dumps(event, ensure_ascii=False))
        return
    if action is not None:
        if "=" in action or action.upper().startswith("SIWAH_"):
            typer.echo(
                "It looks like you passed an environment assignment as an argument.\n"
                'PowerShell: $env:SIWAH_LIVE="1"\n'
                "bash: export SIWAH_LIVE=1\n"
                "Then rerun: siwah evolve --live --model <provider/model> "
                "--max-allowance <amount>",
                err=True,
            )
        else:
            typer.echo(
                "usage: siwah evolve [log] [--pause|--resume|--reset-cooldown|--seed]",
                err=True,
            )
        raise typer.Exit(2)
    if pause and resume:
        typer.echo("--pause and --resume are mutually exclusive", err=True)
        raise typer.Exit(2)
    if pause:
        set_paused(True)
        audit_event("evolve.paused", {"by": "cli"})
        typer.echo("evolution paused")
        return
    if resume:
        set_paused(False)
        audit_event("evolve.resumed", {"by": "cli"})
        typer.echo("evolution resumed")
        return
    if reset_cooldown:
        if pause or resume or seed or live or patch_file is not None:
            typer.echo("--reset-cooldown cannot be combined with other actions", err=True)
            raise typer.Exit(2)
        try:
            config = load_config()
        except ConfigError as exc:
            typer.echo(f"Config error: {exc}", err=True)
            raise typer.Exit(2) from exc
        BudgetGovernor(config.budget).reset_cooldown()
        audit_event("evolve.cooldown_reset", {"by": "cli"})
        typer.echo("evolution cooldown cleared")
        return
    if patch_file is not None:
        if seed or live:
            typer.echo("--patch-file cannot be combined with --seed or --live", err=True)
            raise typer.Exit(2)
        try:
            config = load_config()
        except ConfigError as exc:
            typer.echo(f"Config error: {exc}", err=True)
            raise typer.Exit(2) from exc
        path = patch_file.expanduser().resolve()
        try:
            diff = path.read_text(encoding="utf-8")
        except OSError as exc:
            typer.echo(f"Cannot read patch: {exc}", err=True)
            raise typer.Exit(2) from exc
        candidate = Candidate(
            id=f"level2-{path.stem}",
            level=2,
            kind="core_code",
            scope="global",
            target_paths=[],
            changes={},
            diff=diff,
            rationale=str(path),
            expected_metric="Level 2 gate",
            risk="core code change",
        )
        result = Level2Evolver(repo_root(), gate=config.gate).run(candidate)
        typer.echo(f"status: {result.status}")
        if result.reason:
            typer.echo(result.reason)
        if result.promotion:
            typer.echo(f"promoted as {result.promotion.tag}")
        if result.checks:
            typer.echo(json.dumps(result.checks, ensure_ascii=False))
        raise typer.Exit(0 if result.status == "promoted" else 1)
    if live:
        if os.environ.get("SIWAH_LIVE") != "1":
            typer.echo(
                "--live requires SIWAH_LIVE=1.\n"
                'PowerShell: $env:SIWAH_LIVE="1"\n'
                "bash: export SIWAH_LIVE=1\n"
                "Then rerun: siwah evolve --live --model <provider/model> "
                "--max-allowance <amount>",
                err=True,
            )
            raise typer.Exit(2)
        if max_allowance is None or max_allowance <= 0:
            typer.echo(
                "--live requires a positive --max-allowance (for example 0.05)",
                err=True,
            )
            raise typer.Exit(2)
    if not seed and not live:
        typer.echo(
            "Non-seed evolution requires real evaluation: use --live with "
            "SIWAH_LIVE=1 and a positive --max-allowance. "
            "Synthetic evaluation is available only with explicit --seed.",
            err=True,
        )
        raise typer.Exit(2)

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if harness_dir_option is None:
        ensure_harness()
        target_harness = harness_dir()
    else:
        target_harness = harness_dir_option.expanduser().resolve()
        if not target_harness.is_dir():
            typer.echo(f"Harness directory does not exist: {target_harness}", err=True)
            raise typer.Exit(2)
    repo = HarnessRepository(target_harness)
    repo.ensure()
    weekly = weekly_allowance
    if weekly is None:
        raw = os.environ.get("SIWAH_WEEKLY_ALLOWANCE")
        if raw:
            try:
                weekly = float(raw)
            except ValueError:
                typer.echo("SIWAH_WEEKLY_ALLOWANCE must be a number", err=True)
                raise typer.Exit(2) from None
    governor = BudgetGovernor(config.budget, weekly_allowance=weekly)
    model = model_option or config.roles.reflector or config.roles.main or "seed/local"
    eval_model = (
        model_option or config.roles.eval_screen or config.roles.reflector or config.roles.main
    )
    if live and eval_model is None:
        typer.echo(
            "live evolution needs [roles].eval_screen, [roles].reflector, or [roles].main",
            err=True,
        )
        raise typer.Exit(2)

    token_override = os.environ.get("SIWAH_EVOLVE_MAX_TOKENS")
    if token_override is None:
        # No artificial siwah cap: use the provider-declared model maximum when
        # the profile has it. With no profile, omit max_tokens and let the
        # provider choose its default.
        proposal_max_tokens = _profile_max_output(model)
    else:
        normalized = token_override.strip().lower()
        if normalized in {"", "none", "unlimited", "0"}:
            proposal_max_tokens = None
        else:
            try:
                proposal_max_tokens = int(normalized)
            except ValueError:
                typer.echo(
                    "SIWAH_EVOLVE_MAX_TOKENS must be a positive integer, 'none', or 0",
                    err=True,
                )
                raise typer.Exit(2) from None
            if proposal_max_tokens <= 0:
                typer.echo(
                    "SIWAH_EVOLVE_MAX_TOKENS must be a positive integer, 'none', or 0",
                    err=True,
                )
                raise typer.Exit(2)
    store: Store | None = None
    registry: ProviderRegistry | None = None
    model_spent = 0.0
    actual_model: str | None = None

    async def cycle():
        nonlocal store, registry
        cycle_started = time.monotonic()
        if seed:
            reflector = SeedReflector()
            proposer: object = SeedProposer(str(target_harness))
            observations = []
        else:
            if (
                model_option is None
                and config.roles.reflector is None
                and config.roles.main is None
            ):
                raise ConfigError("configure [roles].reflector or [roles].main first")
            registry = ProviderRegistry(config, AuthStore())
            router = RoleRouter(config, registry)

            async def call(prompt: str) -> str:
                nonlocal model_spent, actual_model
                limit = governor.check_cycle(model_spent, time.monotonic() - cycle_started)
                if not limit.allowed:
                    raise ProbeError(limit.reason or "evolution budget exceeded")
                reported_allowance = 0.0

                async def emit(event: ProviderEvent) -> None:
                    nonlocal model_spent, reported_allowance
                    if event.usage is not None:
                        delta = max(0.0, event.usage.allowance - reported_allowance)
                        model_spent += delta
                        reported_allowance += delta

                outcome = await router.stream(
                    "reflector",
                    messages=[Message(role="user", content=prompt)],
                    tools=[],
                    session_id="evolution",
                    emit=emit,
                    model_override=model,
                    temperature=0.0,
                    max_tokens=proposal_max_tokens,
                    require_private=True,
                )
                model_spent += max(0.0, outcome.usage.allowance - reported_allowance)
                actual_model = f"{outcome.provider}/{outcome.model}"
                audit_event(
                    "evolve.model_call",
                    {
                        "model": actual_model,
                        "allowance": max(reported_allowance, outcome.usage.allowance),
                        "max_tokens": proposal_max_tokens,
                    },
                )
                limit = governor.check_cycle(model_spent, time.monotonic() - cycle_started)
                if not limit.allowed:
                    raise ProbeError(limit.reason or "evolution budget exceeded")
                return outcome.text or ""

            reflector = Reflector(call, model_used=lambda: actual_model)
            proposer = LLMProposer(call)
            observations = None
            store = Store()
        prober = (
            EvalProber(
                eval_dir=None,
                config=config,
                auth=AuthStore(),
                model=str(eval_model),
                tier="t2",
                max_steps=16,
                max_allowance=float(max_allowance or 0.0),
                runs_per_task=1,
                limit=probe_limit,
            )
            if live
            else seed_prober()
        )
        engine = EvolutionEngine(
            target_harness,
            reflector=reflector,
            proposer=proposer,
            prober=prober,
            promoter=Promoter(repo),
            governor=governor,
            gate=config.gate,
            canary=config.canary,
            model=model,
            store=store,
            state_file=evolution_state_path(),
            model_allowance=lambda: model_spent,
        )
        try:
            return await engine.run_once(observations)
        finally:
            if registry is not None:
                await registry.aclose()
            if store is not None:
                store.close()

    try:
        result = asyncio.run(cycle())
    except (
        ConfigError,
        ProposalError,
        PrivacyError,
        ProbeError,
        PromotionError,
        ProviderError,
    ) as exc:
        typer.echo(f"evolution failed: {exc}", err=True)
        raise typer.Exit(2) from exc

    if result.promotion is not None:
        typer.echo(f"promoted {result.promotion.candidate_id} as {result.promotion.tag}")
        typer.echo(f"changed: {', '.join(result.promotion.changed_paths)}")
        if result.gate:
            typer.echo(json.dumps(result.gate, ensure_ascii=False))
    else:
        typer.echo(f"status: {result.status}")
        if result.reason:
            typer.echo(result.reason)
        if result.gate:
            typer.echo(json.dumps(result.gate, ensure_ascii=False))
    if result.spent_allowance:
        typer.echo(f"probe spend: {result.spent_allowance:.8f} allowance, {result.duration_s:.1f}s")


@app.command()
def trace(
    session_id: str = typer.Argument(..., help="Session id."),
    limit: int = typer.Option(200, "--limit", min=1, max=10_000),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Show per-step model traces for a session."""

    store = Store()
    try:
        session = store.get_session(session_id)
        if session is None:
            typer.echo(f"Unknown session: {session_id}", err=True)
            raise typer.Exit(2)
        events = store.list_traces(session_id, limit=limit)
    finally:
        store.close()
    if as_json:
        typer.echo(json.dumps(events, indent=2, ensure_ascii=False))
        return
    table = Table(title=f"trace · {session_id}", box=None)
    table.add_column("step")
    table.add_column("model")
    table.add_column("latency", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("allowance", justify="right")
    table.add_column("tools")
    for event in events:
        table.add_row(
            str(event["step_id"]),
            str(event.get("model") or "-"),
            f"{float(event.get('latency_ms') or 0):.0f}ms",
            f"{event.get('input_tokens', 0)}/{event.get('output_tokens', 0)}",
            f"{float(event.get('allowance') or 0):.8f}",
            ",".join(event.get("tool_names") or []),
        )
    console.print(table)
    if not events:
        typer.echo("(no trace events)")


@auth_app.command("login")
def auth_login(
    provider: str | None = typer.Argument(None),
    api_key: bool = typer.Option(False, "--api-key", help="Use an existing API key."),
    account: str | None = typer.Option(
        None, "--account", help="Reconnect a saved ChatGPT account."
    ),
) -> None:
    """Connect a provider using browser sign-in or an API key."""
    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    name = provider
    if name and name not in config.providers:
        try:
            config = add_default_providers()
        except (OSError, ConfigError) as exc:
            typer.echo(f"Config error: {exc}", err=True)
            raise typer.Exit(2) from exc
    if name is None:
        if len(config.providers) == 1:
            name = next(iter(config.providers))
        else:
            typer.echo("Configured providers: " + ", ".join(config.providers))
            name = typer.prompt("Provider name")
    if name not in config.providers:
        typer.echo(f"Unknown provider '{name}'. Add it to {config_path()} first.", err=True)
        raise typer.Exit(2)
    preset = config.providers[name]
    if preset.auth.scheme == "none":
        typer.echo(f"{name} does not require a credential.")
        return
    if supports_chatgpt_login(preset):
        if api_key:
            typer.echo("Use the openai provider for API-key authentication.", err=True)
            raise typer.Exit(2)
        try:
            asyncio.run(_chatgpt_login(account))
        except AuthError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        typer.echo("ChatGPT account connected.")
        return
    if not api_key and supports_browser_login(preset):
        login = OpenRouterLogin()
        url = login.authorization_url()
        typer.echo(f"Open {url} and copy the one-time code after signing in.")
        with contextlib.suppress(OSError, webbrowser.Error):
            webbrowser.open(url)
        code = typer.prompt("OpenRouter sign-in code", hide_input=True)
        try:
            key = asyncio.run(login.exchange(code))
        except AuthError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
    else:
        key = typer.prompt(f"API key for {name}", hide_input=True)
    if not key.strip():
        typer.echo("Empty key; nothing saved.", err=True)
        raise typer.Exit(2)
    path = AuthStore().save_key(name, key.strip())
    typer.echo(f"Saved credential for {name} in {path}")


async def _chatgpt_login(account: str | None) -> None:
    auth = ChatGPTAuth()
    try:
        flow = await auth.begin(account)
        typer.echo(f"Sign in with ChatGPT at {flow.url}")
        with contextlib.suppress(OSError, webbrowser.Error):
            await asyncio.to_thread(webbrowser.open, flow.url)
        typer.echo("Waiting for authorization in your browser. Press Ctrl+C to cancel.")
        while True:
            try:
                await auth.finish(flow.id)
                break
            except ChatGPTSignInPending:
                await asyncio.sleep(0.5)
    finally:
        auth.close()


@auth_app.command("status")
def auth_status() -> None:
    """Show which providers have credentials, without printing secrets."""
    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    store = AuthStore()
    for name, provider in config.providers.items():
        if supports_chatgpt_login(provider):
            accounts = ChatGPTAuth().accounts()
            if not accounts:
                typer.echo(f"{name}: sign in with ChatGPT to add an account")
            for account in accounts:
                state = (
                    "active"
                    if account.active
                    else "connected"
                    if account.connected
                    else "signed out"
                )
                typer.echo(f"{name}: {account.label} ({state}) / {account.client_id}")
            continue
        env_name = provider.auth.env
        env_set = bool(env_name and os.environ.get(env_name))
        file_set = store.configured(name)
        typer.echo(
            f"{name}: env={env_name or '-'} {'set' if env_set else 'unset'}; "
            f"auth_file={'set' if file_set else 'unset'}"
        )


@auth_app.command("logout")
def auth_logout(
    provider: str = typer.Argument(...),
    account: str | None = typer.Option(None, "--account", help="Sign out a saved ChatGPT account."),
) -> None:
    """Remove a stored credential or sign out of ChatGPT."""
    preset = load_config().providers.get(provider)
    if preset is not None and supports_chatgpt_login(preset):
        try:
            revoked = asyncio.run(ChatGPTAuth().sign_out(account))
        except AuthError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from exc
        typer.echo(
            "Signed out."
            if revoked
            else "Signed out locally. Check connected apps in ChatGPT Settings."
        )
        return
    removed = AuthStore().delete_key(provider)
    typer.echo("Removed." if removed else "No stored key for that provider.")


def _training_label(provider: str, model: str) -> str:
    try:
        profile = load_profile(provider, model)
    except (OSError, ValueError):
        return "?"
    if profile.privacy.training is False:
        return "false"
    if profile.privacy.training is True:
        return "true"
    return "?"


@models_app.command("list")
def models_list(
    provider: str | None = typer.Option(None, "--provider", help="Provider name."),
) -> None:
    """List models from the configured provider endpoint."""
    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not config.providers:
        typer.echo(f"No providers configured in {config_path()}.", err=True)
        raise typer.Exit(2)
    names = [provider] if provider else list(config.providers)
    auth_store = AuthStore()

    def has_credential(name: str) -> bool:
        item = config.providers[name]
        if item.auth.scheme == "none":
            return True
        if item.auth.env and os.environ.get(item.auth.env):
            return True
        return auth_store.configured(name)

    missing = [name for name in names if not has_credential(name)]
    for name in missing:
        console.print(
            f"[yellow]{name}: credential missing; run `siwah` and /connect, "
            f"or `siwah auth login {name}`[/yellow]"
        )
    names = [name for name in names if has_credential(name)]
    if not names:
        typer.echo("No provider credentials available yet.", err=True)
        raise typer.Exit(2)

    table = Table(title="siwah models")
    table.add_column("id")
    table.add_column("ctx", justify="right")
    table.add_column("out", justify="right")
    table.add_column("training")
    table.add_column("tools")
    table.add_column("reasoning")

    async def fetch() -> list[tuple[str, ModelInfo]]:
        registry = ProviderRegistry(config, AuthStore())
        rows: list[tuple[str, ModelInfo]] = []
        try:
            for name in names:
                for model in await registry.list_models(name):
                    rows.append((f"{name}/{model.id}", model))
            return rows
        finally:
            await registry.aclose()

    try:
        rows = asyncio.run(fetch())
    except (ProviderError, AuthError, ConfigError) as exc:
        typer.echo(f"Model list failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    for label, model in rows:
        provider_name, _, model_name = label.partition("/")
        table.add_row(
            label,
            str(model.context_window or "?"),
            str(model.max_output or "?"),
            _training_label(provider_name, model_name or model.id),
            str(model.supports_tools),
            str(model.supports_reasoning),
        )
    console.print(table)


@models_app.command("probe")
def models_probe(
    model_ref: str | None = typer.Argument(None, help="provider/model-id."),
    provider: str | None = typer.Option(None, "--provider"),
    model_id: str | None = typer.Option(None, "--model"),
) -> None:
    """Probe model capabilities and write a profile under ~/.siwah/harness/profiles."""
    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        reference = _resolve_model_ref(config, model_ref, provider, model_id)
    except (ValueError, ConfigError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    async def probe() -> tuple[ModelProfile, Path]:
        registry = ProviderRegistry(config, AuthStore())
        try:
            adapter = registry.adapter(reference.provider)
            info = None
            try:
                models = await adapter.list_models()
                info = next((item for item in models if item.id == reference.model), None)
            except (ProviderError, AuthError):
                info = None
            profile = await probe_model(
                adapter,
                reference.provider,
                reference.model,
                model_info=info.model_dump(mode="json") if info else None,
            )
            try:
                previous = load_profile(reference.provider, reference.model)
            except (OSError, ValueError):
                previous = None
            profile = merge_privacy(profile, previous)
            profile.probed_at = datetime.now(UTC).isoformat()
            return profile, save_profile(profile)
        finally:
            await registry.aclose()

    try:
        profile, path = asyncio.run(probe())
    except (ProviderError, AuthError, ConfigError) as exc:
        typer.echo(f"Probe failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    training = (
        "false"
        if profile.privacy.training is False
        else "true"
        if profile.privacy.training is True
        else "unmarked"
    )
    retention = (
        "?" if profile.privacy.retention_days is None else str(profile.privacy.retention_days)
    )
    typer.echo(f"Profile: {path}")
    typer.echo(
        f"api={profile.api} tools={profile.supports_tools} "
        f"parallel={profile.parallel_tool_calls} reasoning={profile.reasoning_field} "
        f"cache={profile.cache_mode} training={training} retention={retention}"
    )
    if profile.privacy.training is None:
        typer.echo(
            "evolution requires training=false; verify the provider contract, then run: "
            f"siwah models privacy {reference.provider}/{reference.model} --training false"
        )


@models_app.command("privacy")
def models_privacy(
    model_ref: str = typer.Argument(..., help="provider/model-id."),
    training: str = typer.Option(..., "--training", help="false, true, or unknown."),
    retention_days: int | None = typer.Option(None, "--retention-days", min=0),
) -> None:
    """Record a human privacy assertion used by live evolution."""

    try:
        config = load_config()
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        reference = _resolve_model_ref(config, model_ref, None, None)
    except (ValueError, ConfigError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    normalized = training.strip().lower()
    value: bool | None
    if normalized in {"false", "no", "0"}:
        value = False
    elif normalized in {"true", "yes", "1"}:
        value = True
    elif normalized in {"unknown", "unset", "none"}:
        value = None
    else:
        typer.echo("--training must be false, true, or unknown", err=True)
        raise typer.Exit(2)

    try:
        _, path = set_privacy(
            reference.provider,
            reference.model,
            training=value,
            retention_days=retention_days,
        )
    except (OSError, ValueError):
        typer.echo(
            f"No profile for {model_ref}; run `siwah models probe {model_ref}` first.",
            err=True,
        )
        raise typer.Exit(2) from None

    label = "false" if value is False else "true" if value is True else "unknown"
    typer.echo(f"privacy updated: {reference.provider}/{reference.model} training={label}")
    typer.echo(f"Profile: {path}")
    if value is False:
        typer.echo("Only use training=false when the provider contract confirms it.")


@plugin_app.command("list")
def plugin_list() -> None:
    """List installed declarative plugins."""

    infos = list_plugins()
    if not infos:
        typer.echo("No plugins installed. Install one with `siwah plugin install <path>`.")
        return
    table = Table(title="siwah plugins")
    table.add_column("name")
    table.add_column("version")
    table.add_column("status")
    table.add_column("description")
    for info in infos:
        status = "invalid" if not info.valid else "enabled" if info.enabled else "disabled"
        description = info.error or info.description
        table.add_row(info.name, info.version, status, description)
    console.print(table)


@plugin_app.command("info")
def plugin_info(name: str = typer.Argument(..., help="Plugin name.")) -> None:
    """Show one plugin and its content counts."""

    info = next((item for item in list_plugins() if item.name == name), None)
    if info is None:
        typer.echo(f"Plugin not installed: {name}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(info.__dict__, indent=2, default=str, ensure_ascii=False))
    typer.echo(
        "content: "
        f"skills={len(list((info.path / 'skills').glob('*/SKILL.md')))}, "
        f"commands={len(list((info.path / 'commands').glob('*.md')))}, "
        f"subagents={len(list((info.path / 'subagents').glob('*.md')))}, "
        f"hooks={len(list((info.path / 'hooks').glob('*.json')))}"
    )
    mcp_path = info.path / "mcp.json"
    if mcp_path.is_file():
        try:
            payload = json.loads(mcp_path.read_text(encoding="utf-8"))
            servers = payload.get("servers", {})
            count = len(servers) if isinstance(servers, dict) else 0
        except (OSError, json.JSONDecodeError):
            count = 0
        typer.echo(f"mcp_servers={count}")


@plugin_app.command("install")
def plugin_install(
    source: Path = typer.Argument(..., help="Plugin directory or .zip file."),
    force: bool = typer.Option(False, "--force", help="Replace an installed plugin."),
) -> None:
    """Install a declarative plugin bundle."""

    try:
        info = install_plugin(source, force=force)
    except PluginError as exc:
        typer.echo(f"Plugin install failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Installed {info.name} {info.version} at {info.path}")


@plugin_app.command("remove")
def plugin_remove(
    name: str = typer.Argument(..., help="Plugin name."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Remove an installed plugin."""

    if not yes and not typer.confirm(f"Remove plugin {name}?"):
        raise typer.Exit(0)
    try:
        remove_plugin(name)
    except PluginError as exc:
        typer.echo(f"Plugin remove failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Removed plugin {name}")


@plugin_app.command("enable")
def plugin_enable(name: str = typer.Argument(..., help="Plugin name.")) -> None:
    """Enable a plugin for the next session."""

    try:
        info = set_enabled(name, True)
    except PluginError as exc:
        typer.echo(f"Plugin enable failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Enabled plugin {info.name}")


@plugin_app.command("disable")
def plugin_disable(name: str = typer.Argument(..., help="Plugin name.")) -> None:
    """Disable a plugin without removing its files."""

    try:
        info = set_enabled(name, False)
    except PluginError as exc:
        typer.echo(f"Plugin disable failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Disabled plugin {info.name}")


@skills_app.command("list")
def skills_list() -> None:
    """List skills in the editable harness."""
    ensure_harness()
    skills = list_skills(Path.cwd())
    if not skills:
        typer.echo("No skills yet. The agent can create one with skill_manage.")
        return
    table = Table(title="siwah skills")
    table.add_column("name")
    table.add_column("description")
    for skill in skills:
        table.add_row(skill.name, skill.description)
    console.print(table)


@skills_app.command("view")
def skills_view(name: str = typer.Argument(..., help="Skill name.")) -> None:
    """Print a skill's full Markdown body."""
    ensure_harness()
    try:
        skill = read_skill(name, Path.cwd())
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"# {skill.name}")
    typer.echo(skill.description)
    typer.echo()
    typer.echo(skill.content)


@memory_app.command("show")
def memory_show(cwd: Path | None = typer.Option(None, "--cwd")) -> None:
    """Show user, project, and quick memory."""
    ensure_harness()
    workspace = (cwd or Path.cwd()).expanduser().resolve()
    snapshot = read_memory(workspace)
    for title, item in (
        ("user", snapshot.user),
        ("project", snapshot.project),
        ("quick", snapshot.quick),
    ):
        console.print(f"[bold]{title}[/bold] ({item.path})")
        console.print(item.content or "(empty)")
        console.print()


def _mcp_pairs(values: list[str] | None, label: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise McpError(f"{label} must use Name=Value: {value}")
        key, item = value.split("=", 1)
        if not key.strip():
            raise McpError(f"{label} has an empty name: {value}")
        pairs[key.strip()] = item.strip()
    return pairs


def _mcp_redact(config: dict[str, object]) -> dict[str, object]:
    clone = json.loads(json.dumps(config))
    for key in ("headers", "env"):
        value = clone.get(key)
        if isinstance(value, dict):
            clone[key] = {name: "***" for name in value}
    return clone


def _mcp_discover(
    manager: McpManager, server: str | None
) -> tuple[dict[str, list[McpToolInfo]], dict[str, str]]:
    async def run() -> tuple[dict[str, list[McpToolInfo]], dict[str, str]]:
        try:
            return await manager.list_tools_report(server)
        finally:
            await manager.close()

    return asyncio.run(run())


def _mcp_manager() -> McpManager:
    payload = load_mcp_config(strict=True)
    return McpManager(payload.get("servers", {}), include_plugins=True)


@mcp_app.command("path")
def mcp_path() -> None:
    """Print the MCP configuration file path."""
    ensure_harness()
    typer.echo(str(mcp_config_path()))


@mcp_app.command("list")
def mcp_list(
    tools: bool = typer.Option(False, "--tools", help="Discover tools (starts servers)."),
) -> None:
    """List configured MCP servers."""
    ensure_harness()
    try:
        manager = _mcp_manager()
    except McpError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    infos = manager.describe()
    if not infos:
        typer.echo(
            "No MCP servers configured. Add one with `siwah mcp add <name> "
            '--command "..."` or `--url https://...`.'
        )
        typer.echo(f"Config: {mcp_config_path()}")
        return
    table = Table(title="siwah MCP servers")
    table.add_column("name")
    table.add_column("transport")
    table.add_column("status")
    table.add_column("auth")
    table.add_column("oauth")
    table.add_column("description")
    for info in infos:
        if not info.valid:
            status = "invalid"
            description = info.error or ""
        elif not info.enabled:
            status = "disabled"
            description = info.description
        else:
            status = "enabled"
            description = info.description
        oauth = oauth_status(info.name) if info.auth == "oauth" else "-"
        table.add_row(info.name, info.transport, status, info.auth, oauth, description)
    console.print(table)
    if not tools:
        typer.echo("Use --tools to discover tool names and descriptions.")
        return
    found, errors = _mcp_discover(manager, None)
    for server, items in found.items():
        for item in items:
            typer.echo(f"{server}/{item.name}: {item.description}")
    for server, error in errors.items():
        typer.echo(f"{server}: ERROR {error}", err=True)
    if errors:
        raise typer.Exit(1)


@mcp_app.command("add")
def mcp_add(
    name: str = typer.Argument(..., help="Server name used by the mcp tool."),
    command: str | None = typer.Option(
        None, "--command", help='stdio command line, e.g. "npx -y server".'
    ),
    extra_arg: list[str] | None = typer.Option(
        None, "--arg", help="Extra stdio argument; repeatable."
    ),
    url: str | None = typer.Option(None, "--url", help="Streamable HTTP endpoint."),
    header: list[str] | None = typer.Option(
        None, "--header", help="HTTP header Name=Value; repeatable."
    ),
    bearer_token_env_var: str | None = typer.Option(
        None, "--bearer-token-env-var", help="Environment variable holding an HTTPS bearer token."
    ),
    auth: str | None = typer.Option(None, "--auth", help="HTTP auth mode: none or oauth."),
    scope: list[str] | None = typer.Option(None, "--scope", help="OAuth scope; repeatable."),
    client_id: str | None = typer.Option(
        None, "--client-id", help="Pre-registered OAuth client id."
    ),
    client_secret_env: str | None = typer.Option(
        None,
        "--client-secret-env",
        help="Environment variable holding the OAuth client secret.",
    ),
    env: list[str] | None = typer.Option(
        None, "--env", help="stdio environment Name=Value; repeatable."
    ),
    cwd: str | None = typer.Option(None, "--cwd", help="stdio working directory."),
    timeout_s: float = typer.Option(30.0, "--timeout", help="Request timeout in seconds."),
    description: str = typer.Option("", "--description", help="Short server description."),
    transport: str | None = typer.Option(None, "--transport", help="stdio or http."),
    force: bool = typer.Option(False, "--force", help="Replace an existing server."),
) -> None:
    """Add or replace an MCP server definition."""
    ensure_harness()
    try:
        if bearer_token_env_var is not None and not url:
            raise McpError("--bearer-token-env-var requires --url")
        if command and url:
            raise McpError("Use --command for stdio or --url for HTTP, not both")
        if command:
            parts = split_command(command)
            if extra_arg:
                parts.extend(extra_arg)
            config: dict[str, object] = {"transport": "stdio", "command": parts}
            if cwd:
                config["cwd"] = cwd
            environment = _mcp_pairs(env, "--env")
            if environment:
                config["env"] = environment
        elif url:
            if extra_arg or env or cwd:
                raise McpError("--arg, --env, and --cwd are only valid with --command")
            config = {"transport": "http", "url": url}
            headers = _mcp_pairs(header, "--header")
            if headers:
                config["headers"] = headers
            if bearer_token_env_var is not None:
                config["bearer_token_env_var"] = bearer_token_env_var
            if auth:
                config["auth"] = auth
            if scope:
                config["scopes"] = list(scope)
            if client_id:
                config["client_id"] = client_id
            if client_secret_env:
                config["client_secret_env"] = client_secret_env
        else:
            if auth or scope or client_id or client_secret_env:
                raise McpError(
                    "--auth, --scope, --client-id, and --client-secret-env require --url"
                )
            raise McpError("Provide --command (stdio) or --url (HTTP)")
        if transport:
            requested = transport.strip().lower()
            if requested in {"streamable-http", "streamable_http"}:
                requested = "http"
            if requested not in {"stdio", "http"}:
                raise McpError("--transport must be stdio or http")
            if requested != config["transport"]:
                raise McpError("--transport does not match --command/--url")
        if header and config["transport"] != "http":
            raise McpError("--header is only valid with --url")
        config["timeout_s"] = timeout_s
        if description:
            config["description"] = description
        config = normalize_server(name, config)
        payload = load_mcp_config(strict=True)
        servers = payload.setdefault("servers", {})
        if name in servers and not force:
            raise McpError(f"MCP server {name} already exists; use --force to replace it")
        servers[name] = config
        path = save_mcp_config(payload)
    except McpError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Saved {name} ({config['transport']}) to {path}")


@mcp_app.command("remove")
def mcp_remove(
    name: str = typer.Argument(..., help="Server name to remove."),
) -> None:
    """Remove an MCP server definition."""
    ensure_harness()
    try:
        payload = load_mcp_config(strict=True, include_plugins=False)
        servers = payload.get("servers", {})
        if name not in servers:
            if name in plugin_mcp_servers():
                raise McpError(
                    f"MCP server {name} is provided by a plugin; "
                    "disable or remove the plugin instead"
                )
            raise McpError(f"Unknown MCP server: {name}")
        del servers[name]
        path = save_mcp_config(payload)
    except McpError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"Removed {name} from {path}")


@mcp_app.command("auth")
def mcp_auth(
    name: str = typer.Argument(..., help="HTTP MCP server configured with auth=oauth."),
    device: bool = typer.Option(
        False, "--device", help="Use the device flow instead of a browser callback."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the authorization URL without opening a browser."
    ),
) -> None:
    """Authorize an MCP server with OAuth (generic discovery + PKCE)."""

    ensure_harness()
    try:
        payload = load_mcp_config(strict=True)
        servers = payload.get("servers", {})
        raw = servers.get(name)
        if not isinstance(raw, dict):
            raise McpError(f"Unknown MCP server: {name}")
        config = normalize_server(name, raw)
        if config["transport"] != "http":
            raise McpError(f"MCP server {name} is not an HTTP server")
        if config.get("auth") != "oauth":
            raise McpError(f"MCP server {name} is not configured with auth=oauth")
    except McpError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc

    client = OAuthClient(
        name,
        str(config["url"]),
        client_id=config.get("client_id"),
        client_secret_env=config.get("client_secret_env"),
        scopes=config.get("scopes") or (),
        notify=lambda message: typer.echo(message),
    )

    async def run() -> None:
        try:
            await client.authorize(device=device, no_browser=no_browser)
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
    except (McpAuthRequired, McpOAuthError, httpx.HTTPError) as exc:
        typer.echo(f"OAuth failed: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"OAuth authenticated: {name}")
    typer.echo(f"state: {oauth_state_path(name)}")


@mcp_app.command("logout")
def mcp_logout(name: str = typer.Argument(..., help="MCP server to log out.")) -> None:
    """Remove stored OAuth tokens for an MCP server."""

    ensure_harness()
    removed = delete_state(name)
    typer.echo(f"OAuth state removed: {name}" if removed else f"No OAuth state for {name}")


@mcp_app.command("show")
def mcp_show(
    name: str = typer.Argument(..., help="Server name to show."),
) -> None:
    """Show one server definition with secret values redacted."""
    ensure_harness()
    payload = load_mcp_config(strict=True)
    servers = payload.get("servers", {})
    config = servers.get(name)
    if not isinstance(config, dict):
        typer.echo(f"Unknown MCP server: {name}", err=True)
        raise typer.Exit(2)
    typer.echo(json.dumps(_mcp_redact(config), indent=2, ensure_ascii=False))


def _mcp_print_tools(server: str | None) -> None:
    ensure_harness()
    try:
        manager = _mcp_manager()
    except McpError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    found, errors = _mcp_discover(manager, server)
    for server_name, items in found.items():
        for item in items:
            typer.echo(f"{server_name}/{item.name}: {item.description}")
    for server_name, error in errors.items():
        typer.echo(f"{server_name}: ERROR {error}", err=True)
    if errors:
        raise typer.Exit(1)
    if not found:
        typer.echo("(no MCP tools)")


@mcp_app.command("status")
def mcp_status(
    server: str | None = typer.Argument(None, help="Optional server name."),
) -> None:
    """Check whether configured MCP servers connect and report tool counts."""
    ensure_harness()
    try:
        manager = _mcp_manager()
    except McpError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    infos = manager.describe()
    if server is not None and not any(info.name == server for info in infos):
        typer.echo(f"Unknown MCP server: {server}", err=True)
        raise typer.Exit(2)

    async def run() -> list[tuple[str, str, str, str, str, int, float, str]]:
        rows: list[tuple[str, str, str, str, str, int, float, str]] = []
        try:
            for info in infos:
                if server is not None and info.name != server:
                    continue
                oauth = oauth_status(info.name) if info.auth == "oauth" else "-"
                if not info.valid:
                    rows.append(
                        (
                            info.name,
                            info.transport,
                            info.auth,
                            oauth,
                            "invalid",
                            0,
                            0.0,
                            info.error or "",
                        )
                    )
                    continue
                if not info.enabled:
                    rows.append(
                        (info.name, info.transport, info.auth, oauth, "disabled", 0, 0.0, "")
                    )
                    continue
                started = time.perf_counter()
                tools, errors = await manager.list_tools_report(info.name)
                elapsed = (time.perf_counter() - started) * 1000.0
                if info.name in errors:
                    rows.append(
                        (
                            info.name,
                            info.transport,
                            info.auth,
                            oauth,
                            "error",
                            0,
                            elapsed,
                            errors[info.name],
                        )
                    )
                else:
                    rows.append(
                        (
                            info.name,
                            info.transport,
                            info.auth,
                            oauth,
                            "connected",
                            len(tools.get(info.name, [])),
                            elapsed,
                            "",
                        )
                    )
        finally:
            await manager.close()
        return rows

    rows = asyncio.run(run())
    table = Table(title="siwah MCP status")
    table.add_column("server")
    table.add_column("transport")
    table.add_column("auth")
    table.add_column("oauth")
    table.add_column("status")
    table.add_column("tools", justify="right")
    table.add_column("latency", justify="right")
    table.add_column("detail")
    for name, transport, auth, oauth, status, count, elapsed, detail in rows:
        table.add_row(
            name,
            transport,
            auth,
            oauth,
            status,
            str(count),
            f"{elapsed:.0f} ms" if elapsed else "-",
            detail,
        )
    console.print(table)
    if any(row[4] == "error" for row in rows):
        raise typer.Exit(1)


@mcp_app.command("tools")
def mcp_tools(
    server: str | None = typer.Argument(None, help="Optional server name."),
) -> None:
    """Discover tools from configured MCP servers."""
    _mcp_print_tools(server)


@mcp_app.command("test")
def mcp_test(
    server: str = typer.Argument(..., help="Server name to initialize and test."),
) -> None:
    """Initialize one MCP server and list its tools."""
    _mcp_print_tools(server)
    typer.echo(f"MCP server {server}: ok")


@app.command("eval")
def eval_suite(
    suite: str = typer.Option("golden", "--suite", help="golden, heldout, or traps."),
    harness: str = typer.Option("siwah", "--harness", help="siwah or naive."),
    tier: str = typer.Option("t3", "--tier", help="t0, t1, t2, or t3."),
    model: str | None = typer.Option(
        None,
        "--model",
        help=(
            "Model id. With --live it defaults to the configured main model; "
            "otherwise the deterministic mock harness is used."
        ),
    ),
    seed: int = typer.Option(0, "--seed"),
    runs: int = typer.Option(1, "--runs", min=1, max=10),
    max_steps: int = typer.Option(8, "--max-steps", min=1, max=100),
    limit: int | None = typer.Option(None, "--limit", min=1),
    only: str | None = typer.Option(None, "--only", help="Comma-separated task ids."),
    baseline: bool = typer.Option(False, "--baseline", help="Run the naive baseline too."),
    live: bool = typer.Option(
        False,
        "--live",
        help=(
            "Use the real provider and model. Requires SIWAH_LIVE=1, "
            "--max-allowance, and a configured provider."
        ),
    ),
    max_allowance: float | None = typer.Option(None, "--max-allowance"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    write_budget: bool = typer.Option(
        False,
        "--write-budget",
        help="Write the calibrated budget table to config; requires --live.",
    ),
    as_json: bool = typer.Option(False, "--json"),
    output: Path | None = typer.Option(None, "--output", help="Report JSON path."),
    eval_dir: Path | None = typer.Option(None, "--eval-dir", help="Eval fixture root."),
) -> None:
    """Run an evaluation suite and render aggregate metrics."""
    from ..eval.mock import MOCK_MODEL
    from ..eval.report import comparison_summary, render_comparison, render_report, write_report
    from ..eval.runner import EvalRunner

    if live:
        if model is None:
            model = os.environ.get("SIWAH_LIVE_MODEL") or None
        if model is None:
            try:
                model = load_config().roles.main
            except ConfigError:
                model = None
        if not model:
            console.print(
                "[red]eval failed:[/red] --live requires a configured main model or --model"
            )
            raise typer.Exit(2)
    else:
        model = MOCK_MODEL
        if not as_json:
            typer.echo(
                "deterministic mock evaluation (no provider calls); "
                "use --live to measure a real model",
                err=True,
            )
    if write_budget and not live:
        console.print("[red]eval failed:[/red] --write-budget requires --live measurements")
        raise typer.Exit(2)
    if write_budget and tier.lower() == "t0":
        console.print("[red]eval failed:[/red] --write-budget cannot use a T0 validation run")
        raise typer.Exit(2)
    if baseline and harness != "siwah":
        console.print("[red]eval failed:[/red] --baseline compares siwah with naive")
        raise typer.Exit(2)
    runner = EvalRunner(eval_dir=eval_dir)

    async def execute(target_harness: str):
        return await runner.run(
            suite=suite,
            harness=target_harness,
            tier=tier,
            model=model,
            seed=seed,
            runs_per_task=runs,
            max_steps=max_steps,
            limit=limit,
            only=[item.strip() for item in only.split(",")] if only else None,
            use_cache=not no_cache,
            live=live,
            max_allowance=max_allowance,
        )

    try:
        report = asyncio.run(execute(harness))
        naive = asyncio.run(execute("naive")) if baseline else None
    except Exception as exc:
        console.print(f"[red]eval failed:[/red] {exc}")
        raise typer.Exit(2) from exc

    siwah_path = write_report(report, output)
    if naive is not None:
        naive_output = output.with_name(output.stem + "-naive.json") if output is not None else None
        naive_path = write_report(naive, naive_output)
    if write_budget and report.stopped_early:
        console.print("[red]eval failed:[/red] the run stopped at the allowance cap")
        raise typer.Exit(2)
    budget_path = (
        update_budget_config(report.budget_suggestion.model_dump()) if write_budget else None
    )

    if as_json:
        payload: dict[str, object] = {"siwah": report.model_dump(mode="json")}
        if naive is not None:
            payload["naive"] = naive.model_dump(mode="json")
            payload["comparison"] = comparison_summary(report, naive)
        if budget_path is not None:
            payload["budget_config"] = str(budget_path)
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        console.print(render_report(report))
        if report.validation_errors:
            for error in report.validation_errors:
                console.print(f"[red]validation:[/red] {error}")
        if naive is not None:
            console.print(render_comparison(report, naive))
            summary = comparison_summary(report, naive)
            console.print(
                f"delta pass rate: {summary['delta']:+.3f}; siwah higher: {summary['siwah_higher']}"
            )
        console.print(f"report: {siwah_path}")
        if naive is not None:
            console.print(f"baseline report: {naive_path}")
        if budget_path is not None:
            console.print(f"budget config: {budget_path}")

    if report.validation_errors:
        raise typer.Exit(1)
    if report.metrics.passed < report.metrics.tasks:
        raise typer.Exit(1)


@app.command()
def doctor() -> None:
    """Check local configuration and runtime readiness."""
    ensure_home()
    problems = 0
    console.print(f"siwah {__version__}")
    console.print(f"home: {siwah_home()}")
    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        config = Config()
        problems += 1
    console.print(f"config: {config_path()} ({'exists' if config_path().exists() else 'missing'})")
    kernel_status = verify_manifest(repo_root())
    console.print(f"kernel: {kernel_status.summary()}")
    if kernel_status.present and not kernel_status.ok:
        problems += 1
    console.print(
        f"budget: {config.budget.max_allowance_per_cycle} per cycle, "
        f"{config.budget.max_cycles_per_day} cycles/day, "
        f"reserve {config.budget.reserve_weekly_pct}%"
    )
    console.print(
        f"evolution: auto={config.evolution.auto}, "
        f"min_sessions={config.evolution.min_sessions}, "
        f"cooldown={config.evolution.cooldown_minutes}m"
    )
    console.print(f"auth: {auth_path()} ({'exists' if auth_path().exists() else 'missing'})")
    ensure_harness()
    console.print(f"harness: {harness_dir()} (skills={len(list_skills())})")
    project_harness = Path.cwd() / ".siwah"
    if project_harness.is_dir():
        console.print(
            f"project harness: {project_harness} "
            f"(skills={len(list((project_harness / 'skills').glob('*/SKILL.md')))}, "
            f"subagents={len(list((project_harness / 'subagents').glob('*.md')))}, "
            f"commands={len(list((project_harness / 'commands').glob('*.md')))})"
        )
    if not config.providers:
        console.print("[yellow]no providers configured[/yellow]")
    store = AuthStore()
    for name, provider in config.providers.items():
        env_name = provider.auth.env
        env_set = bool(env_name and os.environ.get(env_name))
        console.print(
            f"provider {name}: {provider.api}, credential="
            f"{'env' if env_set else ('file' if store.configured(name) else 'missing')}"
        )
    try:
        store_db = Store()
        report = inspect_home(store_db)
        store_db.close()
        console.print(f"state db: ok ({state_path()})")
        console.print(
            f"artifacts: checkpoints={len(report.checkpoint_dirs)} "
            f"({len(report.checkpoint_orphans)} orphaned), "
            f"trajectories={len(report.trajectory_files)} "
            f"({len(report.trajectory_orphans)} orphaned), "
            f"tool_outputs={len(report.tool_output_dirs)}"
        )
        if report.orphan_count:
            console.print(
                "[yellow]orphaned session artifacts found; run "
                "`siwah clean --sessions` (or --dry-run first).[/yellow]"
            )
        if report.log_bytes:
            console.print(f"logs: daemon.log {report.log_bytes / 1024:.1f} KiB")
        if report.tui_binary:
            staged = "yes" if report.tui_staged else "no"
            console.print(f"tui: {report.tui_binary} (staged update: {staged})")
        if report.harness_dirty:
            console.print(
                f"harness git: {len(report.harness_dirty)} changed path(s) "
                f"({', '.join(report.harness_dirty[:3])}"
                f"{', ...' if len(report.harness_dirty) > 3 else ''})"
            )
        console.print(f"mcp: {report.mcp_servers} server(s) configured")
    except Exception as exc:
        console.print(f"[red]state db error:[/red] {exc}")
        problems += 1
    console.print(f"bun: {shutil.which('bun') or 'not found (TUI source build)'}")
    if config.roles.main:
        console.print(f"main model: {config.roles.main}")
    else:
        console.print(
            "[yellow]main model not configured for headless runs; the TUI "
            "chooses a model per session (use --model or \\[roles].main for "
            "`siwah run`).[/yellow]"
        )
    if problems:
        raise typer.Exit(1)


@app.command("clean")
def clean(
    sessions: bool = typer.Option(
        True, "--sessions/--no-sessions", help="Remove orphaned session artifacts."
    ),
    logs: bool = typer.Option(False, "--logs", help="Rotate daemon.log when it is large."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Remove stale local artifacts under ~/.siwah."""
    store = Store()
    try:
        removed: list[Path] = []
        if sessions:
            report = inspect_home(store)
            count = report.orphan_count
            if (
                count
                and not dry_run
                and not yes
                and not typer.confirm(f"Remove {count} orphaned session artifact(s)?")
            ):
                raise typer.Exit(1)
            removed = prune_orphans(store, dry_run=dry_run)
            for path in removed[:50]:
                typer.echo(f"{'would remove' if dry_run else 'removed'} {path}")
            if len(removed) > 50:
                typer.echo(f"... and {len(removed) - 50} more")
            if not removed:
                typer.echo("No orphaned session artifacts.")
        if logs:
            rotated = rotate_daemon_log(dry_run=dry_run)
            if rotated:
                typer.echo(f"{'would rotate' if dry_run else 'rotated'} {rotated}")
            else:
                typer.echo("daemon.log is missing or below the rotation threshold.")
    finally:
        store.close()


@app.command("update")
def update(
    apply: bool = typer.Option(
        False, "--apply", help="Fast-forward the checkout and rebuild the TUI."
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore the cached version check."),
    json_output: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Check GitHub for a newer siwah version, or install it."""
    if apply:
        result = apply_update()
        if json_output:
            typer.echo(json.dumps(asdict(result), indent=2))
        else:
            console.print(("✓ " if result.ok else "✗ ") + result.detail)
            for step in result.steps:
                console.print(f"  · {step}")
            if result.ok and result.restart_required:
                console.print("Restart siwah to use the new version.")
        if not result.ok:
            raise typer.Exit(1)
        return
    status = check_for_update(refresh=refresh)
    if json_output:
        typer.echo(json.dumps(asdict(status), indent=2))
        return
    if status.error:
        console.print(f"[yellow]update check:[/yellow] {status.error}")
    if status.latest is None:
        console.print(f"siwah {status.current}; no remote version found")
        return
    if status.available:
        console.print(f"siwah {status.current} → {status.latest} available")
        if status.can_apply:
            console.print("Run `siwah update --apply` or `/update` in the TUI.")
        else:
            console.print(status.detail or "Reinstall from a git checkout to update.")
    else:
        console.print(f"siwah {status.current} is up to date (latest {status.latest}).")


@app.command("openapi")
def openapi() -> None:
    """Print the OpenAPI schema used to generate TypeScript types."""
    from ..server.app import create_app

    schema = create_app(Config(), runtime=None).openapi()
    typer.echo(json.dumps(schema, indent=2, sort_keys=True))


def _repo_tui_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "ui" / "tui"


def _tui_binary_name() -> str:
    return "siwah-tui.exe" if os.name == "nt" else "siwah-tui"


def _find_tui_binary() -> Path | None:
    candidates: list[Path] = []
    explicit = os.environ.get("SIWAH_TUI_BINARY")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    home = Path(os.environ.get("SIWAH_HOME", Path.home() / ".siwah")).expanduser()
    candidates.append(home / "bin" / _tui_binary_name())
    candidates.append(Path.home() / ".siwah" / "bin" / _tui_binary_name())
    candidates.append(Path(sys.executable).parent / _tui_binary_name())
    found = shutil.which("siwah-tui")
    if found:
        candidates.append(Path(found))
    candidates.append(_repo_tui_dir() / "dist" / _tui_binary_name())
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _find_bun() -> str | None:
    for candidate in (
        shutil.which("bun"),
        str(Path.home() / ".bun" / "bin" / "bun"),
        str(Path.home() / ".bun" / "bin" / "bun.exe"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    for local in (
        Path.cwd() / ".tools" / "bun" / "bun.exe",
        Path(__file__).resolve().parents[3] / ".tools" / "bun" / "bun.exe",
    ):
        if local.is_file():
            return str(local)
    return None


def _ensure_daemon() -> tuple[str, str, subprocess.Popen[str] | None]:
    endpoint = runtime_endpoint()
    if endpoint is not None and runtime_healthy(endpoint):
        return endpoint.server, endpoint.token, None

    typer.echo("Starting siwah daemon…")
    process = subprocess.Popen(
        [sys.executable, "-m", "siwah", "supervise"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        text=True,
    )
    for _ in range(300):
        if process.poll() is not None:
            break
        endpoint = runtime_endpoint()
        if endpoint is not None and runtime_healthy(endpoint):
            return endpoint.server, endpoint.token, process
        time.sleep(0.1)
    process.terminate()
    _stop_process_tree(process)
    raise RuntimeError("siwah daemon did not become healthy")


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
    else:
        with contextlib.suppress(OSError):
            process.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        process.wait(timeout=5)


def _terminate_runtime_daemon() -> None:
    data = read_runtime()
    if data is None:
        return
    pid = data.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        return
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    with contextlib.suppress(OSError):
        runtime_path().unlink()


def _promote_tui_binary() -> None:
    """Activate a staged TUI build once the previous TUI is no longer running."""

    name = _tui_binary_name()
    home = Path(os.environ.get("SIWAH_HOME", Path.home() / ".siwah")).expanduser()
    staged = home / "bin" / f"{name}.next"
    target = home / "bin" / name
    if not staged.is_file():
        return
    try:
        staged.replace(target)
    except OSError:
        # Another TUI instance may still hold the executable on Windows.
        return


def launch_tui(*, allow_all: bool = False) -> None:
    if allow_all:
        os.environ["SIWAH_PERMISSION_MODE"] = "yolo"
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo(
            'The interactive TUI needs a terminal. Use `siwah run "<prompt>" --ui plain` '
            "in non-interactive environments.",
            err=True,
        )
        raise typer.Exit(1)
    tui_dir = _repo_tui_dir()
    source = tui_dir / "src" / "index.tsx"
    daemon: subprocess.Popen[str] | None = None
    completed: subprocess.CompletedProcess[bytes] | None = None
    try:
        while True:
            _promote_tui_binary()
            binary = _find_tui_binary()
            if binary is not None:
                command = [str(binary)]
                workdir = str(Path.cwd())
            else:
                bun = _find_bun()
                if bun is None or not source.is_file():
                    typer.echo(
                        "The TUI needs a compiled siwah-tui binary (or Bun with the TUI source). "
                        "Put it in ~/.siwah/bin/, next to the siwah executable, or set "
                        "SIWAH_TUI_BINARY. Use `siwah run ... --ui plain` when no TUI is available.",
                        err=True,
                    )
                    raise typer.Exit(1)
                command = [bun, "--conditions=browser", str(source)]
                workdir = str(tui_dir)
            try:
                server, token, daemon = _ensure_daemon()
            except RuntimeError as exc:
                typer.echo(f"Could not start the daemon: {exc}", err=True)
                raise typer.Exit(1) from exc
            env = os.environ.copy()
            env["SIWAH_SERVER"] = server
            env["SIWAH_TOKEN"] = token
            env.setdefault("SIWAH_WORKSPACE", str(Path.cwd()))
            completed = subprocess.run(command, cwd=workdir, env=env)
            if completed.returncode != 75:
                break
            typer.echo("Restarting siwah…")
            if daemon is not None:
                _stop_process_tree(daemon)
                daemon = None
            _terminate_runtime_daemon()
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    finally:
        if daemon is not None:
            _stop_process_tree(daemon)
            _terminate_runtime_daemon()
    raise typer.Exit(completed.returncode if completed is not None else 1)


def _resolve_model_ref(
    config: Config, value: str | None, provider: str | None, model_id: str | None
) -> ModelRef:
    if value:
        return ModelRef.parse(value)
    if model_id:
        if provider:
            return ModelRef(provider=provider, model=model_id)
        main = config.roles.main
        if main:
            return ModelRef.parse(main).model_copy(update={"model": model_id})
        raise ConfigError("Pass --provider with --model")
    if config.roles.main:
        return ModelRef.parse(config.roles.main)
    raise ConfigError("Pass a provider/model-id or configure [roles].main")


if __name__ == "__main__":
    app()


__all__ = ["app", "run", "serve"]
