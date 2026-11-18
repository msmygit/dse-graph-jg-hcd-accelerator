"""CLI entry point for the DSE Graph → JanusGraph / HCD Migration Accelerator."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from typing import Any

import click
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn

from dse_jg_migrator.models import ClusterAuth, UserFacingError

console = Console()


def _format_duration(seconds: float) -> str:
    """Compact elapsed time: ``850ms``, ``3.2s``, ``1m 12s``, ``1h 5m 3s``.

    Rounded before the unit is chosen, so 0.9996 s reads ``1.0s`` (not ``1000ms``) and 59.96 s ``1m 0s``.
    """
    if round(seconds * 1000) < 1000:
        return f"{round(seconds * 1000)}ms"
    if round(seconds, 1) < 60:
        return f"{round(seconds, 1):.1f}s"
    minutes, secs = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


def _elapsed(start: float) -> str:
    """Time since ``start`` (a ``time.perf_counter()`` value), formatted."""
    return _format_duration(time.perf_counter() - start)


def _took(start: float) -> str:
    """`` [3.2s]`` suffix for a step's result line; the bracket is escaped so rich does not read it as markup."""
    return f" [dim]\\[{_elapsed(start)}][/dim]"


# Shared by translate, migrate and run-all: migrate writes exactly the keys (and value classes) translate created,
# so both must receive the same values (run-all passes one value of each to both).
udt_strategy_option = click.option(
    "--udt-strategy",
    type=click.Choice(["flat", "json"]),
    default="flat",
    show_default=True,
    help="UDT strategy: flat explodes UDT fields into <udt>_<field> keys; json serialises as one JSON string.",
)
map_strategy_option = click.option(
    "--map-strategy",
    type=click.Choice(["flat", "json"]),
    default="flat",
    show_default=True,
    help="mapOf() strategy: flat produces parallel <prop>_key/<prop>_value LISTs; json one JSON string.",
)
decimal_strategy_option = click.option(
    "--decimal-strategy",
    type=click.Choice(["double", "string"]),
    default="double",
    show_default=True,
    help="Decimal storage: double (numeric; inexact values beyond ~15 digits are counted) or string (exact).",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_risk_banner(risk: str, summary: dict[str, Any]) -> None:  # noqa: ANN401
    """Print a coloured risk banner to the console."""
    colours = {"CRITICAL": "red", "HIGH": "red", "MEDIUM": "yellow", "LOW": "green"}
    colour = colours.get(risk, "white")
    total = summary.get("total_findings", 0)
    by_sev = summary.get("by_severity", {})
    console.print(
        f"\n[bold {colour}]Risk Level: {risk}[/bold {colour}]  "
        f"({total} findings: {by_sev.get('RED', 0)} RED, "
        f"{by_sev.get('YELLOW', 0)} YELLOW, {by_sev.get('GREEN', 0)} GREEN)"
    )


def _load_assessment_risk(json_path: str) -> str:
    """Load overall_risk from a saved assessment JSON file."""
    try:
        with open(json_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return str(data.get("overall_risk", "LOW"))
    except (OSError, json.JSONDecodeError):
        return "LOW"


# Driver exceptions that mean "the cluster is not reachable" (matched by class name so the CLI does not have to import
# aiohttp / cassandra-driver at start-up).
_CONNECTION_ERROR_NAMES = {"ClientConnectionError", "WSServerHandshakeError", "NoHostAvailable", "OperationTimedOut"}
STACK_HINT = "Is the stack running and healthy? Start it with ./docker/up.sh — it waits until every service is healthy."
AUTH_HINT = (
    "The server requires (other) credentials: set DSE_USERNAME / DSE_PASSWORD (or --source-username / "
    "--source-password) for the DSE source, HCD_CQL_USERNAME / HCD_CQL_PASSWORD for HCD."
)
# How gremlin-python (DSE / JanusGraph) and cassandra-driver word a missing or rejected login (seen live).
_AUTH_ERROR_MARKERS = ("requires authentication", "failed to login", "authenticationfailed", "bad credentials")

# Every command that writes files writes them here; `run-all` and the individual commands share one default, so the
# files of one step are where the next step's README example reads them.
DEFAULT_OUTPUT_DIR = "out"
_output_dir_option = click.option(
    "--output-dir", default=DEFAULT_OUTPUT_DIR, show_default=True, help="Directory for reports and intermediate files."
)

# HCD credentials come from the environment (or the flag) — never a built-in default (AGENTS.md §6.2).
_target_cql_username_option = click.option(
    "--target-cql-username", envvar="HCD_CQL_USERNAME", default="cassandra", show_default=True
)
_target_cql_password_option = click.option(
    "--target-cql-password",
    envvar="HCD_CQL_PASSWORD",
    default=None,
    help="Password for --target-cql-username. Defaults to $HCD_CQL_PASSWORD.",
)


# Source DSE credentials and TLS, shared by every command that connects to the source. Credentials come from the
# environment (DSE_USERNAME / DSE_PASSWORD) or the flags; the old --username / --password names still work.
_SOURCE_AUTH_OPTIONS = (
    click.option(
        "--source-username",
        "--username",
        "source_username",
        envvar="DSE_USERNAME",
        default=None,
        help="DSE user for Gremlin and CQL (needs SELECT on the graph keyspace). Defaults to $DSE_USERNAME.",
    ),
    click.option(
        "--source-password",
        "--password",
        "source_password",
        envvar="DSE_PASSWORD",
        default=None,
        help="Password for --source-username. Defaults to $DSE_PASSWORD (prefer it: flags show in shell history).",
    ),
    click.option(
        "--source-cql-ssl", is_flag=True, default=False, help="Use TLS for the source CQL connection (client-to-node)."
    ),
    click.option(
        "--source-ca-cert",
        type=click.Path(exists=True, dir_okay=False),
        default=None,
        help="PEM CA certificate that verifies the source's TLS (CQL with --source-cql-ssl, and wss:// Gremlin). "
        "Without it the system trust store is used.",
    ),
)


def _source_auth_options(func: Callable[..., Any]) -> Callable[..., Any]:
    """Add ``--source-username``, ``--source-password``, ``--source-cql-ssl`` and ``--source-ca-cert``."""
    for option in reversed(_SOURCE_AUTH_OPTIONS):
        func = option(func)
    return func


def _source_auth(
    source_username: str | None,
    source_password: str | None,
    source_cql_ssl: bool,
    source_ca_cert: str | None,
) -> ClusterAuth:
    """Build the source ``ClusterAuth``; a user without a password (or the reverse) is a mistake, not anonymous."""
    if bool(source_username) != bool(source_password):
        raise UserFacingError(
            "Set both the DSE user and password (--source-username / --source-password, or DSE_USERNAME / "
            "DSE_PASSWORD), or neither for a cluster without authentication."
        )
    return ClusterAuth(
        username=source_username, password=source_password, cql_ssl=source_cql_ssl, ca_cert=source_ca_cert
    )


def _require_target_password(target_cql_host: str | None, target_cql_password: str | None) -> str:
    """Return the HCD password, or fail clearly when a target host is given without one."""
    if target_cql_host and not target_cql_password:
        raise UserFacingError(
            "--target-cql-host needs a password: set HCD_CQL_PASSWORD or pass --target-cql-password "
            "(on a fresh local stack: export HCD_CQL_PASSWORD=cassandra)."
        )
    return target_cql_password or ""


def _is_connection_error(exc: BaseException) -> bool:
    """True for a refused / unreachable / timed-out connection from Python, aiohttp or cassandra-driver."""
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return any(cls.__name__ in _CONNECTION_ERROR_NAMES for cls in type(exc).__mro__)


class _CliGroup(click.Group):
    """Turns a connection failure in any sub-command into one clear message and exit 1 (AGENTS.md §4.6).

    Without this, a cluster that is not up yet surfaced as a 60-line aiohttp traceback (found in a fresh-user test).
    """

    def invoke(self, ctx: click.Context) -> Any:  # noqa: ANN401  # click returns the sub-command's arbitrary result
        try:
            return super().invoke(ctx)
        except Exception as exc:
            if _is_connection_error(exc):
                console.print(f"[red]✗[/red] Cannot connect: {exc}")
                is_auth = any(marker in str(exc).lower() for marker in _AUTH_ERROR_MARKERS)
                console.print(f"  {AUTH_HINT if is_auth else STACK_HINT}")
            elif isinstance(exc, UserFacingError):  # e.g. unknown graph, a seed statement DSE rejected
                console.print(f"[red]✗[/red] {exc}")
            else:
                raise
            raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group(cls=_CliGroup)
def main() -> None:
    """DSE Graph → JanusGraph / HCD Migration Accelerator.

    Analyse, translate, initialise, and migrate DSE Graph workloads to
    JanusGraph backed by HCD 2.x (IBM Hyper-Converged Database).

    Use --help on any sub-command for detailed options.
    """


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--source-gremlin-url",
    required=True,
    help="WebSocket URL of the DSE Gremlin server to seed (ws://<host>:8182/gremlin)",
)
@click.option(
    "--template",
    required=True,
    type=click.Choice(["food", "social", "iot", "enterprise"]),
    help="Built-in schema template to use.",
)
@click.option("--graph-name", default=None, help="Name for the created graph. Defaults to the template name.")
@click.option(
    "--vertex-count",
    default=1_000,
    show_default=True,
    help="Number of synthetic vertices to insert per primary vertex label.",
)
@click.option(
    "--edge-count", default=5_000, show_default=True, help="Number of synthetic edges to insert per primary edge label."
)
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Required: confirms you understand this writes data to the DSE cluster.",
)
def seed(
    source_gremlin_url: str,
    template: str,
    graph_name: str | None,
    vertex_count: int,
    edge_count: int,
    confirm: bool,
) -> None:
    """Seed a DSE Graph cluster with a built-in sample workload.

    ⚠ WRITES DATA to the source DSE cluster. For demo and development use only.
    Never run against a production cluster.

    The schema is created with ifNotExists(), but every run appends data — seed under a
    new --graph-name to start over.

    Examples::

      dse-jg-migrator seed --source-gremlin-url ws://localhost:8183/gremlin \\
                           --template food --confirm

      dse-jg-migrator seed --source-gremlin-url ws://localhost:8183/gremlin \\
                           --template enterprise --graph-name demo_enterprise \\
                           --vertex-count 500 --confirm
    """
    if not confirm:
        console.print(
            "[yellow]⚠ This command writes data to the DSE cluster.[/yellow]\n"
            "Add [bold]--confirm[/bold] to proceed, or answer 'y' to continue interactively."
        )
        if not click.confirm("Proceed with seeding?"):
            raise SystemExit(0)

    start = time.perf_counter()  # after the prompt: think-time is not counted
    resolved_name = graph_name or template
    from dse_jg_migrator.seeder import Seeder

    seeder = Seeder(
        gremlin_url=source_gremlin_url,
        template=template,
        graph_name=resolved_name,
        vertex_count=vertex_count,
        edge_count=edge_count,
    )
    with console.status(f"Seeding '{resolved_name}' graph using '{template}' template..."):
        result = seeder.run()

    console.print(f"[green]✓[/green] Graph [bold]{result.graph_name}[/bold] seeded successfully.{_took(start)}")
    requested = f"{result.vertex_count:,} vertices / {result.edge_count:,} edges per primary label"
    if result.total_vertices is not None and result.total_edges is not None:
        console.print(
            f"  Graph now holds {result.total_vertices:,} vertices and {result.total_edges:,} edges ({requested})."
        )
    else:
        console.print(f"  Requested {requested}.")
    console.print("\n[bold]Next step:[/bold]")
    console.print(f"  {result.next_command}")


