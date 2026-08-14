"""``python -m signal_hub tick | status | queue`` (design section 5).

The queue subcommands are not a convenience wrapper -- at MVP they ARE the
consumer.  There is no headless drainer; an aesop session claims, completes and
fails tasks through this CLI, which is why ``claim`` is the rename-as-mutex and
why every subcommand prints something even when there is nothing to say.

Exit codes:
    0  the command did what it says
    1  the command failed (task missing, no status file yet)
    2  usage error
    3  the tick was short-circuited by the ``state/.HALT`` sentinel

A tick that ran but hit source errors still exits 0 -- see ``_cmd_tick``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from signal_hub import queue as queue_mod
from signal_hub import tick

__all__ = ["main", "build_parser", "default_root"]

PROG = "python -m signal_hub"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_HALTED = 3


def default_root() -> Path:
    """Where the hub lives.  ``SIGNAL_HUB_ROOT`` wins, then the repo the
    package was imported from -- so a scheduled task needs no arguments."""
    env = os.environ.get("SIGNAL_HUB_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")

    def with_root(p):
        # SUPPRESS, not None: --root is declared on both a parent and its leaf,
        # and a leaf default of None would blank out a root the parent already
        # parsed, making `queue --root X list` silently target the wrong hub.
        p.add_argument(
            "--root",
            default=argparse.SUPPRESS,
            help="hub root (default: $SIGNAL_HUB_ROOT, else the repo)",
        )
        return p

    tick_p = with_root(sub.add_parser("tick", help="run one orchestrated pass"))
    tick_p.add_argument(
        "--include-preseason",
        action="store_true",
        help="capture PRE rows so the pipeline can be exercised out of season "
        "(every MVP rule still filters to REG/POST, so no content task fires)",
    )
    tick_p.add_argument(
        "--dry-run",
        action="store_true",
        help="run every stage and write NOTHING; print what would be enqueued",
    )
    tick_p.add_argument("--rules-dir", default=None, help="rules/ directory override")
    tick_p.add_argument(
        "--offline",
        action="store_true",
        help="refuse every network fetch (sources record ERROR snapshots)",
    )
    tick_p.add_argument("--json", action="store_true", help="print the tick result as JSON")

    status_p = with_root(sub.add_parser("status", help="print state/hub-status.json"))
    status_p.add_argument("--json", action="store_true", help="raw JSON, unformatted")

    queue_p = with_root(sub.add_parser("queue", help="drain the task queue"))
    queue_sub = queue_p.add_subparsers(dest="action")
    # --root is repeated on every leaf: argparse hands everything after the
    # sub-subcommand to the leaf parser, so `queue list --root X` would
    # otherwise be a usage error while `queue --root X list` worked. Both
    # spellings must work -- a consumer types the one they think of first.
    list_p = with_root(queue_sub.add_parser("list", help="list pending tasks, highest priority first"))
    list_p.add_argument("--json", action="store_true")
    claim_p = with_root(queue_sub.add_parser("claim", help="claim a pending task"))
    claim_p.add_argument("task")
    done_p = with_root(queue_sub.add_parser("complete", help="finish a claimed task"))
    done_p.add_argument("task")
    fail_p = with_root(queue_sub.add_parser("fail", help="give a claimed task back as failed"))
    fail_p.add_argument("task")
    fail_p.add_argument("--reason", default="unspecified")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if not args.command:
        parser.print_usage(sys.stderr)
        print("%s: a command is required (tick|status|queue)" % PROG, file=sys.stderr)
        return EXIT_USAGE

    root = Path(args.root) if getattr(args, "root", None) else default_root()
    if args.command == "tick":
        return _cmd_tick(args, root)
    if args.command == "status":
        return _cmd_status(args, root)
    if args.command == "queue":
        return _cmd_queue(args, root, parser)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------

def _cmd_tick(args, root: Path) -> int:
    result = tick.run_tick(
        root,
        http_get=tick.offline_http_get if args.offline else None,
        rules_dir=args.rules_dir,
        include_preseason=args.include_preseason,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(_tick_json(result), indent=2, sort_keys=True))
    else:
        _print_tick(result, args.dry_run)

    # A completed tick exits 0 even when a source was unreachable or a rule file
    # was malformed.  Those are normal operating conditions with their own
    # visible channels -- an ERROR snapshot, a stderr line, an alarm in
    # hub-status.json -- and painting the scheduled task red every five minutes
    # because GitHub blipped is how people learn to ignore a red task.
    # Nonzero here means the tick did not run at all.
    return EXIT_HALTED if result.halted else EXIT_OK


def _print_tick(result, dry_run: bool) -> None:
    if result.halted:
        print("HALT: tick short-circuited by state/.HALT (reason: %s)" % (result.halt_reason or "none"))
        return
    prefix = "DRY-RUN " if dry_run else ""
    captured = ", ".join("%s=%s" % item for item in sorted(result.captured.items())) or "nothing due"
    print("%stick mode=%s rules=%d/%d ms=%d" % (
        prefix, result.mode, result.rules_loaded,
        result.rules_loaded + result.rules_invalid, result.tick_ms,
    ))
    print("%s  captured: %s" % (prefix, captured))
    print("%s  events:   %d derived, %d recorded" % (prefix, len(result.events), len(result.recorded)))
    if dry_run:
        for action in result.actions:
            task = action.get("task") or {}
            print("DRY-RUN   WOULD ENQUEUE %s -> %s (%s) event=%s" % (
                action["rule_id"], task.get("kind", action["kind"]),
                task.get("template") or task.get("target") or "-", action["event_id"],
            ))
        print("DRY-RUN   %d action(s) would fire; 0 written" % len(result.actions))
    else:
        for task_id in result.enqueued:
            print("  ENQUEUED %s" % task_id)
        print("  %d task(s) enqueued" % len(result.enqueued))
    for error in result.errors:
        print("  ERROR %s" % error, file=sys.stderr)


def _tick_json(result) -> dict:
    return {
        "mode": result.mode,
        "dry_run": result.dry_run,
        "halted": result.halted,
        "halt_reason": result.halt_reason,
        "tick_ms": result.tick_ms,
        "captured": result.captured,
        "events": [event.to_dict() for event in result.recorded],
        "actions": result.actions,
        "enqueued": result.enqueued,
        "rules": {"loaded": result.rules_loaded, "invalid": result.rules_invalid},
        "errors": result.errors,
    }


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _cmd_status(args, root: Path) -> int:
    path = root / "state" / "hub-status.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        print(
            "no hub-status.json at %s -- the tick has never completed here" % path,
            file=sys.stderr,
        )
        return EXIT_FAIL
    except ValueError as exc:
        print("hub-status.json at %s is not valid JSON: %s" % (path, exc), file=sys.stderr)
        return EXIT_FAIL

    if args.json:
        print(json.dumps(doc, sort_keys=True))
        return EXIT_OK

    print("signal-hub  mode=%s  last_tick=%s  tick_ms=%s" % (
        doc.get("mode"), doc.get("last_tick"), doc.get("tick_ms"),
    ))
    for name, entry in sorted((doc.get("sources") or {}).items()):
        print("  source %-24s %-9s age=%ss" % (
            name, entry.get("status"), entry.get("age_s", "?"),
        ))
    rules = doc.get("rules") or {}
    print("  rules   loaded=%s invalid=%s fired_today=%s throttled_today=%s" % (
        rules.get("loaded"), rules.get("invalid"),
        rules.get("fired_today"), rules.get("throttled_today"),
    ))
    q = doc.get("queue") or {}
    print("  queue   pending=%s claimed=%s oldest=%ss expired_today=%s" % (
        q.get("pending"), q.get("claimed"),
        q.get("oldest_pending_age_s"), q.get("expired_today"),
    ))
    alarms = doc.get("alarms") or []
    print("  alarms  %s" % (", ".join(alarms) if alarms else "none"))
    return EXIT_OK


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

SUFFIX = ".task.json"


def _filename(task: str) -> str:
    return task if task.endswith(SUFFIX) else task + SUFFIX


def _cmd_queue(args, root: Path, parser) -> int:
    queue_dir = root / "queue"
    action = getattr(args, "action", None)
    if not action:
        parser.print_usage(sys.stderr)
        print("%s queue: an action is required (list|claim|complete|fail)" % PROG, file=sys.stderr)
        return EXIT_USAGE

    if action == "list":
        tasks = queue_mod.list_pending_tasks(queue_dir)
        tasks.sort(key=lambda t: (-int(t.get("priority") or 0), t.get("created_at") or ""))
        if args.json:
            print(json.dumps(tasks, indent=2, sort_keys=True))
            return EXIT_OK
        for task in tasks:
            task_args = task.get("args") or {}
            print("  [%3s] %s  %s  %s  expires=%s" % (
                task.get("priority"), task.get("task_id"), task.get("kind"),
                task_args.get("template") or task_args.get("target") or "-",
                task.get("expires_at"),
            ))
        print("%d pending" % len(tasks))
        return EXIT_OK

    name = _filename(args.task)
    if action == "claim":
        task = queue_mod.claim_task(queue_dir, name)
        if task is None:
            print(
                "cannot claim %s: not in queue/pending (already claimed, expired or "
                "never existed)" % args.task,
                file=sys.stderr,
            )
            return EXIT_FAIL
        print("claimed %s" % task.get("task_id"))
        print(json.dumps(task, indent=2, sort_keys=True))
        return EXIT_OK

    if action == "complete":
        if not queue_mod.complete_task(queue_dir, name):
            print("cannot complete %s: not in queue/claimed" % args.task, file=sys.stderr)
            return EXIT_FAIL
        print("completed %s" % args.task)
        return EXIT_OK

    if action == "fail":
        if not queue_mod.fail_task(queue_dir, name, reason=args.reason):
            print("cannot fail %s: not in queue/claimed" % args.task, file=sys.stderr)
            return EXIT_FAIL
        print("failed %s (%s)" % (args.task, args.reason))
        return EXIT_OK

    parser.print_usage(sys.stderr)
    return EXIT_USAGE
