"""CLI entry point for the DSE Graph → JanusGraph / HCD Migration Accelerator."""

from __future__ import annotations

import json
import os
from typing import Any

import click
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn

console = Console()

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


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
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

    The graph is created with ifNotExists() — safe to run multiple times.

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

    console.print(f"[green]✓[/green] Graph [bold]{result.graph_name}[/bold] seeded successfully.")
    console.print(f"  Vertices: ~{result.vertex_count:,}  |  Edges: ~{result.edge_count:,}")
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
@click.option("--output-dir", default=".", show_default=True)
@click.option(
    "--target-cql-host",
    default=None,
    help="CQL host of the target HCD cluster. When provided, runs HCD pre-flight checks.",
)
@click.option("--target-cql-port", default=9042, show_default=True)
@click.option("--target-cql-username", default="cassandra", show_default=True)
@click.option(
    "--target-cql-password",
    default="cassandra",
    show_default=True,  # noqa: S106
    hide_input=True,
)
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
@click.option("--username", default=None, help="DSE source authentication username.")
@click.option("--password", default=None, hide_input=True, help="DSE source authentication password.")
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
    target_cql_password: str,
    output_formats: tuple[str, ...],
    filter_category: str,
    filter_severity: str,
    username: str | None,
    password: str | None,
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
        target_cql_password=target_cql_password,
        username=username,
        password=password,
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
    for fmt, path in paths.items():
        console.print(f"  [green]✓[/green] {fmt:8s} → {path}")

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
@click.option("--output-dir", default=".", show_default=True)
@click.option("--username", default=None)
@click.option("--password", default=None, hide_input=True)
def analyse(
    source_gremlin_url: str,
    source_cql_host: str,
    source_cql_port: int,
    graph_name: str,
    output_dir: str,
    username: str | None,
    password: str | None,
) -> None:
    """Analyse DSE Graph cluster and extract schema to JSON."""
    from dse_jg_migrator.analyser import Analyser

    analyser = Analyser(
        gremlin_url=source_gremlin_url,
        cql_host=source_cql_host,
        cql_port=source_cql_port,
        graph_name=graph_name,
        username=username,
        password=password,
        output_dir=output_dir,
    )
    console.print(f"[bold]Analysing graph:[/bold] {graph_name}")
    with console.status("Extracting schema (read-only)..."):
        result = analyser.run()

    out_file = os.path.join(output_dir, f"{graph_name}-analysis.json")
    console.print(f"[green]✓[/green] Analysis written → {out_file}")
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
@click.option("--output-dir", default=".", show_default=True)
def translate(
    analysis_path: str,
    udt_strategy: str,
    map_strategy: str,
    decimal_strategy: str,
    output_dir: str,
) -> None:
    """Translate DSE Graph schema to JanusGraph JSON schema."""
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
    console.print(f"[green]✓[/green] Schema written → {schema_file}")
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
    console.print("[green]✓[/green] Schema initialised successfully.")


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
@click.option("--source-cql-port", default=9043, show_default=True)
@click.option("--cql-fetch-size", default=1000, show_default=True)
@click.option("--gremlin-batch", default=200, show_default=True)
@click.option("--write-batch", default=100, show_default=True)
@click.option("--parallel-labels", default=1, show_default=True)
@udt_strategy_option
@map_strategy_option
@decimal_strategy_option
@click.option("--output-dir", default=".", show_default=True)
def migrate(
    analysis_path: str,
    source_gremlin_url: str,
    target_gremlin_url: str,
    source_cql_host: str,
    source_cql_port: int,
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
        console.print(f"[red]✗[/red] Migration incomplete: {total_v:,} vertices, {total_e:,} edges written")
        for err in stats["errors"]:
            console.print(f"  [red]✗[/red] {err}")
        console.print("  Re-run migrate to retry; already-migrated elements are skipped.")
        raise SystemExit(1)
    console.print(f"[green]✓[/green] Migration complete: {total_v:,} vertices, {total_e:,} edges")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@main.command()
@click.option("--analysis", "analysis_path", required=True, type=click.Path(exists=True))
@click.option("--source-gremlin-url", required=True)
@click.option("--target-gremlin-url", required=True)
@click.option("--validation-queries", "queries_path", default=None, type=click.Path())
@click.option("--output-dir", default=".", show_default=True)
def validate(
    analysis_path: str,
    source_gremlin_url: str,
    target_gremlin_url: str,
    queries_path: str | None,
    output_dir: str,
) -> None:
    """Compare source and target vertex/edge counts and run smoke tests."""
    from dse_jg_migrator.translator import Translator
    from dse_jg_migrator.validator import Validator

    tr = Translator(analysis=None, output_dir=output_dir)  # type: ignore[arg-type]
    analysis = tr.load_analysis(analysis_path)

    validator = Validator(
        analysis=analysis,
        source_gremlin_url=source_gremlin_url,
        target_gremlin_url=target_gremlin_url,
        validation_queries_path=queries_path,
        output_dir=output_dir,
    )

    console.print(f"[bold]Validating graph:[/bold] {analysis.graph_name}")
    with console.status("Running validation checks..."):
        report = validator.run()

    passes = sum(1 for r in report["count_results"] if r["match"])
    total = len(report["count_results"])
    status_icon = "[green]✓[/green]" if report["overall_pass"] else "[red]✗[/red]"
    console.print(f"{status_icon} Validation: {passes}/{total} count checks passed")
    if report.get("reason"):
        console.print(f"  [red]✗[/red] {report['reason']}")
    if not report["overall_pass"]:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@main.command()
@click.option("--graph-name", required=True)
@click.option("--output-dir", default=".", show_default=True)
def report(graph_name: str, output_dir: str) -> None:
    """Generate the final migration summary report."""
    from dse_jg_migrator.reporter import Reporter

    r = Reporter(graph_name=graph_name, output_dir=output_dir)
    path = r.generate()
    console.print(f"[green]✓[/green] Report written → {path}")


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
@click.option("--target-cql-username", default="cassandra", show_default=True)
@click.option(
    "--target-cql-password",
    default="cassandra",
    show_default=True,  # noqa: S106
    hide_input=True,
)
@click.option("--output-dir", default=".", show_default=True)
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
    target_cql_password: str,
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
) -> None:
    """Run the full migration pipeline.

    Phases: seed (optional) → assess → analyse → translate → init-target
            → migrate → validate → report

    Optional zero-to-migration demo::

      dse-jg-migrator run-all --seed --template food --analyze-only ...
    """
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
        username=None,
        password=None,
        skip_assessment_gate=skip_assessment_gate,
    )

    if analyze_only:
        console.print("[bold]--analyze-only:[/bold] Stopping after assessment.")
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
    ctx.invoke(
        analyse,
        source_gremlin_url=source_gremlin_url,
        source_cql_host=source_cql_host,
        source_cql_port=source_cql_port,
        graph_name=graph_name,
        output_dir=output_dir,
        username=None,
        password=None,
    )

    # 2. Translate
    ctx.invoke(
        translate,
        analysis_path=analysis_path,
        udt_strategy=udt_strategy,
        map_strategy=map_strategy,
        decimal_strategy=decimal_strategy,
        output_dir=output_dir,
    )

    # 3. Init target
    ctx.invoke(
        init_target,
        target_gremlin_url=target_gremlin_url,
        schema_path=schema_path,
        mode="gremlin-script",
        username=None,
        password=None,
        index_timeout_min=30,
    )

    # 4. Migrate
    ctx.invoke(
        migrate,
        analysis_path=analysis_path,
        source_gremlin_url=source_gremlin_url,
        target_gremlin_url=target_gremlin_url,
        source_cql_host=source_cql_host,
        source_cql_port=source_cql_port,
        cql_fetch_size=cql_fetch_size,
        gremlin_batch=200,
        write_batch=write_batch,
        parallel_labels=1,
        udt_strategy=udt_strategy,
        map_strategy=map_strategy,
        decimal_strategy=decimal_strategy,
        output_dir=output_dir,
    )

    # 5. Validate
    ctx.invoke(
        validate,
        analysis_path=analysis_path,
        source_gremlin_url=source_gremlin_url,
        target_gremlin_url=target_gremlin_url,
        queries_path=None,
        output_dir=output_dir,
    )

    # 6. Report
    ctx.invoke(report, graph_name=graph_name, output_dir=output_dir)
    console.print(f"\n[bold green]✓ Migration pipeline complete for graph '{graph_name}'.[/bold green]")
