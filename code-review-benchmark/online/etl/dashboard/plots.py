"""Chart rendering for the Streamlit dashboard."""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

_CHATBOT_COLORS = px.colors.qualitative.Plotly


def _color_map(names: list[str]) -> dict[str, str]:
    """Build a stable name→color mapping (sorted alphabetically so order is deterministic)."""
    return {name: _CHATBOT_COLORS[i % len(_CHATBOT_COLORS)] for i, name in enumerate(sorted(set(names)))}


def _compute_f_beta(p, r, beta: float):
    """Compute F-beta from precision and recall Series/scalars."""
    b2 = beta**2
    denom = b2 * p + r
    # avoid division by zero
    if isinstance(denom, pd.Series):
        return ((1 + b2) * p * r / denom).where(denom > 0, None)
    return (1 + b2) * p * r / denom if denom and denom > 0 else None


def f_beta_over_time(
    daily_metrics: list[dict], start_date: str | None = None, end_date: str | None = None, beta: float = 1.0
) -> go.Figure | None:
    """Line chart: F-beta score over time, one line per chatbot."""
    if not daily_metrics:
        return None

    df = pd.DataFrame(daily_metrics)
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    if start_date:
        df = df[df["date"] >= pd.Timestamp(start_date, tz="UTC")]
    if end_date:
        df = df[df["date"] <= pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)]
    if df.empty:
        return None

    df["avg_f_beta"] = _compute_f_beta(df["avg_precision"], df["avg_recall"], beta)
    cmap = _color_map(df["github_username"].tolist())

    fig = px.line(
        df,
        x="date",
        y="avg_f_beta",
        color="github_username",
        color_discrete_map=cmap,
        title=f"F{beta:.1f} Score Over Time (Daily Average)",
        labels={"date": "Date", "avg_f_beta": f"F{beta:.1f} Score", "github_username": "Chatbot"},
        markers=True,
        hover_data={"pr_count": True},
    )
    fig.update_layout(
        yaxis_range=[0, 1],
        hovermode="x unified",
        xaxis={
            "tickformat": "%Y-%m-%d",
            "dtick": "D1" if len(df) < 60 else None,
        },
    )
    return fig


def precision_recall_scatter(
    analyses: list[dict], start_date: str | None = None, end_date: str | None = None, beta: float = 1.0
) -> go.Figure | None:
    """Scatter plot: one dot per chatbot, aggregated precision & recall in date range."""
    if not analyses:
        return None

    df = pd.DataFrame(analyses)
    df = df.dropna(subset=["precision", "recall"])

    if "bot_reviewed_at" in df.columns:
        df["bot_reviewed_at"] = pd.to_datetime(df["bot_reviewed_at"], utc=True, errors="coerce")
        if start_date:
            df = df[df["bot_reviewed_at"] >= pd.Timestamp(start_date, tz="UTC")]
        if end_date:
            df = df[df["bot_reviewed_at"] <= pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)]

    if df.empty:
        return None

    agg = (
        df.groupby("github_username")
        .agg(
            precision=("precision", "mean"),
            recall=("recall", "mean"),
            pr_count=("precision", "count"),
        )
        .reset_index()
    )
    agg["f_beta"] = _compute_f_beta(agg["precision"], agg["recall"], beta)
    cmap = _color_map(agg["github_username"].tolist())

    fig = px.scatter(
        agg,
        x="precision",
        y="recall",
        color="github_username",
        color_discrete_map=cmap,
        size="pr_count",
        hover_data={"f_beta": ":.2f", "precision": ":.2f", "recall": ":.2f", "pr_count": True},
        title="% Acted On vs # Comments Acted On (aggregated per tool)",
        labels={
            "precision": "% Comments Acted On",
            "recall": "# Comments Acted On",
            "github_username": "Chatbot",
            "pr_count": "PRs",
            "f_beta": f"F{beta:.1f}",
        },
        size_max=30,
    )
    fig.update_layout(
        xaxis_range=[0, 1.05],
        yaxis_range=[0, 1.05],
    )
    return fig


def status_summary_chart(status_data: list[dict]) -> go.Figure | None:
    """Stacked bar chart showing PR processing status per chatbot."""
    if not status_data:
        return None

    df = pd.DataFrame(status_data)
    fig = px.bar(
        df,
        x="github_username",
        y="count",
        color="status",
        title="PR Processing Status by Chatbot",
        labels={"github_username": "Chatbot", "count": "Number of PRs", "status": "Status"},
        barmode="stack",
    )
    return fig
