"""Top-level Heta CLI."""

from __future__ import annotations

import sys
import time
import warnings

import click
from urllib3.exceptions import InsecureRequestWarning

from heta.client import (
    AnswerJudge,
    HetaCliError,
    HetaClient,
    TERMINAL_TASK_STATUSES,
    build_dataset_name,
    collect_insert_files,
    load_judge_config,
    load_cli_settings,
    related_memories,
)


def _client_from_context(ctx: click.Context) -> HetaClient:
    return HetaClient(ctx.obj["settings"])


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--base-url",
    default=None,
    help="Unified Heta API base URL. Defaults to HETA_BASE_URL or config.yaml.",
)
@click.pass_context
def cli(ctx: click.Context, base_url: str | None) -> None:
    """Heta command line interface."""
    ctx.ensure_object(dict)
    ctx.obj["settings"] = load_cli_settings(base_url=base_url)
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


@cli.command("serve")
@click.option("--host", default=None, help="Bind host for the unified Heta API.")
@click.option("--port", type=int, default=None, help="Bind port for the unified Heta API.")
@click.option("--reload/--no-reload", default=None, help="Enable or disable uvicorn reload.")
@click.option("--log-level", default=None, help="Uvicorn log level.")
def serve(host: str | None, port: int | None, reload: bool | None, log_level: str | None) -> None:
    """Start the unified Heta service."""
    from heta.app import run_server

    run_server(host=host, port=port, reload=reload, log_level=log_level)


@cli.command("insert")
@click.argument("targets", nargs=-1)
@click.option("--kb", required=True, help="Target knowledge base.")
@click.option("-b", "--background", is_flag=True, help="Start parsing and return immediately.")
@click.pass_context
def insert(ctx: click.Context, targets: tuple[str, ...], kb: str, background: bool) -> None:
    """Upload files to HetaDB and parse them into a knowledge base."""
    files = collect_insert_files(targets)
    dataset = build_dataset_name(kb)

    with _client_from_context(ctx) as client:
        client.check_health()
        click.echo(f"KB: {kb}")
        click.echo(f"Dataset: {dataset}")
        click.echo(f"Files: {len(files)}")
        click.echo()

        client.ensure_knowledge_base(kb)
        client.create_dataset(dataset)

        click.echo("Uploading:")
        for file_path in files:
            client.upload_file(dataset, file_path)
            click.echo(f"  OK {file_path.name}")

        parse_payload = client.parse_dataset(kb, dataset)
        tasks = parse_payload.get("data", {}).get("tasks", [])
        if not tasks:
            raise HetaCliError("Parse request returned no task.")
        task_id = tasks[0]["task_id"]

        if background:
            click.echo()
            click.echo("Parse started in background.")
            click.echo(f"Task: {task_id}")
            click.echo()
            click.echo("Next:")
            click.echo("  heta status")
            return

        click.echo()
        click.echo("Parsing:")
        final_task = _wait_for_task(client, task_id)
        status = str(final_task.get("status", "")).lower()
        progress = _normalize_progress(final_task.get("progress", 0))

        if status == "completed":
            _render_progress(progress, "Done", final=True)
            click.echo()
            click.echo("Done.")
            click.echo("Next:")
            click.echo(f'  heta query "..." --kb {kb}')
            return

        if status == "failed":
            _render_progress(progress, "Failed", final=True)
            click.echo()
            click.echo("Error:")
            click.echo(f"  {final_task.get('error') or 'Processing failed'}")
            raise click.exceptions.Exit(1)

        _render_progress(progress, "Cancelled", final=True)
        raise click.exceptions.Exit(1)


