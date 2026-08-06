"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config.loader import ConfigError, default_config_path, load_config, write_example_config
from .config.schema import Config

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aih",
        description="A coding agent harness that runs on any OpenAI-compatible API.",
    )
    parser.add_argument("-C", "--cwd", default=".", help="workspace directory")
    parser.add_argument("--config", help="path to an extra config file, merged last")
    parser.add_argument("-m", "--model", help="model, or model@account, for this run")
    parser.add_argument("-p", "--print", dest="prompt", help="run one prompt headlessly and exit")
    parser.add_argument("-r", "--resume", help="resume a session by id")
    parser.add_argument(
        "--mode", choices=("ask", "auto", "yolo"), help="override the permission mode"
    )
    parser.add_argument("--zh", action="store_true", help="use Chinese labels where applicable")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="write a starter config file")
    sub.add_parser("doctor", help="check configuration and API credentials")
    sub.add_parser("sessions", help="list saved sessions")
    sub.add_parser("jobs", help="list scheduled jobs")
    sub.add_parser("daemon", help="run only the scheduler, with no UI")

    gui = sub.add_parser("gui", help="open the desktop window (default)")
    gui.add_argument("--serve", action="store_true",
                     help="only run the local server, and print its URL")
    gui.add_argument("--debug", action="store_true", help="open developer tools")

    sub.add_parser("tui", help="use the terminal interface instead")

    run_job = sub.add_parser("run-job", help="run one scheduled job immediately")
    run_job.add_argument("job_id")

    delete = sub.add_parser("delete", help="delete sessions")
    delete.add_argument("target", help="a session id, or 'all'")

    return parser


def _load(args: argparse.Namespace) -> tuple[Config, Path]:
    workspace = Path(args.cwd).expanduser().resolve()
    if not workspace.is_dir():
        raise ConfigError(f"workspace does not exist: {workspace}")
    config = load_config(workspace, Path(args.config) if args.config else None)
    if args.mode:
        config.permissions.mode = args.mode
    return config, workspace