# ---------------------------------------------------------------------------
# assess
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--source-gremlin-url",
    required=True,
    help="WebSocket URL of the source DSE Gremlin server (ws://<host>:8182/gremlin)",
)
@click.option("--source-cql-host", required=True, help="CQL host of the source DSE cluster.")
@click.option("--source-cql-port", default=9042, show_default=True)
@click.option("--graph-name", required=True)
@_output_dir_option
@click.option(
    "--target-cql-host",
    default=None,
    help="CQL host of the target HCD cluster. When provided, runs HCD pre-flight checks.",
)
@click.option("--target-cql-port", default=9042, show_default=True)
@_target_cql_username_option
@_target_cql_password_option
@click.option(
    "--format",
    "output_formats",
    type=click.Choice(["md", "json", "html", "all"]),
    multiple=True,
    default=["all"],
    show_default=True,
)
@click.option(
    "--filter-category",
    type=click.Choice(
        [
            "query_pattern",
            "schema",
            "index",
            "configuration",
            "olap",
            "security",
            "tinkerpop_delta",
            "data_type",
            "hcd_target",
            "all",
        ]
    ),
    default="all",
    show_default=True,
)
@click.option(
    "--filter-severity",
    type=click.Choice(["RED", "YELLOW", "GREEN", "all"]),
    default="all",
    show_default=True,
)
@_source_auth_options
@click.option(
    "--skip-assessment-gate", is_flag=True, default=False, help="Do not exit non-zero when risk is HIGH or CRITICAL."
)
def assess(
    source_gremlin_url: str,
    source_cql_host: str,
    source_cql_port: int,
    graph_name: str,
    output_dir: str,
    target_cql_host: str | None,
    target_cql_port: int,
    target_cql_username: str,
    target_cql_password: str | None,
    output_formats: tuple[str, ...],
    filter_category: str,
    filter_severity: str,
    source_username: str | None,
    source_password: str | None,
    source_cql_ssl: bool,
    source_ca_cert: str | None,
    skip_assessment_gate: bool,
) -> None:
    """Run a read-only pre-migration compatibility assessment.

    Connects to the source DSE Graph cluster and (optionally) the target HCD 2.x
    cluster to analyse schema, query patterns, indexes, configuration, and HCD
    pre-flight requirements against JanusGraph's capability matrix.

    Produces a structured report in Markdown, JSON, and HTML formats.

    This command is NON-DESTRUCTIVE and can be run repeatedly.

    Examples::

      dse-jg-migrator assess \\
        --source-gremlin-url ws://localhost:8183/gremlin \\
        --source-cql-host localhost --source-cql-port 9043 \\
        --graph-name food

      dse-jg-migrator assess \\
        --source-gremlin-url ws://localhost:8183/gremlin \\
        --source-cql-host localhost --source-cql-port 9043 \\
        --target-cql-host localhost --target-cql-port 9042 \\
        --graph-name food
    """
    start = time.perf_counter()
    from dse_jg_migrator.assessment_reporter import AssessmentReporter
    from dse_jg_migrator.assessor import Assessor

    assessor = Assessor(
        gremlin_url=source_gremlin_url,
        cql_host=source_cql_host,
        cql_port=source_cql_port,
        graph_name=graph_name,
        target_cql_host=target_cql_host,
        target_cql_port=target_cql_port,
        target_cql_username=target_cql_username,
        target_cql_password=_require_target_password(target_cql_host, target_cql_password),
        source_auth=_source_auth(source_username, source_password, source_cql_ssl, source_ca_cert),
    )
    console.print(f"[bold]Assessing graph:[/bold] {graph_name} at {source_gremlin_url}")
    with console.status("Running compatibility assessment (read-only)..."):
        result = assessor.run()

    # Apply filters
    if filter_category != "all":
        result.findings = [f for f in result.findings if f.category == filter_category]
    if filter_severity != "all":
        order: dict[str, int] = {"RED": 3, "YELLOW": 2, "GREEN": 1}
        min_level = order[filter_severity]
        result.findings = [f for f in result.findings if order[f.severity] >= min_level]

    reporter = AssessmentReporter(result, output_dir=output_dir)
    paths = reporter.write_all()

    _print_risk_banner(result.overall_risk, result.summary)
    took = _took(start)  # once, on the last report line
    for i, (fmt, path) in enumerate(paths.items(), 1):
        console.print(f"  [green]✓[/green] {fmt:8s} → {path}{took if i == len(paths) else ''}")

    if result.overall_risk in ("HIGH", "CRITICAL") and not skip_assessment_gate:
        console.print(
            "[red]Assessment gate FAILED.[/red] Resolve RED findings before running migration. "
            "Pass --skip-assessment-gate to override."
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# analyse
# ---------------------------------------------------------------------------


@main.command()
@click.option("--source-gremlin-url", required=True)
@click.option("--source-cql-host", required=True)
@click.option("--source-cql-port", default=9042, show_default=True)
@click.option("--graph-name", required=True)
@_output_dir_option
@_source_auth_options
def analyse(
    source_gremlin_url: str,
    source_cql_host: str,
    source_cql_port: int,
    graph_name: str,
    output_dir: str,
    source_username: str | None,
    source_password: str | None,
    source_cql_ssl: bool,
    source_ca_cert: str | None,
) -> None:
    """Analyse DSE Graph cluster and extract schema to JSON."""
    start = time.perf_counter()
    from dse_jg_migrator.analyser import Analyser

    analyser = Analyser(
        gremlin_url=source_gremlin_url,
        cql_host=source_cql_host,
        cql_port=source_cql_port,
        graph_name=graph_name,
        source_auth=_source_auth(source_username, source_password, source_cql_ssl, source_ca_cert),
        output_dir=output_dir,
    )
    console.print(f"[bold]Analysing graph:[/bold] {graph_name}")
    with console.status("Extracting schema (read-only)..."):
        result = analyser.run()

    out_file = os.path.join(output_dir, f"{graph_name}-analysis.json")
    console.print(f"[green]✓[/green] Analysis written → {out_file}{_took(start)}")
    console.print(
        f"  {len(result.vertex_labels)} vertex labels, "
        f"{len(result.edge_labels)} edge labels, "
        f"{len(result.indexes)} indexes"
    )


# ---------------------------------------------------------------------------
# translate
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--analysis",
    "analysis_path",
    required=True,
    type=click.Path(exists=True),
    help="Path to <graph-name>-analysis.json produced by the analyse command.",
)
@udt_strategy_option
@map_strategy_option
@decimal_strategy_option
@_output_dir_option
def translate(
    analysis_path: str,
    udt_strategy: str,
    map_strategy: str,
    decimal_strategy: str,
    output_dir: str,
) -> None:
    """Translate DSE Graph schema to JanusGraph JSON schema."""
    start = time.perf_counter()
    from dse_jg_migrator.translator import Translator

    tr = Translator(
        analysis=None,  # type: ignore[arg-type]
        udt_strategy=udt_strategy,
        map_strategy=map_strategy,
        output_dir=output_dir,
        decimal_strategy=decimal_strategy,
    )
    analysis = tr.load_analysis(analysis_path)
    tr.analysis = analysis

    console.print(f"[bold]Translating schema:[/bold] {analysis.graph_name}")
    with console.status("Translating (no network calls)..."):
        result = tr.run()

    schema_file = os.path.join(output_dir, f"{analysis.graph_name}-janusgraph-schema.json")
    console.print(f"[green]✓[/green] Schema written → {schema_file}{_took(start)}")
    console.print(
        f"  {len(result.propertyKeys)} property keys, "
        f"{len(result.vertexLabels)} vertex labels, "
        f"{len(result.compositeIndexes)} composite indexes, "
        f"{len(result.mixedIndexes)} mixed indexes"
    )


