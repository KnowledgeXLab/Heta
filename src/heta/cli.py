"""Top-level Heta CLI."""

from __future__ import annotations

import click


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Heta command line interface."""
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


def main() -> None:
    """Console entry point."""
    cli(obj={})
