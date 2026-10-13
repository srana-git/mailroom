"""The `mailroom` command line.

index    build the vector index over the playbook
triage   read one letter and queue the next move
trace    print the audit trail of one run
queue    list pending, approved and rejected items
approve  approve a pending item (simulated effect, recorded)
reject   reject a pending item
eval     run the golden set and write evals/results and docs/EVALS.md
models   ask the API which models the key can use
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from .agent import Triager
from .audit import read_run, summarize_run
from .config import DEFAULT_EVAL_SET, EVAL_CONFIGS, EVAL_SETS, Settings, load_api_key
from .evaluation import report
from .evaluation.harness import EvalHarness, load_results, write_results
from .evaluation.rescore import RescoreError, rescore_result
from .fake import FakeBackend
from .indexer import PlaybookIndex
from .llm import BackendError
from .modeldocs import render_models_doc
from .models import PendingAction, TriageResult, TriageStatus
from .queue import ActionQueue, QueueError
from .registry import load_registry
from .retrieval import Retriever, dedupe_by_playbook

app = typer.Typer(
    add_completion=False,
    help=(
        "Bilingual official-mail triage. Reads a letter, finds the procedure, "
        "queues the next move."
    ),
)

MODELS_DOC_PATH = Path("docs/MODELS.md")


@dataclass
class Context:
    settings: Settings


def _context(ctx: typer.Context) -> Context:
    if not isinstance(ctx.obj, Context):
        raise typer.Exit(code=2)
    return ctx.obj


@app.callback()
def main_callback(
    ctx: typer.Context,
    backend: str = typer.Option(
        None,
        "--backend",
        help="'cohere' (default) or 'fake' for an offline run with no API key.",
    ),
    root: Path = typer.Option(None, "--root", help="Repository root. Defaults to the cwd."),
    state_dir: Path = typer.Option(
        None,
        "--state-dir",
        help="Where the index, the queue and the runs live. Defaults to <root>/.mailroom.",
    ),
) -> None:
    overrides: dict[str, object] = {}
    if state_dir is not None:
        overrides = {
            "state_dir": state_dir,
            "index_dir": state_dir / "index",
            "queue_path": state_dir / "queue.jsonl",
            "runs_dir": state_dir / "runs",
        }
    ctx.obj = Context(settings=Settings.build(root=root, backend=backend, **overrides))


# -- helpers ---------------------------------------------------------------


def _build_backend(settings: Settings) -> Any:
    if settings.is_fake:
        return FakeBackend()
    key = load_api_key()
    if not key:
        typer.echo(
            "No Cohere API key. Set COHERE_API_KEY, or store one with "
            "`pass insert apis/cohere`, or run with --backend fake for an offline demo.",
            err=True,
        )
        raise typer.Exit(code=2)
    import cohere

    return _cohere_backend(cohere.ClientV2(api_key=key), settings)


def _cohere_backend(client: Any, settings: Settings) -> Any:
    from .llm import CohereBackend

    return CohereBackend(client, settings)


def _read_letter(path: Path) -> tuple[str, str]:
    """Return (letter text, source label). Accepts a golden case or a plain letter."""
    if not path.exists():
        typer.echo(f"No such file: {path}", err=True)
        raise typer.Exit(code=2)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw, path.name
        if isinstance(payload, dict) and isinstance(payload.get("letter_text"), str):
            return payload["letter_text"], str(payload.get("id", path.name))
    return raw, path.name


def _triage_result_text(result: TriageResult) -> str:
    lines = [
        f"run id:  {result.run_id}",
        f"source:  {result.source or '-'}",
        f"status:  {result.status.value}",
        f"reason:  {result.reason}",
    ]
    if result.plan:
        lines.append("plan:")
        lines.extend(f"  - {step}" for step in result.plan)
    fields = result.fields
    if fields is None:
        lines.append("fields:  none")
    else:
        lines.extend(
            [
                f"issuer:  {fields.issuer}",
                f"type:    {fields.letter_type.value}   language: {fields.language.value}",
                f"ref:     {fields.reference_number or '-'}",
                "amount:  "
                + (
                    f"{fields.amount_due:.2f} {fields.currency or ''}".strip()
                    if fields.amount_due is not None
                    else "-"
                ),
                f"due:     {fields.due_date.isoformat() if fields.due_date else '-'}",
                f"action:  {fields.required_action.value}   urgency: {fields.urgency}",
                f"suspicious: {fields.is_suspicious}"
                + (f" ({fields.suspicion_reason})" if fields.suspicion_reason else ""),
            ]
        )
    if result.injection_flags:
        lines.append("injection wording: " + "; ".join(result.injection_flags))
    if result.retrieval:
        lines.append("procedures:")
        for hit in dedupe_by_playbook(result.retrieval):
            lines.append(f"  - {hit.playbook_id} {hit.title} ({hit.stage}, {hit.score:.4f})")
    if result.actions:
        lines.append("queued:")
        for action in result.actions:
            mark = "BLOCKED" if action.blocked else "pending"
            detail = "; ".join(action.guard_reasons)
            lines.append(f"  - {action.id} {action.kind.value} [{mark}] {detail}")
    else:
        lines.append("queued:  nothing")
    lines.append(f"steps:   {result.steps}   latency: {result.latency_ms} ms")
    return "\n".join(lines)


def _action_line(action: PendingAction) -> str:
    mark = "BLOCKED" if action.blocked else action.status
    return (
        f"{action.id}  {action.kind.value:9s} {mark:9s} run={action.run_id}  "
        f"created={action.created_at.isoformat(timespec='seconds')}"
    )


# -- commands --------------------------------------------------------------


@app.command()
def index(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Rebuild even when an index exists."),
) -> None:
    """Embed the playbook and write the LanceDB index."""
    settings = _context(ctx).settings
    backend = _build_backend(settings)
    playbook_index = PlaybookIndex(settings, backend)
    existing = playbook_index.read_meta()
    if existing and not force:
        typer.echo(
            f"index already built: {existing['chunks']} chunks from "
            f"{existing['procedures']} procedures, {existing['embed_model']}, "
            f"{existing['dimension']} dimensions, {existing['built_at']}"
        )
        typer.echo("pass --force to rebuild")
        raise typer.Exit(code=0)
    meta = playbook_index.build(force=force)
    typer.echo(
        f"indexed {meta['chunks']} chunks from {meta['procedures']} procedures with "
        f"{meta['embed_model']} ({meta['dimension']} dimensions) into {settings.index_dir}"
    )


@app.command()
def triage(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="A letter (.txt) or a golden case (.json)."),
    text_only: bool = typer.Option(
        False, "--text-only", help="Print the plain-text report instead of JSON."
    ),
) -> None:
    """Read one letter, find the procedure, queue the next move."""
    context = _context(ctx)
    settings = context.settings
    settings.ensure_state_dirs()
    letter_text, source = _read_letter(path)
    backend = _build_backend(settings)
    retriever = Retriever(settings, backend, index=PlaybookIndex(settings, backend))
    triager = Triager(
        backend=backend,
        retriever=retriever,
        registry=load_registry(settings.registry_path),
        queue=ActionQueue(settings.queue_path),
        settings=settings,
    )
    result = triager.run(letter_text, source=source)
    if text_only:
        typer.echo(_triage_result_text(result))
    else:
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        typer.echo("")
        typer.echo(_triage_result_text(result))
    if result.status is TriageStatus.ERROR:
        raise typer.Exit(code=1)


@app.command()
def trace(
    ctx: typer.Context, run_id: str = typer.Argument(..., help="A run id from a triage.")
) -> None:
    """Pretty-print the audit trail of one run."""
    settings = _context(ctx).settings
    events = read_run(run_id, settings.runs_dir)
    if not events:
        typer.echo(f"No run {run_id} under {settings.runs_dir}", err=True)
        raise typer.Exit(code=2)
    summary = summarize_run(events)
    typer.echo(f"run {summary['run_id']}")
    for key in (
        "status",
        "reason",
        "events",
        "model_calls",
        "model_failures",
        "retries",
        "tool_calls",
        "tokens_in",
        "tokens_out",
        "model_latency_ms",
        "retrieval_latency_ms",
        "latency_ms",
    ):
        typer.echo(f"  {key:15s} {summary.get(key)}")
    typer.echo("")
    for event in events:
        typer.echo(_format_event(event))


def _format_event(event: dict[str, Any]) -> str:
    stamp = str(event.get("timestamp", ""))[11:23]
    kind = event.get("kind")
    if kind == "model_call":
        return (
            f"  {stamp} model_call  {event.get('operation', 'chat')} {event.get('model')} "
            f"attempt={event.get('attempt')} "
            f"{event.get('status')} {event.get('latency_ms')}ms "
            f"in={event.get('tokens_in')} out={event.get('tokens_out')}"
            + (f" error={event.get('error')}" if event.get("error") else "")
        )
    if kind == "tool_call":
        guard = event.get("guard")
        guard_text = ""
        if guard:
            guard_text = " guard=allow" if guard.get("allowed") else " guard=block"
        return (
            f"  {stamp} tool_call   step={event.get('step')} {event.get('tool')} "
            f"{event.get('latency_ms')}ms{guard_text}"
            + (f" error={event.get('error')}" if event.get("error") else "")
        )
    if kind == "note":
        return f"  {stamp} note        {event.get('message')}"
    if kind in ("run_start", "run_end"):
        payload = {k: v for k, v in event.items() if k not in ("timestamp", "kind", "run_id")}
        return f"  {stamp} {kind:11s} {json.dumps(payload, ensure_ascii=False)}"
    return f"  {stamp} {kind}"


@app.command()
def queue(
    ctx: typer.Context,
    all_items: bool = typer.Option(False, "--all", help="Include approved and rejected items."),
) -> None:
    """List the pending actions waiting for a human."""
    settings = _context(ctx).settings
    store = ActionQueue(settings.queue_path)
    items = store.all_items() if all_items else store.pending()
    if not items:
        typer.echo("nothing pending" if not all_items else "the queue is empty")
        return
    for item in items:
        typer.echo(_action_line(item))
        typer.echo(f"    payload {json.dumps(item.payload, ensure_ascii=False)}")


@app.command()
def approve(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="A pending id from `mailroom queue`."),
    note: str = typer.Option(None, "--note", help="Why it was approved."),
) -> None:
    """Approve a pending item. The effect is simulated and recorded, nothing is sent."""
    _decide(_context(ctx).settings, item_id, approve_it=True, note=note)


@app.command()
def reject(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="A pending id from `mailroom queue`."),
    note: str = typer.Option(None, "--note", help="Why it was rejected."),
) -> None:
    """Reject a pending item."""
    _decide(_context(ctx).settings, item_id, approve_it=False, note=note)


def _decide(settings: Settings, item_id: str, *, approve_it: bool, note: str | None) -> None:
    store = ActionQueue(settings.queue_path)
    try:
        item = store.approve(item_id, note) if approve_it else store.reject(item_id, note)
    except QueueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if approve_it:
        typer.echo(
            f"approved {item.id} ({item.kind.value}). Effect simulated: nothing was sent or paid."
        )
    else:
        typer.echo(f"rejected {item.id} ({item.kind.value}). Nothing was sent or paid.")


@app.command()
def eval(  # noqa: A001 - the command is named eval on purpose
    ctx: typer.Context,
    config: str = typer.Option(None, "--config", help="A, B or C. Default: all three, in order."),
    case_set: str = typer.Option(
        DEFAULT_EVAL_SET,
        "--set",
        help="Which letters to score: 'golden' (default) or 'holdout'.",
    ),
    limit: int = typer.Option(None, "--limit", help="Score only the first N golden letters."),
    rate_limit: int = typer.Option(
        None, "--rate-limit", help="Requests per minute ceiling. Default: 15."
    ),
    render_only: bool = typer.Option(
        False,
        "--render-only",
        help="Rewrite docs/EVALS.md from existing result files. No API calls.",
    ),
    rescore: Path = typer.Option(
        None,
        "--rescore",
        help=(
            "Re-score an existing result file under the current gold and scoring "
            "rules, and write <name>-rescored.json beside it. No API calls."
        ),
    ),
) -> None:
    """Run the golden set and write evals/results plus docs/EVALS.md."""
    settings = _context(ctx).settings
    if rescore is not None:
        try:
            path = rescore_result(settings, rescore)
        except RescoreError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        typer.echo(f"re-scored {rescore} -> {path}")
        return

    if render_only:
        payloads = load_results(settings)
        path = report.write_evals_doc(settings.root, payloads)
        typer.echo(f"wrote {path} from {len(payloads)} result file(s)")
        return

    if case_set not in EVAL_SETS:
        typer.echo(
            f"Unknown letter set {case_set}. Use one of: {', '.join(EVAL_SETS)}.", err=True
        )
        raise typer.Exit(code=2)

    names = [config.upper()] if config else list(EVAL_CONFIGS)
    unknown = [name for name in names if name not in EVAL_CONFIGS]
    if unknown:
        typer.echo(f"Unknown config {', '.join(unknown)}. Use A, B or C.", err=True)
        raise typer.Exit(code=2)

    effective = settings
    if rate_limit is not None:
        effective = Settings.build(
            root=settings.root, backend=settings.backend, requests_per_minute=rate_limit
        )
    backend = _build_backend(effective)

    written: list[Path] = []
    for name in names:
        chosen = EVAL_CONFIGS[name]
        typer.echo(f"config {name}: {chosen.describe()} (set {case_set})")
        try:
            harness = EvalHarness(
                settings=effective,
                backend=backend,
                config=chosen,
                case_set=case_set,
                progress=lambda message: typer.echo(f"  {message}"),
            )
            payload = harness.run(limit=limit)
        except (RuntimeError, BackendError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        path = write_results(effective, payload)
        written.append(path)
        typer.echo(f"  wrote {path}")

    payloads = load_results(effective)
    doc = report.write_evals_doc(effective.root, payloads)
    typer.echo(f"wrote {doc} from {len(payloads)} result file(s)")


@app.command()
def models(
    ctx: typer.Context,
    write_docs: bool = typer.Option(False, "--write-docs", help="Rewrite docs/MODELS.md."),
) -> None:
    """Ask the API which models the key can use."""
    settings = _context(ctx).settings
    listed: list[dict[str, Any]] | None = None
    if settings.is_fake or not load_api_key():
        typer.echo(
            "No API key, so the models endpoint was not called. "
            "The pinned ids in mailroom/config.py stand unconfirmed."
        )
    else:
        backend = _build_backend(settings)
        try:
            listed = backend.list_models()
        except Exception as exc:  # noqa: BLE001 - a listing failure must not lose the doc
            typer.echo(f"models listing failed: {exc}", err=True)
        if listed:
            for item in listed:
                endpoints = ", ".join(item.get("endpoints") or []) or "-"
                typer.echo(f"{item.get('name'):40s} {endpoints}")
    if write_docs:
        path = settings.root / MODELS_DOC_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_models_doc(listed), encoding="utf-8")
        typer.echo(f"wrote {path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