# ---------------------------------------------------------------------------
# init-target
# ---------------------------------------------------------------------------


@main.command("init-target")
@click.option("--target-gremlin-url", required=True, help="WebSocket URL of the target JanusGraph Gremlin server.")
@click.option(
    "--schema",
    "schema_path",
    required=True,
    type=click.Path(exists=True),
    help="Path to <graph>-janusgraph-schema.json from the translate command.",
)
@click.option(
    "--mode",
    type=click.Choice(["gremlin-script", "server-restart"]),
    default="gremlin-script",
)
@click.option("--username", default=None)
@click.option("--password", default=None, hide_input=True)
@click.option(
    "--index-timeout",
    "index_timeout_min",
    type=click.IntRange(min=0),
    default=30,
    show_default=True,
    help="Minutes to wait for composite indexes to be enabled before proceeding.",
)
def init_target(
    target_gremlin_url: str,
    schema_path: str,
    mode: str,
    username: str | None,
    password: str | None,
    index_timeout_min: int,
) -> None:
    """Apply JanusGraph schema to the target HCD/JanusGraph cluster."""
    start = time.perf_counter()
    from dse_jg_migrator.initialiser import Initialiser

    init = Initialiser(
        target_gremlin_url=target_gremlin_url,
        schema_json_path=schema_path,
        mode=mode,
        username=username,
        password=password,
        index_timeout_s=index_timeout_min * 60,
    )
    console.print(f"[bold]Initialising target:[/bold] {target_gremlin_url}")
    with console.status("Applying schema to target cluster..."):
        result = init.run()

    console.print(f"  Created: {len(result.get('created', []))} elements")
    if result.get("errors"):
        # Exit non-zero: migrating into an incomplete schema (schema.default=none) fails every batch (found live).
        console.print("[red]✗[/red] Schema initialisation failed:")
        for err in result["errors"]:
            console.print(f"  [red]✗[/red] {err}")
        raise SystemExit(1)
    console.print(f"[green]✓[/green] Schema initialised successfully.{_took(start)}")