def _require_models(config: Config) -> None:
    """Refuse to run headless work with nothing configured.

    The interactive UI does not call this: it starts empty on purpose and
    walks the user through ``/setup``. Only the non-interactive entry points
    need to fail fast, because there is nobody there to configure anything.

    Raises:
      ConfigError: When no model is usable.
    """
    if config.models and config.roles:
        return
    raise ConfigError(
        "No models are configured, and this harness never guesses at your\n"
        "environment or ships defaults.\n\n"
        "Start it interactively and run /setup:\n\n"
        "    aih\n\n"
        f"Or edit {default_config_path()} by hand — `aih init` writes an\n"
        "annotated empty template there."
    )


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    try:
        path = write_example_config()
    except ConfigError as error:
        print(f"{error}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"Wrote {path}")
    print("Edit it, export your API keys, then run `aih doctor`.")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from .providers.openai_compat import probe_account

    config, _ = _load(args)
    problems = config.validate()
    if problems:
        print("Configuration problems:")
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("Configuration looks consistent.")

    async def probe_all() -> list[tuple[str, bool, str]]:
        results = []
        for account in config.accounts:
            if not account.enabled:
                results.append((account.id, False, "disabled"))
                continue
            ok, detail = await probe_account(account)
            results.append((account.id, ok, detail))
        return results

    print("\nAccounts:")
    for account_id, ok, detail in asyncio.run(probe_all()):
        print(f"  [{'ok' if ok else '--'}] {account_id}: {detail}")
    return EXIT_OK if not problems else EXIT_CONFIG


def cmd_sessions(args: argparse.Namespace) -> int:
    from .session.store import SessionStore

    _, workspace = _load(args)
    entries = SessionStore().list(workspace=workspace)
    if not entries:
        print("No saved sessions for this workspace.")
        return EXIT_OK
    for meta in entries:
        print(
            f"{meta.id}  {meta.updated_label}  {meta.message_count:>4} msg  "
            f"${meta.total_cost:.4f}  {meta.title or '(untitled)'}"
        )
    return EXIT_OK


def cmd_delete(args: argparse.Namespace) -> int:
    from .session.store import SessionStore

    _, workspace = _load(args)
    store = SessionStore()
    if args.target == "all":
        entries = store.list(workspace=workspace)
        if not entries:
            print("Nothing to delete.")
            return EXIT_OK
        answer = input(f"Delete all {len(entries)} session(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("Cancelled.")
            return EXIT_OK
        print(f"Deleted {store.delete_all(workspace=workspace)} session(s).")
        return EXIT_OK
    if store.delete(args.target):
        print(f"Deleted {args.target}.")
        return EXIT_OK
    print(f"No session {args.target}.", file=sys.stderr)
    return EXIT_ERROR


def cmd_jobs(args: argparse.Namespace) -> int:
    from .scheduler.jobs import JobStore

    store = JobStore()
    jobs = store.all()
    if not jobs:
        print("No scheduled jobs. Add one from the UI with /job add.")
        return EXIT_OK
    for job in jobs:
        state = "on " if job.enabled else "off"
        print(
            f"{job.id}  [{state}]  {job.describe():<28}  next {job.next_run_label()}  {job.name}"
        )
    return EXIT_OK


def cmd_run_job(args: argparse.Namespace) -> int:
    from .providers.router import Router
    from .scheduler.jobs import JobStore
    from .scheduler.runner import Scheduler

    config, _ = _load(args)
    _require_models(config)
    store = JobStore()
    job = store.get(args.job_id)
    if job is None:
        print(f"No job {args.job_id}.", file=sys.stderr)
        return EXIT_ERROR

    async def go() -> int:
        router = Router(config)
        scheduler = Scheduler(config, router, store)
        try:
            record = await scheduler.run_now(job.id)
        finally:
            await router.aclose()
        if record is None:
            return EXIT_ERROR
        print(f"{'ok' if record.ok else 'failed'} in {record.duration:.0f}s, ${record.cost:.4f}")
        if record.error:
            print(record.error, file=sys.stderr)
        if record.summary:
            print("\n" + record.summary)
        return EXIT_OK if record.ok else EXIT_ERROR

    return asyncio.run(go())


def cmd_daemon(args: argparse.Namespace) -> int:
    from .providers.router import Router
    from .scheduler.jobs import JobStore
    from .scheduler.runner import Scheduler

    config, _ = _load(args)
    _require_models(config)
    store = JobStore()

    async def go() -> int:
        router = Router(config)
        scheduler = Scheduler(
            config,
            router,
            store,
            on_run=lambda job, record: print(
                f"[{record.started_label}] {job.name}: "
                f"{'ok' if record.ok else 'failed'} ({record.duration:.0f}s, ${record.cost:.4f})"
                + (f" — {record.error}" if record.error else "")
            ),
        )
        scheduler.start()
        print(f"Scheduler running with {len(store.all())} job(s). Ctrl-C to stop.")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await scheduler.stop()
            await router.aclose()
        return EXIT_OK

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_headless(args: argparse.Namespace) -> int:
    """Run a single prompt and print the result, with no TUI."""
    from .agent.loop import Agent, Done, Notice, Text, ToolEnd, ToolStart
    from .permissions import PermissionEngine
    from .providers.router import NoRouteError, Router, Selection
    from .session.store import SessionStore
    from .skills import SkillLibrary
    from .toolset import build_registry

    config, workspace = _load(args)
    _require_models(config)

    async def go() -> int:
        router = Router(config)
        permissions = PermissionEngine(config.permissions, workspace)
        sessions = SessionStore()
        session = (
            sessions.open(args.resume)
            if args.resume
            else sessions.create(workspace)
        )
        if session is None:
            print(f"No session {args.resume}.", file=sys.stderr)
            return EXIT_ERROR

        agent = Agent(
            config,
            router,
            build_registry(),
            permissions,
            workspace,
            skills=SkillLibrary(workspace, config.skill_paths).load(),
            session=session,
        )
        if args.model:
            try:
                agent.set_selection(Selection.parse(args.model, config))
            except NoRouteError as error:
                print(str(error), file=sys.stderr)
                return EXIT_CONFIG

        exit_code = EXIT_OK
        try:
            async for event in agent.run(args.prompt):
                if isinstance(event, Text):
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                elif isinstance(event, ToolStart):
                    print(f"\n[{event.name}]", file=sys.stderr)
                elif isinstance(event, ToolEnd):
                    if event.result.summary:
                        print(f"  {event.result.summary}", file=sys.stderr)
                elif isinstance(event, Notice):
                    print(f"  {event.text}", file=sys.stderr)
                    if event.level == "error":
                        exit_code = EXIT_ERROR
                elif isinstance(event, Done):
                    print()
        finally:
            await router.aclose()
        return exit_code

    return asyncio.run(go())


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui.app import HarnessApp

    config, workspace = _load(args)
    # No _require_models here on purpose: the UI opens with nothing configured
    # and walks the user through /setup, which is the whole point of not
    # shipping defaults.
    app = HarnessApp(config, workspace, session_id=args.resume, chinese=args.zh)
    if args.model:
        from .providers.router import NoRouteError, Selection

        try:
            app.agent.set_selection(Selection.parse(args.model, config))
        except NoRouteError as error:
            print(str(error), file=sys.stderr)
            return EXIT_CONFIG
    app.run()
    return EXIT_OK


def cmd_gui(args: argparse.Namespace) -> int:
    """Open the desktop window, or just serve the UI for a browser."""
    config, workspace = _load(args)
    if getattr(args, "serve", False):
        from .gui.server import serve_forever

        asyncio.run(serve_forever(config, workspace))
        return EXIT_OK

    from .gui.desktop import launch

    return launch(config, workspace, debug=getattr(args, "debug", False))


DISPATCH = {
    "init": cmd_init,
    "gui": cmd_gui,
    "tui": cmd_ui,
    "doctor": cmd_doctor,
    "sessions": cmd_sessions,
    "delete": cmd_delete,
    "jobs": cmd_jobs,
    "run-job": cmd_run_job,
    "daemon": cmd_daemon,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in DISPATCH:
            return DISPATCH[args.command](args)
        if args.prompt:
            return cmd_headless(args)
        # No subcommand means the desktop window. The terminal UI is still
        # there under `aih tui` for people who prefer it or are on a server.
        return cmd_gui(args)
    except ConfigError as error:
        print(f"{error}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
