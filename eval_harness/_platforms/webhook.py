"""Canonical summary-message struct + per-platform formatters.

The webhook TraceStore builds one `SummaryMessage` from the run's
`RunSummary` (+ optional `ComparisonReport(kind='drift')`), then
dispatches to a platform-specific formatter. Slack Block Kit / Discord
embed / Linear GraphQL quirks live in their formatter and never leak
into the summary builder.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_TOP_REGRESSIONS_LIMIT = 10
_TOP_IMPROVEMENTS_LIMIT = 5


class VariantLine(BaseModel):
    name: str
    pass_rate: float
    cases_passed: int
    cases_total: int


class DriftSummary(BaseModel):
    baseline_run_id: str
    regressions_count: int
    improvements_count: int
    pass_rate_delta: float
    top_regressions: list[str] = Field(default_factory=list)
    top_improvements: list[str] = Field(default_factory=list)


class SummaryMessage(BaseModel):
    """Canonical summary the webhook formatters consume.

    Build via `build_summary_message(run_summary)`. The struct is
    deliberately platform-agnostic — render Slack Block Kit, Discord
    embeds, or Linear markdown by passing it to the matching
    `format_*` helper."""

    run_id: str
    eval_name: str
    config_hash: str
    cases_total: int
    variants: list[VariantLine] = Field(default_factory=list)
    drift: DriftSummary | None = None


def build_summary_message(run_summary: Any) -> SummaryMessage:
    """Reduce a `RunSummary` to the platform-agnostic `SummaryMessage`.

    Tolerant of unset / sparse fields so this never crashes inside the
    webhook's save_summary hook.
    """
    variants = [
        VariantLine(
            name=v.name,
            pass_rate=float(v.pass_rate),
            cases_passed=int(v.cases_passed),
            cases_total=int(v.cases_total),
        )
        for v in run_summary.variants
    ]
    drift = _maybe_drift(run_summary)
    eval_name = _eval_name_from(run_summary)
    return SummaryMessage(
        run_id=run_summary.run_id,
        eval_name=eval_name,
        config_hash=getattr(run_summary, "config_hash", "") or "",
        cases_total=int(run_summary.cases_total),
        variants=variants,
        drift=drift,
    )


def _eval_name_from(run_summary: Any) -> str:
    """Pull eval_name from `config_path`'s stem when not otherwise present.
    Most run_summaries store `config_path: 'eval.yaml'`; we fall back to
    the run_id's suffix (the v0 run-id convention is `<ts>_<eval_name>`)."""
    rid = str(getattr(run_summary, "run_id", "") or "")
    if "_" in rid:
        return rid.split("_", 1)[1]
    return rid


def _maybe_drift(run_summary: Any) -> DriftSummary | None:
    comp = getattr(run_summary, "comparison", None)
    if comp is None or getattr(comp, "kind", "ad_hoc") != "drift":
        return None
    delta = comp.deltas[0] if comp.deltas else None
    return DriftSummary(
        baseline_run_id=str(getattr(comp, "baseline_run_id", "") or comp.baseline),
        regressions_count=int(comp.regressions_count or 0),
        improvements_count=int(comp.improvements_count or 0),
        pass_rate_delta=float(delta.pass_rate_delta) if delta is not None else 0.0,
        top_regressions=(delta.regressions[:_TOP_REGRESSIONS_LIMIT] if delta else []),
        top_improvements=(
            delta.improvements[:_TOP_IMPROVEMENTS_LIMIT] if delta else []
        ),
    )


# ---- Slack Block Kit ----------------------------------------------------


def format_slack(msg: SummaryMessage) -> dict[str, Any]:
    """Slack Block Kit payload: header + variant fields + (drift) section."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"evalh {msg.eval_name} — {msg.run_id}",
            },
        },
    ]
    if msg.variants:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*{v.name}*\n"
                            f"{v.cases_passed}/{v.cases_total} passed "
                            f"({v.pass_rate:.1%})"
                        ),
                    }
                    for v in msg.variants
                ],
            }
        )

    if msg.drift is not None:
        d = msg.drift
        emoji = ":warning:" if d.regressions_count else ":white_check_mark:"
        text_lines = [
            f"*{emoji} Drift vs `{d.baseline_run_id}`*",
            f"pass-rate Δ: *{d.pass_rate_delta:+.2%}*    "
            f"regressions: *{d.regressions_count}*    "
            f"improvements: *{d.improvements_count}*",
        ]
        if d.top_regressions:
            text_lines.append(
                "regressions: " + ", ".join(f"`{c}`" for c in d.top_regressions)
            )
        if d.top_improvements:
            text_lines.append(
                "improvements: " + ", ".join(f"`{c}`" for c in d.top_improvements)
            )
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(text_lines)},
            }
        )

    return {"blocks": blocks}


# ---- Discord embed ------------------------------------------------------


def format_discord(msg: SummaryMessage) -> dict[str, Any]:
    """Discord webhook payload with one embed."""
    fields: list[dict[str, Any]] = [
        {
            "name": v.name,
            "value": (
                f"{v.cases_passed}/{v.cases_total} passed ({v.pass_rate:.1%})"
            ),
            "inline": True,
        }
        for v in msg.variants
    ]
    description_parts: list[str] = [f"**eval:** `{msg.eval_name}`"]

    color = 0x2ECC71  # green default
    if msg.drift is not None:
        d = msg.drift
        if d.regressions_count:
            color = 0xE74C3C
        description_parts.append(
            f"**Drift vs `{d.baseline_run_id}`** — "
            f"Δ {d.pass_rate_delta:+.2%}, "
            f"{d.regressions_count} regression(s), "
            f"{d.improvements_count} improvement(s)"
        )
        if d.top_regressions:
            description_parts.append(
                "regressions: " + ", ".join(f"`{c}`" for c in d.top_regressions)
            )

    embed: dict[str, Any] = {
        "title": f"evalh — {msg.run_id}",
        "description": "\n".join(description_parts),
        "color": color,
        "fields": fields,
    }
    return {"embeds": [embed]}


# ---- Linear markdown comment -------------------------------------------


def format_linear(msg: SummaryMessage) -> str:
    """Linear API takes a free-form markdown comment body. Render the
    same structure the other two formatters cover."""
    lines: list[str] = [
        f"### evalh `{msg.eval_name}` — `{msg.run_id}`",
        "",
        f"cases_total: **{msg.cases_total}**",
        "",
    ]
    if msg.variants:
        lines.append("| variant | passed | pass rate |")
        lines.append("|---|---|---|")
        for v in msg.variants:
            lines.append(
                f"| `{v.name}` | {v.cases_passed}/{v.cases_total} | {v.pass_rate:.1%} |"
            )
        lines.append("")

    if msg.drift is not None:
        d = msg.drift
        lines.append(f"#### Drift vs `{d.baseline_run_id}`")
        lines.append("")
        lines.append(f"- pass-rate Δ: **{d.pass_rate_delta:+.2%}**")
        lines.append(f"- regressions: **{d.regressions_count}**")
        lines.append(f"- improvements: **{d.improvements_count}**")
        if d.top_regressions:
            lines.append("")
            lines.append("**Top regressions:**")
            lines.extend(f"- `{c}`" for c in d.top_regressions)
        if d.top_improvements:
            lines.append("")
            lines.append("**Top improvements:**")
            lines.extend(f"- `{c}`" for c in d.top_improvements)

    return "\n".join(lines)


__all__ = [
    "DriftSummary",
    "SummaryMessage",
    "VariantLine",
    "build_summary_message",
    "format_discord",
    "format_linear",
    "format_slack",
]