# ---------------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--analysis",
    "analysis_path",
    required=True,
    type=click.Path(exists=True),
)
@click.option("--source-gremlin-url", required=True)
@click.option("--target-gremlin-url", required=True)
@click.option("--source-cql-host", default="")
@click.option("--source-cql-port", default=9042, show_default=True)
@_source_auth_options
@click.option("--cql-fetch-size", default=1000, show_default=True)
@click.option("--gremlin-batch", default=200, show_default=True)
@click.option("--write-batch", default=100, show_default=True)
@click.option("--parallel-labels", default=1, show_default=True)
@udt_strategy_option
@map_strategy_option
@decimal_strategy_option
@_output_dir_option
def migrate(
    analysis_path: str,
    source_gremlin_url: str,
    target_gremlin_url: str,
    source_cql_host: str,
    source_cql_port: int,
    source_username: str | None,
    source_password: str | None,
    source_cql_ssl: bool,
    source_ca_cert: str | None,
    cql_fetch_size: int,
    gremlin_batch: int,
    write_batch: int,
    parallel_labels: int,
    udt_strategy: str,
    map_strategy: str,
    decimal_strategy: str,
    output_dir: str,
) -> None:
    """Migrate vertex and edge data from DSE Graph to JanusGraph.

    Exits non-zero if any batch still failed after retries; re-running resumes and retries only the elements
    that are not yet migrated.
    """
    start = time.perf_counter()
    from dse_jg_migrator.migrator import Migrator
    from dse_jg_migrator.translator import Translator

    tr = Translator(analysis=None, output_dir=output_dir)  # type: ignore[arg-type]
    analysis = tr.load_analysis(analysis_path)

    migrator = Migrator(
        analysis=analysis,
        source_gremlin_url=source_gremlin_url,
        target_gremlin_url=target_gremlin_url,
        source_cql_host=source_cql_host,
        source_cql_port=source_cql_port,
        source_auth=_source_auth(source_username, source_password, source_cql_ssl, source_ca_cert),
        cql_fetch_size=cql_fetch_size,
        gremlin_batch=gremlin_batch,
        write_batch=write_batch,
        parallel_labels=parallel_labels,
        output_dir=output_dir,
        decimal_strategy=decimal_strategy,
        udt_strategy=udt_strategy,
        map_strategy=map_strategy,
    )

    console.print(f"[bold]Migrating graph:[/bold] {analysis.graph_name}")
    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        BarColumn(),
        TaskProgressColumn(),
    ) as progress:
        task = progress.add_task("Migrating...", total=None)
        stats = migrator.run()
        progress.update(task, completed=1, total=1)

    total_v = sum(stats.get("vertices", {}).values())
    total_e = sum(stats.get("edges", {}).values())
    if stats.get("already_migrated"):
        console.print(f"  Resumed: {stats['already_migrated']:,} element(s) already migrated were skipped")
    for key, count in stats.get("lossy_decimals", {}).items():
        console.print(f"[yellow]⚠[/yellow] {count:,} '{key}' Decimal value(s) are inexact as Double")
    for label, count in stats.get("missing_endpoints", {}).items():
        console.print(f"[yellow]⚠[/yellow] {count:,} '{label}' edge(s) skipped: endpoint vertex not migrated")
    if stats.get("errors"):
        console.print(
            f"[red]✗[/red] Migration incomplete: {total_v:,} vertices, {total_e:,} edges written{_took(start)}"
        )
        for err in stats["errors"]:
            console.print(f"  [red]✗[/red] {err}")
        console.print("  Re-run migrate to retry; already-migrated elements are skipped.")
        raise SystemExit(1)
    console.print(f"[green]✓[/green] Migration complete: {total_v:,} vertices, {total_e:,} edges{_took(start)}")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@main.command()
