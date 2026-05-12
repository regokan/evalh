from __future__ import annotations

from pathlib import Path

from eval_harness.core.models import RunSummary


def write_markdown_report(
    run_dir: Path,
    summary: RunSummary,
    out_path: Path | None = None,
) -> Path:
    """Render `summary` as a human-friendly markdown report.

    Writes to `out_path` (default `{run_dir}/report.md`). Returns the written
    path. Plain markdown — GitHub, Slack, and `glow` all render it.
    """
    target = out_path if out_path is not None else run_dir / "report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render(summary), encoding="utf-8")
    return target


def _render(summary: RunSummary) -> str:
    parts: list[str] = []
    parts.append(_render_header(summary))
    parts.append(_render_variants(summary))
    if summary.comparison is not None:
        parts.append(_render_comparison(summary))
    if summary.by_evaluator:
        parts.append(_render_evaluator_rollup(summary))
    return "\n".join(parts).rstrip() + "\n"


def _render_header(summary: RunSummary) -> str:
    started = summary.started_at.isoformat()
    finished = summary.finished_at.isoformat()
    duration_s = (summary.finished_at - summary.started_at).total_seconds()
    lines = [
        f"# Eval run — `{summary.run_id}`",
        "",
        f"- **Config:** `{summary.config_path}` (hash `{summary.config_hash}`)",
        f"- **Started:** {started}",
        f"- **Finished:** {finished} _(duration {duration_s:.1f}s)_",
        f"- **Cases:** {summary.cases_total}",
        "",
    ]
    return "\n".join(lines)


def _render_variants(summary: RunSummary) -> str:
    lines = [
        "## Variants",
        "",
        "| Variant | Pass rate | Passed / Total | Errored | Avg latency (ms) | Avg cost (USD) | Avg tokens in / out |",
        "|---|---|---|---|---|---|---|",
    ]
    for v in summary.variants:
        cost = _fmt_float(v.avg_cost_usd, precision=4)
        toks_in = _fmt_float(v.avg_tokens_input, precision=1)
        toks_out = _fmt_float(v.avg_tokens_output, precision=1)
        lines.append(
            f"| `{v.name}` | {v.pass_rate * 100:.1f}% | "
            f"{v.cases_passed} / {v.cases_total} | {v.cases_errored} | "
            f"{v.avg_latency_ms:.0f} | {cost} | {toks_in} / {toks_out} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_comparison(summary: RunSummary) -> str:
    assert summary.comparison is not None
    cmp = summary.comparison
    lines = [
        "## Comparison",
        "",
        f"Baseline: `{cmp.baseline}`",
        "",
    ]
    if not cmp.deltas:
        lines.append("_No non-baseline variants to compare._")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "| Variant | Δ pass rate | Δ avg latency (ms) | Regressions | Improvements |",
            "|---|---|---|---|---|",
        ]
    )
    for d in cmp.deltas:
        lines.append(
            f"| `{d.variant}` | {d.pass_rate_delta * 100:+.1f}pp | "
            f"{d.avg_latency_delta_ms:+.0f} | "
            f"{len(d.regressions)} | {len(d.improvements)} |"
        )
    lines.append("")

    for d in cmp.deltas:
        if not d.regressions and not d.improvements:
            continue
        lines.append(f"### `{d.variant}` vs `{cmp.baseline}`")
        lines.append("")
        if d.regressions:
            lines.append(f"**Regressions ({len(d.regressions)}):**")
            for case_id in d.regressions:
                lines.append(f"- `{case_id}`")
            lines.append("")
        if d.improvements:
            lines.append(f"**Improvements ({len(d.improvements)}):**")
            for case_id in d.improvements:
                lines.append(f"- `{case_id}`")
            lines.append("")
    return "\n".join(lines)


def _render_evaluator_rollup(summary: RunSummary) -> str:
    variant_names = [v.name for v in summary.variants]
    if not variant_names:
        return ""

    header = "| Evaluator | " + " | ".join(f"`{v}`" for v in variant_names) + " |"
    sep = "|---|" + "|".join(["---"] * len(variant_names)) + "|"
    lines = ["## Per-evaluator pass rates", "", header, sep]
    for ev in summary.by_evaluator:
        row = [f"`{ev.evaluator}`"]
        for v in variant_names:
            rollup = ev.by_variant.get(v)
            if rollup is None:
                row.append("—")
            else:
                score = _fmt_float(rollup.avg_score, precision=2)
                row.append(f"{rollup.pass_rate * 100:.1f}% (score {score})")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


def _fmt_float(value: float | None, *, precision: int) -> str:
    if value is None:
        return "—"
    return f"{value:.{precision}f}"
