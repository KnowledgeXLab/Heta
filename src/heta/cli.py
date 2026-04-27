"""Top-level Heta CLI."""

from __future__ import annotations

import sys
import time

import click

from heta.client import (
    HetaCliError,
    HetaClient,
    TERMINAL_TASK_STATUSES,
    build_dataset_name,
    collect_insert_files,
    load_cli_settings,
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