@click.option("--analysis", "analysis_path", required=True, type=click.Path(exists=True))
@click.option("--source-gremlin-url", required=True)
@click.option("--target-gremlin-url", required=True)
@click.option("--validation-queries", "queries_path", default=None, type=click.Path())
@_output_dir_option
@_source_auth_options
def validate(
    analysis_path: str,
    source_gremlin_url: str,
    target_gremlin_url: str,
    queries_path: str | None,
    output_dir: str,
    source_username: str | None,
    source_password: str | None,
    source_cql_ssl: bool,
    source_ca_cert: str | None,
) -> None:
    """Compare source and target vertex/edge counts and run smoke tests."""
    start = time.perf_counter()
    from dse_jg_migrator.translator import Translator
    from dse_jg_migrator.validator import Validator

    tr = Translator(analysis=None, output_dir=output_dir)  # type: ignore[arg-type]
    analysis = tr.load_analysis(analysis_path)

    validator = Validator(
        analysis=analysis,
        source_gremlin_url=source_gremlin_url,
        target_gremlin_url=target_gremlin_url,
        source_auth=_source_auth(source_username, source_password, source_cql_ssl, source_ca_cert),
        validation_queries_path=queries_path,
        output_dir=output_dir,
    )

    console.print(f"[bold]Validating graph:[/bold] {analysis.graph_name}")
    with console.status("Running validation checks..."):
        report = validator.run()

    passes = sum(1 for r in report["count_results"] if r["match"])
    total = len(report["count_results"])
    status_icon = "[green]✓[/green]" if report["overall_pass"] else "[red]✗[/red]"
    console.print(f"{status_icon} Validation: {passes}/{total} count checks passed{_took(start)}")
    if report.get("reason"):
        console.print(f"  [red]✗[/red] {report['reason']}")
    if not report["overall_pass"]:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@main.command()
