"""`aigw` command line: serve, migrate, bootstrap, worker, verify-restore."""

from __future__ import annotations

import asyncio
import os
import pathlib
from decimal import Decimal

import typer

app = typer.Typer(help="Independent AI Gateway", no_args_is_help=True)


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8080, role: str | None = None, workers: int = 1):
    """Run the gateway/admin HTTP server (role from AIGW_ROLE unless --role is given)."""
    import uvicorn

    if role:
        os.environ["AIGW_ROLE"] = role
    uvicorn.run("aigw.main:get_app", host=host, port=port, factory=True, workers=workers, proxy_headers=True)


@app.command()
def worker():
    """Run the background worker (outbox, reconciliation, expiry)."""
    from aigw.worker.main import main

    asyncio.run(main())


@app.command()
def migrate(revision: str = "head"):
    """Apply database migrations."""
    from alembic import command
    from alembic.config import Config

    root = pathlib.Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(root / "migrations"))
    command.upgrade(cfg, revision)


@app.command()
def bootstrap(file: pathlib.Path = typer.Argument(..., exists=True), print_key: bool = True):
    """Create org/team/project/models/deployments/prices/budgets/key from a YAML file (deploy/compose/bootstrap.yaml)."""
    import yaml

    from aigw.bootstrap import apply

    spec = yaml.safe_load(file.read_text())
    result = asyncio.run(apply(spec))
    for k, v in result.items():
        if k == "key" and not print_key:
            continue
        typer.echo(f"{k}: {v}")


@app.command("verify-restore")
def verify_restore():
    """Consistency checks after a restore: budgets' spent equals settled usage; no orphan reservations."""
    from aigw.bootstrap import verify

    problems = asyncio.run(verify())
    if problems:
        for p in problems:
            typer.echo(f"PROBLEM: {p}")
        raise typer.Exit(code=1)
    typer.echo("restore verified: ledger consistent")


@app.command()
def price(provider: str, provider_model: str, input_per_million: str, output_per_million: str, source: str = "manual"):
    """Insert a new price version for a provider model (amounts per million tokens, e.g. 0.20)."""
    from aigw.bootstrap import add_price

    typer.echo(
        asyncio.run(add_price(provider, provider_model, Decimal(input_per_million), Decimal(output_per_million), source))
    )


if __name__ == "__main__":
    app()