@cli.command("query")
@click.argument("question")
@click.option("--kb", default=None, help="Uploaded knowledge base to search when needed.")
@click.option(
    "--from",
    "source",
    type=click.Choice(["auto", "memory", "kb"]),
    default="auto",
    show_default=True,
    help="Query source: auto, memory, or kb.",
)
@click.pass_context
def query(ctx: click.Context, question: str, kb: str | None, source: str) -> None:
    """Query memory and uploaded knowledge bases."""
    if source == "kb" and not kb:
        raise HetaCliError("--from kb requires --kb <name>.")

    warnings.filterwarnings("ignore", category=InsecureRequestWarning)

    with _client_from_context(ctx) as client:
        client.check_health()
        judge = AnswerJudge(load_judge_config())

        if source == "memory":
            result = _query_memory(client, judge, question)
        elif source == "kb":
            result = _query_kb(client, judge, question, kb or "")
        else:
            result = _query_auto(client, judge, question, kb)

    _render_query_result(result)


@cli.command("remember")
@click.argument("text")
@click.option("--long-term", is_flag=True, help="Store in MemoryKB instead of fast MemoryVG.")
@click.pass_context
def remember(ctx: click.Context, text: str, long_term: bool) -> None:
    """Store a fact in Heta memory."""
    text = text.strip()
    if not text:
        raise HetaCliError("Memory text must not be empty.")

    with _client_from_context(ctx) as client:
        client.check_health()
        if long_term:
            payload = client.add_memory_kb(text)
            click.echo("Saved to MemoryKB.")
            click.echo("Indexing will run in the background and may take a few minutes.")
            if payload.get("id"):
                click.echo(f"Task: {payload['id']}")
        else:
            payload = client.add_memory_vg(text)
            click.echo("Saved to MemoryVG.")
            _render_saved_memories(payload)

    click.echo()
    click.echo("Next:")
    click.echo(f'  heta query "{_query_hint(text)}" --from memory')