@click.option("--graph-name", required=True)
@_output_dir_option
def report(graph_name: str, output_dir: str, durations: dict[str, str] | None = None) -> None:
    """Generate the final migration summary report.

    ``durations`` is not a CLI option: ``run-all`` passes its per-phase times so the report records them.
    """
    start = time.perf_counter()
    from dse_jg_migrator.reporter import Reporter

    r = Reporter(graph_name=graph_name, output_dir=output_dir)
    path = r.generate(durations)
    console.print(f"[green]✓[/green] Report written → {path}{_took(start)}")


# ---------------------------------------------------------------------------
# run-all
# ---------------------------------------------------------------------------


@main.command("run-all")
@click.option("--source-gremlin-url", required=True)
@click.option("--source-cql-host", required=True)
@click.option("--source-cql-port", default=9042, show_default=True)
@click.option("--graph-name", required=True)
@click.option("--target-gremlin-url", required=True)
@click.option("--target-cql-host", default=None)
@click.option("--target-cql-port", default=9042, show_default=True)
@_target_cql_username_option
@_target_cql_password_option
@_output_dir_option
@click.option("--analyze-only", is_flag=True, default=False, help="Run only assessment and analysis phases, then exit.")
@click.option(
    "--skip-assessment-gate", is_flag=True, default=False, help="Proceed even if assessment risk is HIGH or CRITICAL."
)
@click.option(
    "--seed",
    "do_seed",
    is_flag=True,
    default=False,
    help="Seed the source DSE cluster before assessing. Requires --template.",
)
@click.option(
    "--template",
    default="food",
    type=click.Choice(["food", "social", "iot", "enterprise"]),
    help="Template to use when --seed is set.",
)
@udt_strategy_option
@map_strategy_option
@decimal_strategy_option
@click.option("--cql-fetch-size", default=1000, show_default=True)
@click.option("--write-batch", default=100, show_default=True)
@_source_auth_options
@click.pass_context
def run_all(  # noqa: PLR0913
    ctx: click.Context,
    source_gremlin_url: str,
    source_cql_host: str,
    source_cql_port: int,
    graph_name: str,
    target_gremlin_url: str,
    target_cql_host: str | None,
    target_cql_port: int,
    target_cql_username: str,
    target_cql_password: str | None,
    output_dir: str,
    analyze_only: bool,
    skip_assessment_gate: bool,
    do_seed: bool,
    template: str,
    udt_strategy: str,
    map_strategy: str,
    decimal_strategy: str,
    cql_fetch_size: int,
    write_batch: int,
    source_username: str | None,
    source_password: str | None,
    source_cql_ssl: bool,
    source_ca_cert: str | None,
) -> None:
    """Run the full migration pipeline.

    Phases: seed (optional) → assess → analyse → translate → init-target
            → migrate → validate → report

    Optional zero-to-migration demo::

      dse-jg-migrator run-all --seed --template food --analyze-only ...
    """
    start = time.perf_counter()
    durations: dict[str, str] = {}  # per phase, recorded in the migration report
    target_cql_password = _require_target_password(target_cql_host, target_cql_password)  # fail before seeding
    _source_auth(source_username, source_password, source_cql_ssl, source_ca_cert)  # likewise
    os.makedirs(output_dir, exist_ok=True)
    analysis_path = os.path.join(output_dir, f"{graph_name}-analysis.json")
    schema_path = os.path.join(output_dir, f"{graph_name}-janusgraph-schema.json")

    # 0a. Optional seed
    if do_seed:
        ctx.invoke(
            seed,
            source_gremlin_url=source_gremlin_url,
            template=template,
            graph_name=graph_name,
            vertex_count=1000,
            edge_count=5000,
            confirm=True,
        )

    # 0b. Assess
    phase_start = time.perf_counter()
    ctx.invoke(
        assess,
        source_gremlin_url=source_gremlin_url,
        source_cql_host=source_cql_host,
        source_cql_port=source_cql_port,
        graph_name=graph_name,
        output_dir=output_dir,
        target_cql_host=target_cql_host,
        target_cql_port=target_cql_port,
        target_cql_username=target_cql_username,
        target_cql_password=target_cql_password,
        output_formats=("all",),
        filter_category="all",
        filter_severity="all",
        source_username=source_username,
        source_password=source_password,
        source_cql_ssl=source_cql_ssl,
        source_ca_cert=source_ca_cert,
        skip_assessment_gate=skip_assessment_gate,
    )
    durations["assessment"] = _elapsed(phase_start)

    if analyze_only:
        console.print(f"[bold]--analyze-only:[/bold] Stopping after assessment.{_took(start)}")
        return

    # Check assessment gate
    assessment_json = os.path.join(output_dir, f"{graph_name}-assessment-report.json")
    risk = _load_assessment_risk(assessment_json)
    if risk in ("HIGH", "CRITICAL") and not skip_assessment_gate:
        console.print(
            f"[red]Migration blocked:[/red] assessment risk is {risk}. "
            "Resolve RED findings or pass --skip-assessment-gate."
        )
        raise SystemExit(1)

    # 1. Analyse
    phase_start = time.perf_counter()
    ctx.invoke(
        analyse,
        source_gremlin_url=source_gremlin_url,
        source_cql_host=source_cql_host,
        source_cql_port=source_cql_port,
        graph_name=graph_name,
        output_dir=output_dir,
        source_username=source_username,
        source_password=source_password,
        source_cql_ssl=source_cql_ssl,
        source_ca_cert=source_ca_cert,
    )
    durations["analysis"] = _elapsed(phase_start)

    # 2. Translate
    phase_start = time.perf_counter()
    ctx.invoke(
        translate,
        analysis_path=analysis_path,
        udt_strategy=udt_strategy,
        map_strategy=map_strategy,
        decimal_strategy=decimal_strategy,
        output_dir=output_dir,
    )
    durations["translation"] = _elapsed(phase_start)

    # 3. Init target
    phase_start = time.perf_counter()
    ctx.invoke(
        init_target,
        target_gremlin_url=target_gremlin_url,
        schema_path=schema_path,
        mode="gremlin-script",
        username=None,
        password=None,
        index_timeout_min=30,
    )
    durations["init_target"] = _elapsed(phase_start)

    # 4. Migrate
    phase_start = time.perf_counter()
    ctx.invoke(
        migrate,
        analysis_path=analysis_path,
        source_gremlin_url=source_gremlin_url,
        target_gremlin_url=target_gremlin_url,
        source_cql_host=source_cql_host,
        source_cql_port=source_cql_port,
        source_username=source_username,
        source_password=source_password,
        source_cql_ssl=source_cql_ssl,
        source_ca_cert=source_ca_cert,
        cql_fetch_size=cql_fetch_size,
        gremlin_batch=200,
        write_batch=write_batch,
        parallel_labels=1,
        udt_strategy=udt_strategy,
        map_strategy=map_strategy,
        decimal_strategy=decimal_strategy,
        output_dir=output_dir,
    )
    durations["migration"] = _elapsed(phase_start)

    # 5. Validate
    phase_start = time.perf_counter()
    ctx.invoke(
        validate,
        analysis_path=analysis_path,
        source_gremlin_url=source_gremlin_url,
        target_gremlin_url=target_gremlin_url,
        queries_path=None,
        output_dir=output_dir,
        source_username=source_username,
        source_password=source_password,
        source_cql_ssl=source_cql_ssl,
        source_ca_cert=source_ca_cert,
    )
    durations["validation"] = _elapsed(phase_start)

    # 6. Report
    durations["total"] = _elapsed(start)  # up to the report, which records it
    ctx.invoke(report, graph_name=graph_name, output_dir=output_dir, durations=durations)
    console.print(f"\n[bold green]✓ Migration pipeline complete for graph '{graph_name}'.[/bold green]{_took(start)}")