@cli.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show the current Heta service status."""
    probe_timeout = 3.0

    with _client_from_context(ctx) as client:
        health = _probe("Heta API", lambda: client.check_health(timeout=probe_timeout))
        if not health["ok"]:
            _render_unavailable_status(client.base_url, health["error"])
            return

        kb_probe = _probe(
            "HetaDB",
            lambda: client.list_knowledge_bases(timeout=probe_timeout),
        )
        task_probe = _probe(
            "Processing tasks",
            lambda: client.list_processing_tasks(limit=5, timeout=probe_timeout),
        )
        vg_probe = _probe(
            "MemoryVG",
            lambda: client.list_memory_vg(limit=1, timeout=probe_timeout),
        )

    _render_status(
        base_url=ctx.obj["settings"].base_url,
        health=health["data"],
        kb_probe=kb_probe,
        task_probe=task_probe,
        vg_probe=vg_probe,
    )


def _probe(name: str, fn) -> dict:
    try:
        return {"name": name, "ok": True, "data": fn(), "error": None}
    except HetaCliError as exc:
        return {"name": name, "ok": False, "data": None, "error": str(exc)}


def _render_status(
    *,
    base_url: str,
    health: dict,
    kb_probe: dict,
    task_probe: dict,
    vg_probe: dict,
) -> None:
    if _use_rich_output():
        _render_status_rich(
            base_url=base_url,
            health=health,
            kb_probe=kb_probe,
            task_probe=task_probe,
            vg_probe=vg_probe,
        )
        return

    mode = health.get("mode")
    click.echo("Heta: running")
    click.echo(f"API: {base_url}")
    if mode:
        click.echo(f"Mode: {mode}")

    click.echo()
    click.echo("Modules:")
    click.echo(f"  HetaDB: {_probe_label(kb_probe)}")
    click.echo(f"  MemoryVG: {_probe_label(vg_probe)}")
    click.echo("  MemoryKB: not probed (long-running module)")

    _render_status_kbs(kb_probe)
    _render_status_tasks(task_probe)

    click.echo()
    click.echo("Next:")
    click.echo("  heta insert ./docs --kb research")
    click.echo('  heta query "..." --kb research')
    click.echo('  heta remember "..."')


def _render_unavailable_status(base_url: str, error: str) -> None:
    if _use_rich_output():
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        console = Console()
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Heta", Text("unavailable", style="red"))
        table.add_row("API", base_url)

        body = Group(
            table,
            Text(""),
            Text("Error", style="bold"),
            Text(f"  {error}", style="red"),
            Text(""),
            Text("Next", style="bold"),
            Text("  heta serve"),
            Text("  ./scripts/bootstrap.sh"),
        )
        console.print(Panel(body, title="Heta Status", border_style="red", expand=True))
        return

    click.echo("Heta: unavailable")
    click.echo(f"API: {base_url}")
    click.echo()
    click.echo("Error:")
    click.echo(f"  {error}")
    click.echo()
    click.echo("Next:")
    click.echo("  heta serve")
    click.echo("  ./scripts/bootstrap.sh")


def _render_status_rich(
    *,
    base_url: str,
    health: dict,
    kb_probe: dict,
    task_probe: dict,
    vg_probe: dict,
) -> None:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold cyan", no_wrap=True)
    summary.add_column()
    summary.add_row("Heta", Text("running", style="green"))
    summary.add_row("API", base_url)
    if health.get("mode"):
        summary.add_row("Mode", str(health["mode"]))

    modules = Table.grid(padding=(0, 2))
    modules.add_column(style="bold")
    modules.add_column()
    modules.add_row("HetaDB", _rich_probe_label(kb_probe))
    modules.add_row("MemoryVG", _rich_probe_label(vg_probe))
    modules.add_row("MemoryKB", Text("not probed", style="yellow"))

    body = [
        summary,
        Text(""),
        Text("Modules", style="bold"),
        modules,
        Text(""),
        Text("Knowledge Bases", style="bold"),
        _rich_kb_table(kb_probe),
        Text(""),
        Text("Recent Tasks", style="bold"),
        _rich_task_table(task_probe),
        Text(""),
        Text("Next", style="bold"),
        Text("  heta insert ./docs --kb research"),
        Text('  heta query "..." --kb research'),
        Text('  heta remember "..."'),
    ]

    console.print(Panel(Group(*body), title="Heta Status", border_style="cyan", expand=True))


def _rich_probe_label(probe: dict):
    from rich.text import Text

    if probe["ok"]:
        return Text("ok", style="green")
    return Text("unavailable", style="red")


def _rich_kb_table(probe: dict):
    from rich.table import Table
    from rich.text import Text

    if not probe["ok"]:
        return Text(f"  unavailable: {probe['error']}", style="red")

    kbs = probe["data"] or []
    if not kbs:
        return Text("  none", style="dim")

    table = Table.grid()
    table.add_column()
    for kb in kbs[:10]:
        name = kb.get("name") or kb.get("id") or "unknown"
        table.add_row(f"  {name}")
    if len(kbs) > 10:
        table.add_row(f"  ... {len(kbs) - 10} more")
    return table


def _rich_task_table(probe: dict):
    from rich.table import Table
    from rich.text import Text

    if not probe["ok"]:
        return Text(f"  unavailable: {probe['error']}", style="red")

    tasks = probe["data"] or []
    if not tasks:
        return Text("  none", style="dim")

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_column(style="dim")
    for task in tasks[:5]:
        status_value = str(task.get("status") or "unknown")
        task_id = str(task.get("task_id") or "unknown")
        message = str(task.get("message") or task.get("error") or "").strip()
        table.add_row(_rich_task_status(status_value), task_id, message)
    return table


def _rich_task_status(status_value: str):
    from rich.text import Text

    styles = {
        "completed": "green",
        "running": "cyan",
        "pending": "yellow",
        "failed": "red",
        "cancelled": "dim",
        "cancelling": "yellow",
    }
    return Text(status_value, style=styles.get(status_value.lower(), "white"))


def _use_rich_output() -> bool:
    return sys.stdout.isatty()


def _probe_label(probe: dict) -> str:
    if probe["ok"]:
        return "ok"
    return "unavailable"


def _render_status_kbs(probe: dict) -> None:
    click.echo()
    click.echo("Knowledge Bases:")
    if not probe["ok"]:
        click.echo(f"  unavailable: {probe['error']}")
        return

    kbs = probe["data"] or []
    if not kbs:
        click.echo("  none")
        return

    for kb in kbs[:10]:
        name = kb.get("name") or kb.get("id") or "unknown"
        click.echo(f"  - {name}")
    if len(kbs) > 10:
        click.echo(f"  ... {len(kbs) - 10} more")


def _render_status_tasks(probe: dict) -> None:
    click.echo()
    click.echo("Recent Tasks:")
    if not probe["ok"]:
        click.echo(f"  unavailable: {probe['error']}")
        return

    tasks = probe["data"] or []
    if not tasks:
        click.echo("  none")
        return

    for task in tasks[:5]:
        status_value = str(task.get("status") or "unknown")
        task_id = str(task.get("task_id") or "unknown")
        message = str(task.get("message") or task.get("error") or "").strip()
        suffix = f" - {message}" if message else ""
        click.echo(f"  - {status_value} {task_id}{suffix}")


def _query_auto(client: HetaClient, judge: AnswerJudge, question: str, kb: str | None) -> dict:
    path: list[str] = []
    memories = _safe_search_memory_vg(client, question)
    path.append("MemoryVG")

    if memories and _judge_accepts(judge, question, str(memories[0].get("memory") or "")):
        return _answer_result(memories[0]["memory"], "MemoryVG", path)

    memory_kb = _safe_query_memory_kb(client, question)
    path.append("MemoryKB")
    memory_kb_answer = str(memory_kb.get("final_answer") or "")
    if _judge_accepts(judge, question, memory_kb_answer):
        return _answer_result(
            memory_kb_answer,
            "MemoryKB",
            path,
            memories=related_memories(memories),
        )

    if kb:
        hetadb = _safe_query_hetadb(client, question, kb)
        path.append("HetaDB")
        hetadb_answer = str(hetadb.get("response") or "")
        if _judge_accepts(judge, question, hetadb_answer):
            return _answer_result(
                hetadb_answer,
                "HetaDB",
                path,
                citations=hetadb.get("citations") or [],
                memories=related_memories(memories),
            )

    tip = "Pass --kb <name> to search uploaded documents." if not kb else f"No answer found in knowledge base: {kb}."
    return _not_found_result(path, memories=related_memories(memories), tip=tip)


def _query_memory(client: HetaClient, judge: AnswerJudge, question: str) -> dict:
    path: list[str] = []
    memories = _safe_search_memory_vg(client, question)
    path.append("MemoryVG")

    if memories and _judge_accepts(judge, question, str(memories[0].get("memory") or "")):
        return _answer_result(memories[0]["memory"], "MemoryVG", path)

    memory_kb = _safe_query_memory_kb(client, question)
    path.append("MemoryKB")
    memory_kb_answer = str(memory_kb.get("final_answer") or "")
    if _judge_accepts(judge, question, memory_kb_answer):
        return _answer_result(
            memory_kb_answer,
            "MemoryKB",
            path,
            memories=related_memories(memories),
        )

    return _not_found_result(path, memories=related_memories(memories), tip="No memory found.")


def _query_kb(client: HetaClient, judge: AnswerJudge, question: str, kb: str) -> dict:
    hetadb = client.query_hetadb(question, kb)
    hetadb_answer = str(hetadb.get("response") or "")
    if _judge_accepts(judge, question, hetadb_answer):
        return _answer_result(
            hetadb_answer,
            "HetaDB",
            ["HetaDB"],
            citations=hetadb.get("citations") or [],
        )
    return _not_found_result(["HetaDB"], tip=f"No answer found in knowledge base: {kb}.")


def _render_saved_memories(payload: dict) -> None:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return
    click.echo()
    click.echo("Memory:")
    for item in results[:5]:
        memory = str(item.get("memory") or "").strip()
        event = str(item.get("event") or "").strip()
        if memory and event:
            click.echo(f"  - {event}: {memory}")
        elif memory:
            click.echo(f"  - {memory}")


def _query_hint(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 40 else text[:37] + "..."


def _safe_search_memory_vg(client: HetaClient, question: str) -> list[dict]:
    try:
        return client.search_memory_vg(question, limit=5)
    except HetaCliError:
        return []


def _safe_query_memory_kb(client: HetaClient, question: str) -> dict:
    try:
        return client.query_memory_kb(question)
    except HetaCliError:
        return {}


def _safe_query_hetadb(client: HetaClient, question: str, kb: str) -> dict:
    try:
        return client.query_hetadb(question, kb)
    except HetaCliError:
        return {}


def _judge_accepts(judge: AnswerJudge, question: str, answer: str) -> bool:
    try:
        return judge.accepts(question, answer)
    except HetaCliError:
        return False


def _answer_result(
    answer: str,
    source: str,
    path: list[str],
    *,
    citations: list[dict] | None = None,
    memories: list[dict] | None = None,
) -> dict:
    return {
        "answer": answer,
        "source": source,
        "path": path,
        "citations": citations or [],
        "memories": memories or [],
        "tip": None,
    }


def _not_found_result(
    path: list[str],
    *,
    memories: list[dict] | None = None,
    tip: str | None = None,
) -> dict:
    return {
        "answer": None,
        "source": None,
        "path": path,
        "citations": [],
        "memories": memories or [],
        "tip": tip,
    }


def _render_query_result(result: dict) -> None:
    answer = result.get("answer")
    if answer:
        click.echo("Answer:")
        click.echo(answer)
        click.echo()
        click.echo(f"Source: {result['source']}")
        click.echo(f"Path: {' -> '.join(result['path'])}")

        _render_citations(result.get("citations") or [])
        _render_related_memories(result.get("memories") or [])
        return

    click.echo("No answer found.")
    click.echo()
    click.echo(f"Path: {' -> '.join(result['path'])}")
    _render_related_memories(result.get("memories") or [])
    if result.get("tip"):
        click.echo()
        click.echo("Tip:")
        click.echo(f"  {result['tip']}")


def _render_citations(citations: list[dict]) -> None:
    if not citations:
        return
    click.echo()
    click.echo("Citations:")
    for index, citation in enumerate(citations[:5], start=1):
        name = citation.get("source_file") or citation.get("dataset") or "unknown"
        click.echo(f"  {index}. {name}")


def _render_related_memories(memories: list[dict]) -> None:
    if not memories:
        return
    click.echo()
    click.echo("Related Memory:")
    for memory in memories[:3]:
        text = str(memory.get("memory") or "").strip()
        if text:
            click.echo(f"  - {text}")


def _wait_for_task(client: HetaClient, task_id: str) -> dict:
    while True:
        task = client.get_processing_task(task_id)
        status = str(task.get("status", "")).lower()
        progress = _normalize_progress(task.get("progress", 0))

        if status in TERMINAL_TASK_STATUSES:
            return task

        _render_progress(progress, "Processing...")
        time.sleep(client.settings.poll_interval_seconds)


def _normalize_progress(value: object) -> float:
    if isinstance(value, (int, float)):
        progress = float(value)
    else:
        progress = 0.0
    if progress > 1:
        progress = progress / 100
    return max(0.0, min(progress, 1.0))


def _render_progress(progress: float, label: str, *, final: bool = False) -> None:
    width = 20
    filled = int(progress * width)
    bar = "█" * filled + "░" * (width - filled)
    percent = int(progress * 100)
    end = "\n" if final else "\r"
    click.echo(f"  [{bar}] {percent}% {label}", nl=False)
    sys.stdout.write(end)
    sys.stdout.flush()


def main() -> None:
    """Console entry point."""
    try:
        cli(obj={}, standalone_mode=False)
    except HetaCliError as exc:
        click_exc = click.ClickException(str(exc))
        click_exc.show()
        raise SystemExit(click_exc.exit_code) from exc
