#!/usr/bin/env python3
"""
Schritt 12: Google-Trends-Rohdateien aufbereiten, eventbezogene
Aufmerksamkeitsvariablen konstruieren und in den 170-Event-Masterdatensatz
integrieren.

Die Rohdateien werden aus einem Ordner wie `google_trends_raw/` gelesen.
Erwartete Dateimuster:
    AAPL_AAPL_1_rep*.csv
    AAPL_AAPL_2_rep*.csv
    AMZN_AMZN_1_rep*.csv
    AMZN_AMZN_2_rep*.csv
    GOOGL_GOOGL_1_rep*.csv
    GOOGL_GOOGL_2_rep*.csv
    MSFT_MSFT_1_rep*.csv
    MSFT_MSFT_2_rep*.csv
    META_META_FB_rep*.csv
    META_META_META_rep*.csv

Methodik
--------
- Google Trends liefert wöchentliche Werte von 0 bis 100.
- "<1" wird als 0,5 kodiert.
- Getrennt exportierte Zeitsegmente desselben Suchbegriffs werden über ihren
  Überlappungszeitraum robust skaliert (Median der Wochenquotienten).
- Für Meta wird bis einschließlich 28.10.2021 "Facebook stock" verwendet,
  danach "Meta stock". Beide Suchbegriffe werden nicht als identische
  Rohserie behandelt; die eventbezogene Log-Veränderung ist innerhalb des
  jeweils verwendeten Suchbegriffs skaleninvariant.
- Eventwoche ist die am Sonntag beginnende Woche, die `reported_date` enthält.
- Hauptmaß:
      log(SVI_event) - log(Median(SVI der bis zu 8 Vorwochen))
  Für die ersten fünf Events stehen nur vier Vorwochen zur Verfügung. Sie
  bleiben in einer klar gekennzeichneten Full-Sample-Variable enthalten.
- Strenge Robustheitsvariable: exakt 8 Vorwochen; 165 Events verfügbar.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf


SCRIPT_VERSION = "1.0.0"
META_RENAME_DATE = pd.Timestamp("2021-10-28")
SAME_TERM_TICKERS = ("AAPL", "AMZN", "GOOGL", "MSFT")
CAR_WINDOWS = ("car_0_0", "car_0_p1", "car_m1_p1", "car_m2_p2", "car_m5_p5")
BENCHMARKS = ("spy", "qqq")


class GoogleTrendsError(RuntimeError):
    """Fehler bei der Google-Trends-Aufbereitung."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Google Trends in den Eventdatensatz integrieren."
    )
    parser.add_argument(
        "--raw-dir",
        default="google_trends_raw",
        help="Ordner mit den Google-Trends-CSV-Dateien.",
    )
    parser.add_argument(
        "--master-file",
        default=(
            "sentiment_integration_results/"
            "event_analysis_master_wide.csv"
        ),
        help="170-zeiliger Event-Masterdatensatz.",
    )
    parser.add_argument(
        "--output-dir",
        default="google_trends_results",
        help="Ausgabeordner.",
    )
    parser.add_argument(
        "--baseline-weeks",
        type=int,
        default=8,
        help="Zahl der Vorwochen für die Hauptbaseline.",
    )
    parser.add_argument(
        "--minimum-fallback-weeks",
        type=int,
        default=4,
        help="Mindestzahl für die Full-Sample-Fallbackvariable.",
    )
    parser.add_argument(
        "--floor-value",
        type=float,
        default=0.5,
        help="Ersatzwert für <1 und nichtpositive SVI-Werte.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Bei fehlenden Full-Sample-Werten abbrechen.",
    )
    return parser.parse_args()


def configure_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                output_dir / "run_google_trends_integration.log",
                encoding="utf-8",
            ),
        ],
        force=True,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_trends_csv(path: Path, floor_value: float) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, skiprows=2, encoding="utf-8-sig")
    except UnicodeDecodeError:
        frame = pd.read_csv(path, skiprows=2, encoding="latin-1")

    if frame.shape[1] != 2:
        raise GoogleTrendsError(
            f"{path.name}: erwartet wurden zwei Spalten, gefunden "
            f"wurden {frame.shape[1]}."
        )

    date_column, value_column = frame.columns[:2]
    frame = frame.rename(
        columns={date_column: "week_start", value_column: "raw_value"}
    )
    frame["week_start"] = pd.to_datetime(
        frame["week_start"], errors="coerce"
    )

    raw = frame["raw_value"].astype(str).str.strip()
    frame["was_less_than_one"] = raw.eq("<1")
    raw = raw.replace({"<1": str(floor_value)})
    raw = raw.str.replace(",", ".", regex=False)
    frame["svi"] = pd.to_numeric(raw, errors="coerce").astype(float)
    frame.loc[frame["svi"] <= 0, "svi"] = float(floor_value)

    if frame[["week_start", "svi"]].isna().any().any():
        raise GoogleTrendsError(
            f"{path.name}: ungültige Wochen- oder SVI-Werte."
        )
    if frame["week_start"].duplicated().any():
        raise GoogleTrendsError(f"{path.name}: doppelte Wochen.")

    frame["source_file"] = path.name
    frame["query_label"] = str(value_column)
    return frame[
        [
            "week_start",
            "svi",
            "was_less_than_one",
            "source_file",
            "query_label",
        ]
    ].sort_values("week_start")


def load_replicates(
    raw_dir: Path,
    pattern: str,
    floor_value: float,
) -> tuple[pd.DataFrame, list[Path]]:
    paths = sorted(raw_dir.glob(pattern))
    if not paths:
        raise GoogleTrendsError(
            f"Keine Datei für Muster {pattern!r} in {raw_dir}."
        )

    frames: list[pd.DataFrame] = []
    for replicate_index, path in enumerate(paths, start=1):
        frame = read_trends_csv(path, floor_value).copy()
        frame["replicate_index"] = replicate_index
        frames.append(frame)

    combined = pd.concat(frames, ignore_index=True)
    summary = (
        combined.groupby("week_start", as_index=False)
        .agg(
            svi=("svi", "mean"),
            replicate_count=("replicate_index", "nunique"),
            less_than_one_count=("was_less_than_one", "sum"),
            query_label=("query_label", "first"),
        )
        .sort_values("week_start")
    )
    summary["query_label"] = (
        summary["query_label"].astype(str).str.split(":", n=1).str[0].str.strip()
    )
    summary["source_files"] = "|".join(path.name for path in paths)
    return summary, paths


def validate_weekly_calendar(frame: pd.DataFrame, label: str) -> None:
    differences = frame["week_start"].sort_values().diff().dropna().dt.days
    bad = differences.loc[differences.ne(7)]
    if not bad.empty:
        raise GoogleTrendsError(
            f"{label}: nicht wöchentliche Abstände gefunden: "
            f"{sorted(bad.unique().tolist())}"
        )


def stitch_same_term(
    ticker: str,
    first: pd.DataFrame,
    second: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    overlap = first[["week_start", "svi"]].merge(
        second[["week_start", "svi"]],
        on="week_start",
        how="inner",
        suffixes=("_first", "_second"),
    )
    valid = overlap.loc[
        (overlap["svi_first"] > 0) & (overlap["svi_second"] > 0)
    ].copy()
    if len(valid) < 20:
        raise GoogleTrendsError(
            f"{ticker}: zu wenig Überlappungswochen für Skalierung."
        )

    ratios = valid["svi_first"] / valid["svi_second"]
    scale_second_to_first = float(ratios.median())
    correlation = float(
        valid[["svi_first", "svi_second"]].corr().iloc[0, 1]
    )

    first_values = first.set_index("week_start")["svi"]
    second_scaled = (
        second.set_index("week_start")["svi"] * scale_second_to_first
    )
    index = first_values.index.union(second_scaled.index).sort_values()
    combined = pd.DataFrame(index=index)
    combined["first"] = first_values
    combined["second_scaled"] = second_scaled
    combined["svi"] = combined[["first", "second_scaled"]].mean(
        axis=1, skipna=True
    )
    combined["ticker"] = ticker
    combined["query_term"] = str(first["query_label"].iloc[0])
    combined["source_method"] = np.select(
        [
            combined["first"].notna() & combined["second_scaled"].notna(),
            combined["first"].notna(),
            combined["second_scaled"].notna(),
        ],
        ["overlap_mean", "segment_1", "segment_2_scaled"],
        default="missing",
    )
    combined = combined.reset_index().rename(columns={"index": "week_start"})
    combined = combined[
        [
            "ticker",
            "week_start",
            "svi",
            "query_term",
            "source_method",
        ]
    ]
    validate_weekly_calendar(combined, ticker)

    diagnostics = {
        "ticker": ticker,
        "method": "same_query_overlap_scaling",
        "first_start": first["week_start"].min().date().isoformat(),
        "first_end": first["week_start"].max().date().isoformat(),
        "second_start": second["week_start"].min().date().isoformat(),
        "second_end": second["week_start"].max().date().isoformat(),
        "overlap_weeks": int(len(overlap)),
        "scale_second_to_first": scale_second_to_first,
        "overlap_correlation": correlation,
        "ratio_q25": float(ratios.quantile(0.25)),
        "ratio_median": float(ratios.median()),
        "ratio_q75": float(ratios.quantile(0.75)),
        "stitched_weeks": int(len(combined)),
    }
    return combined, diagnostics


def load_weekly_series(
    raw_dir: Path,
    floor_value: float,
) -> tuple[pd.DataFrame, pd.DataFrame, list[Path]]:
    weekly_parts: list[pd.DataFrame] = []
    diagnostics: list[dict[str, Any]] = []
    all_paths: list[Path] = []

    for ticker in SAME_TERM_TICKERS:
        first, first_paths = load_replicates(
            raw_dir,
            f"{ticker}_{ticker}_1_rep*.csv",
            floor_value,
        )
        second, second_paths = load_replicates(
            raw_dir,
            f"{ticker}_{ticker}_2_rep*.csv",
            floor_value,
        )
        all_paths.extend(first_paths + second_paths)
        stitched, diagnostic = stitch_same_term(ticker, first, second)
        weekly_parts.append(stitched)
        diagnostic["source_files"] = "|".join(
            path.name for path in first_paths + second_paths
        )
        diagnostics.append(diagnostic)

    facebook, facebook_paths = load_replicates(
        raw_dir,
        "META_META_FB_rep*.csv",
        floor_value,
    )
    meta, meta_paths = load_replicates(
        raw_dir,
        "META_META_META_rep*.csv",
        floor_value,
    )
    all_paths.extend(facebook_paths + meta_paths)
    validate_weekly_calendar(facebook, "Facebook stock")
    validate_weekly_calendar(meta, "Meta stock")

    for source_name, source_frame, query_term in [
        ("facebook", facebook, "Facebook stock"),
        ("meta", meta, "Meta stock"),
    ]:
        part = source_frame[["week_start", "svi"]].copy()
        part["ticker"] = "META"
        part["query_term"] = query_term
        part["source_method"] = f"meta_separate_term_{source_name}"
        weekly_parts.append(
            part[
                [
                    "ticker",
                    "week_start",
                    "svi",
                    "query_term",
                    "source_method",
                ]
            ]
        )

    diagnostics.append(
        {
            "ticker": "META",
            "method": "separate_query_terms_by_event_date",
            "first_start": facebook["week_start"].min().date().isoformat(),
            "first_end": facebook["week_start"].max().date().isoformat(),
            "second_start": meta["week_start"].min().date().isoformat(),
            "second_end": meta["week_start"].max().date().isoformat(),
            "overlap_weeks": int(
                facebook[["week_start"]].merge(
                    meta[["week_start"]], on="week_start"
                ).shape[0]
            ),
            "scale_second_to_first": np.nan,
            "overlap_correlation": np.nan,
            "ratio_q25": np.nan,
            "ratio_median": np.nan,
            "ratio_q75": np.nan,
            "stitched_weeks": np.nan,
            "source_files": "|".join(
                path.name for path in facebook_paths + meta_paths
            ),
            "note": (
                "Facebook stock bis einschließlich 2021-10-28; "
                "Meta stock danach. Keine direkte Rohwertskalierung, "
                "weil es unterschiedliche Suchbegriffe sind."
            ),
        }
    )

    weekly = pd.concat(weekly_parts, ignore_index=True).sort_values(
        ["ticker", "query_term", "week_start"]
    )
    return weekly, pd.DataFrame(diagnostics), sorted(set(all_paths))


def load_master(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise GoogleTrendsError(f"Masterdatei nicht gefunden: {path}")
    master = pd.read_csv(path, encoding="utf-8-sig")
    required = {
        "event_id",
        "ticker",
        "reported_date",
        "event_trading_day",
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        "sentiment_finbert_management",
    }
    missing = required - set(master.columns)
    if missing:
        raise GoogleTrendsError(
            "Fehlende Masterspalten: " + ", ".join(sorted(missing))
        )
    master = master.copy()
    master["event_id"] = master["event_id"].astype(str).str.strip()
    master["ticker"] = master["ticker"].astype(str).str.strip().str.upper()
    master["reported_date"] = pd.to_datetime(
        master["reported_date"], errors="coerce"
    )
    master["event_trading_day"] = pd.to_datetime(
        master["event_trading_day"], errors="coerce"
    )
    if master[["reported_date", "event_trading_day"]].isna().any().any():
        raise GoogleTrendsError("Ungültige Datumswerte in der Masterdatei.")
    if master["event_id"].duplicated().any():
        raise GoogleTrendsError("Doppelte Event-IDs in der Masterdatei.")
    return master.sort_values(["reported_date", "ticker"]).reset_index(drop=True)


def sunday_week_start(date_value: pd.Timestamp) -> pd.Timestamp:
    days_since_sunday = (date_value.dayofweek + 1) % 7
    return date_value.normalize() - pd.Timedelta(days=days_since_sunday)


def select_meta_term(reported_date: pd.Timestamp) -> str:
    return (
        "Facebook stock"
        if reported_date <= META_RENAME_DATE
        else "Meta stock"
    )


def safe_log(value: float, floor_value: float) -> float:
    return math.log(max(float(value), floor_value))


def calculate_attention_for_point(
    series: pd.Series,
    event_week: pd.Timestamp,
    baseline_weeks: int,
    minimum_weeks: int,
    floor_value: float,
) -> dict[str, Any]:
    if event_week not in series.index:
        return {
            "event_svi": np.nan,
            "baseline_observations_available": 0,
            "baseline_median_available": np.nan,
            "baseline_mean_available": np.nan,
            "baseline_std_available": np.nan,
            "attention_log_change_available": np.nan,
            "attention_ratio_available": np.nan,
            "attention_zscore_available": np.nan,
            "baseline_median_exact": np.nan,
            "attention_log_change_exact": np.nan,
            "attention_ratio_exact": np.nan,
            "attention_zscore_exact": np.nan,
        }

    event_svi = float(series.loc[event_week])
    prior_dates = [
        event_week - pd.Timedelta(weeks=offset)
        for offset in range(1, baseline_weeks + 1)
    ]
    prior_values = series.reindex(prior_dates).dropna().astype(float)
    available_n = int(len(prior_values))

    output: dict[str, Any] = {
        "event_svi": event_svi,
        "baseline_observations_available": available_n,
        "baseline_median_available": np.nan,
        "baseline_mean_available": np.nan,
        "baseline_std_available": np.nan,
        "attention_log_change_available": np.nan,
        "attention_ratio_available": np.nan,
        "attention_zscore_available": np.nan,
        "baseline_median_exact": np.nan,
        "attention_log_change_exact": np.nan,
        "attention_ratio_exact": np.nan,
        "attention_zscore_exact": np.nan,
    }

    if available_n >= minimum_weeks:
        median = float(prior_values.median())
        mean = float(prior_values.mean())
        std = float(prior_values.std(ddof=1)) if available_n > 1 else np.nan
        output.update(
            {
                "baseline_median_available": median,
                "baseline_mean_available": mean,
                "baseline_std_available": std,
                "attention_log_change_available": (
                    safe_log(event_svi, floor_value)
                    - safe_log(median, floor_value)
                ),
                "attention_ratio_available": (
                    event_svi / max(median, floor_value)
                ),
                "attention_zscore_available": (
                    (event_svi - mean) / std
                    if np.isfinite(std) and std > 0
                    else np.nan
                ),
            }
        )

    if available_n == baseline_weeks:
        exact_values = prior_values
        exact_median = float(exact_values.median())
        exact_mean = float(exact_values.mean())
        exact_std = float(exact_values.std(ddof=1))
        output.update(
            {
                "baseline_median_exact": exact_median,
                "attention_log_change_exact": (
                    safe_log(event_svi, floor_value)
                    - safe_log(exact_median, floor_value)
                ),
                "attention_ratio_exact": (
                    event_svi / max(exact_median, floor_value)
                ),
                "attention_zscore_exact": (
                    (event_svi - exact_mean) / exact_std
                    if np.isfinite(exact_std) and exact_std > 0
                    else np.nan
                ),
            }
        )

    return output


def construct_event_attention(
    master: pd.DataFrame,
    weekly: pd.DataFrame,
    baseline_weeks: int,
    minimum_fallback_weeks: int,
    floor_value: float,
) -> pd.DataFrame:
    indexed: dict[tuple[str, str], pd.Series] = {}
    for (ticker, query_term), group in weekly.groupby(
        ["ticker", "query_term"]
    ):
        indexed[(ticker, query_term)] = group.set_index("week_start")[
            "svi"
        ].sort_index()

    rows: list[dict[str, Any]] = []
    for event in master.itertuples(index=False):
        event_week = sunday_week_start(pd.Timestamp(event.reported_date))
        if event.ticker == "META":
            query_term = select_meta_term(pd.Timestamp(event.reported_date))
        else:
            candidates = [
                key[1] for key in indexed if key[0] == event.ticker
            ]
            if len(candidates) != 1:
                raise GoogleTrendsError(
                    f"{event.ticker}: Suchbegriff nicht eindeutig: {candidates}"
                )
            query_term = candidates[0]

        key = (event.ticker, query_term)
        if key not in indexed:
            raise GoogleTrendsError(
                f"Keine Zeitreihe für {event.ticker} / {query_term}."
            )
        series = indexed[key]

        current = calculate_attention_for_point(
            series=series,
            event_week=event_week,
            baseline_weeks=baseline_weeks,
            minimum_weeks=minimum_fallback_weeks,
            floor_value=floor_value,
        )

        previous_week = event_week - pd.Timedelta(weeks=1)
        pre_event = calculate_attention_for_point(
            series=series,
            event_week=previous_week,
            baseline_weeks=baseline_weeks,
            minimum_weeks=minimum_fallback_weeks,
            floor_value=floor_value,
        )

        rows.append(
            {
                "event_id": event.event_id,
                "ticker": event.ticker,
                "reported_date": pd.Timestamp(event.reported_date).date().isoformat(),
                "google_trends_event_week_start": event_week.date().isoformat(),
                "google_trends_query_term": query_term,
                "google_trends_event_svi": current["event_svi"],
                "google_trends_baseline_weeks_available": current[
                    "baseline_observations_available"
                ],
                "google_trends_baseline_median": current[
                    "baseline_median_available"
                ],
                "google_trends_baseline_mean": current[
                    "baseline_mean_available"
                ],
                "google_trends_attention": current[
                    "attention_log_change_available"
                ],
                "google_trends_attention_ratio": current[
                    "attention_ratio_available"
                ],
                "google_trends_attention_zscore": current[
                    "attention_zscore_available"
                ],
                "google_trends_attention_exact8": current[
                    "attention_log_change_exact"
                ],
                "google_trends_attention_ratio_exact8": current[
                    "attention_ratio_exact"
                ],
                "google_trends_attention_zscore_exact8": current[
                    "attention_zscore_exact"
                ],
                "google_trends_pre_event_attention": pre_event[
                    "attention_log_change_available"
                ],
                "google_trends_pre_event_attention_exact8": pre_event[
                    "attention_log_change_exact"
                ],
                "google_trends_uses_fallback_baseline": (
                    current["baseline_observations_available"]
                    < baseline_weeks
                ),
                "google_trends_full_sample_usable": pd.notna(
                    current["attention_log_change_available"]
                ),
                "google_trends_exact8_usable": pd.notna(
                    current["attention_log_change_exact"]
                ),
                "google_trends_note": (
                    "Meta query term selected by report date"
                    if event.ticker == "META"
                    else "same query stitched across overlapping exports"
                ),
            }
        )

    result = pd.DataFrame(rows)
    lower = result["google_trends_attention"].quantile(0.01)
    upper = result["google_trends_attention"].quantile(0.99)
    result["google_trends_attention_winsor_1_99"] = result[
        "google_trends_attention"
    ].clip(lower, upper)
    return result


def merge_master(
    master: pd.DataFrame,
    event_attention: pd.DataFrame,
    strict: bool,
) -> pd.DataFrame:
    merged = master.merge(
        event_attention,
        on=["event_id", "ticker"],
        how="left",
        validate="one_to_one",
        indicator=True,
        suffixes=("", "_gt"),
    )
    missing = merged.loc[merged["_merge"] != "both", "event_id"].tolist()
    if missing:
        raise GoogleTrendsError(
            f"{len(missing)} Events ohne Google-Trends-Zuordnung."
        )
    merged = merged.drop(columns="_merge")

    if "reported_date_gt" in merged.columns:
        left_date = pd.to_datetime(merged["reported_date"], errors="coerce")
        right_date = pd.to_datetime(merged["reported_date_gt"], errors="coerce")
        if not left_date.equals(right_date):
            raise GoogleTrendsError(
                "reported_date stimmt zwischen Master und Google Trends nicht überein."
            )
        merged = merged.drop(columns="reported_date_gt")

    if strict and merged["google_trends_attention"].isna().any():
        bad = merged.loc[
            merged["google_trends_attention"].isna(), "event_id"
        ].tolist()
        raise GoogleTrendsError(
            "Full-Sample-Google-Trends-Wert fehlt bei: " + ", ".join(bad)
        )
    return merged


def centered_copy(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    columns = [
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        "sentiment_finbert_management",
        "google_trends_attention",
        "google_trends_attention_exact8",
        "google_trends_pre_event_attention",
    ]
    means: dict[str, float] = {}
    for column in columns:
        if column not in frame.columns:
            continue
        mean = float(frame[column].mean())
        means[column] = mean
        frame[f"{column}_centered"] = frame[column] - mean
    frame.attrs["centering_means"] = means
    return frame


def formula_specs(dependent: str) -> list[dict[str, str]]:
    return [
        {
            "model": "01_existing_controls",
            "formula": (
                f"{dependent} ~ sue_winsor_1_99_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered + C(ticker)"
            ),
            "focus": "sentiment_finbert_management_centered",
        },
        {
            "model": "02_add_google_trends_full170",
            "formula": (
                f"{dependent} ~ sue_winsor_1_99_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered "
                "+ google_trends_attention_centered + C(ticker)"
            ),
            "focus": "google_trends_attention_centered",
        },
        {
            "model": "03_google_trends_exact8_165",
            "formula": (
                f"{dependent} ~ sue_winsor_1_99_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered "
                "+ google_trends_attention_exact8_centered + C(ticker)"
            ),
            "focus": "google_trends_attention_exact8_centered",
        },
        {
            "model": "04_sue_x_google_trends",
            "formula": (
                f"{dependent} ~ sue_winsor_1_99_centered "
                "* google_trends_attention_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered + C(ticker)"
            ),
            "focus": (
                "sue_winsor_1_99_centered:"
                "google_trends_attention_centered"
            ),
        },
        {
            "model": "05_pre_event_google_trends",
            "formula": (
                f"{dependent} ~ sue_winsor_1_99_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered "
                "+ google_trends_pre_event_attention_centered + C(ticker)"
            ),
            "focus": "google_trends_pre_event_attention_centered",
        },
        {
            "model": "06_absolute_car_both_attention",
            "formula": (
                f"abs_{dependent} ~ attention_abnormal_volume_centered "
                "+ google_trends_attention_centered "
                "+ sentiment_finbert_management_centered + C(ticker)"
            ),
            "focus": "google_trends_attention_centered",
        },
    ]


def formula_required_columns(formula: str) -> list[str]:
    candidates = [
        "sue_winsor_1_99_centered",
        "attention_abnormal_volume_centered",
        "sentiment_finbert_management_centered",
        "google_trends_attention_centered",
        "google_trends_attention_exact8_centered",
        "google_trends_pre_event_attention_centered",
    ]
    return [column for column in candidates if column in formula]


def fit_model(formula: str, data: pd.DataFrame, covariance: str):
    model = smf.ols(formula=formula, data=data)
    if covariance == "HC3":
        return model.fit(cov_type="HC3")
    if covariance == "cluster_event_day":
        return model.fit(
            cov_type="cluster",
            cov_kwds={
                "groups": data["event_trading_day"],
                "use_correction": True,
            },
        )
    raise ValueError(covariance)


def extract_rows(
    result,
    benchmark: str,
    window: str,
    model_name: str,
    formula: str,
    covariance: str,
    focus: str,
    cluster_count: int | None,
) -> list[dict[str, Any]]:
    confidence = result.conf_int()
    rows: list[dict[str, Any]] = []
    for term in result.params.index:
        rows.append(
            {
                "benchmark": benchmark.upper(),
                "dependent_variable": window,
                "model": model_name,
                "formula": formula,
                "covariance": covariance,
                "term": term,
                "is_focus_term": term == focus,
                "coefficient": float(result.params[term]),
                "standard_error": float(result.bse[term]),
                "test_statistic": float(result.tvalues[term]),
                "p_value": float(result.pvalues[term]),
                "ci_95_low": float(confidence.loc[term, 0]),
                "ci_95_high": float(confidence.loc[term, 1]),
                "n_obs": int(result.nobs),
                "r_squared": float(result.rsquared),
                "adj_r_squared": float(result.rsquared_adj),
                "aic": float(result.aic),
                "bic": float(result.bic),
                "event_day_cluster_count": cluster_count,
            }
        )
    return rows


def run_regressions(master: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    data = centered_copy(master)
    for benchmark in BENCHMARKS:
        for window in CAR_WINDOWS:
            column = f"{window}_{benchmark}"
            if column in data.columns:
                data[f"abs_{column}"] = data[column].abs()

    rows: list[dict[str, Any]] = []
    summaries: list[str] = []
    cluster_count = int(data["event_trading_day"].nunique())

    for benchmark in BENCHMARKS:
        for window in CAR_WINDOWS:
            dependent = f"{window}_{benchmark}"
            if dependent not in data.columns:
                continue
            for spec in formula_specs(dependent):
                required = formula_required_columns(spec["formula"])
                model_data = data.dropna(subset=[dependent, *required]).copy()
                if len(model_data) < 100:
                    logging.warning(
                        "%s %s %s: nur %d Beobachtungen; übersprungen.",
                        benchmark,
                        window,
                        spec["model"],
                        len(model_data),
                    )
                    continue
                for covariance in ("HC3", "cluster_event_day"):
                    result = fit_model(spec["formula"], model_data, covariance)
                    rows.extend(
                        extract_rows(
                            result=result,
                            benchmark=benchmark,
                            window=window,
                            model_name=spec["model"],
                            formula=spec["formula"],
                            covariance=covariance,
                            focus=spec["focus"],
                            cluster_count=(
                                cluster_count
                                if covariance == "cluster_event_day"
                                else None
                            ),
                        )
                    )
                    summaries.extend(
                        [
                            "=" * 112,
                            f"Benchmark: {benchmark.upper()}",
                            f"Abhängige Variable: {dependent}",
                            f"Modell: {spec['model']}",
                            f"Formel: {spec['formula']}",
                            f"Kovarianz: {covariance}",
                            str(result.summary()),
                            "",
                        ]
                    )
    return pd.DataFrame(rows), "\n".join(summaries)


def make_correlations(master: pd.DataFrame) -> pd.DataFrame:
    variables = [
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        "sentiment_finbert_management",
        "google_trends_attention",
        "google_trends_attention_exact8",
        "google_trends_pre_event_attention",
    ]
    variables += [
        f"{window}_{benchmark}"
        for window in CAR_WINDOWS
        for benchmark in BENCHMARKS
        if f"{window}_{benchmark}" in master.columns
    ]
    correlation = master[variables].corr(method="pearson")
    rows: list[dict[str, Any]] = []
    for variable_1 in correlation.index:
        for variable_2 in correlation.columns:
            rows.append(
                {
                    "variable_1": variable_1,
                    "variable_2": variable_2,
                    "pearson_correlation": float(
                        correlation.loc[variable_1, variable_2]
                    ),
                }
            )
    return pd.DataFrame(rows)


def create_distribution_plot(event_attention: pd.DataFrame, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9, 6))
    for ticker, group in event_attention.groupby("ticker"):
        ax.hist(
            group["google_trends_attention"],
            bins=12,
            alpha=0.45,
            label=ticker,
        )
    ax.axvline(0, linewidth=1)
    ax.set_xlabel("Google-Trends-Aufmerksamkeit (Log-Veränderung)")
    ax.set_ylabel("Anzahl Events")
    ax.set_title("Verteilung der eventbezogenen Google-Trends-Aufmerksamkeit")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def create_weekly_plot(weekly: pd.DataFrame, ticker: str, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 5.5))
    data = weekly.loc[weekly["ticker"] == ticker].copy()
    for query_term, group in data.groupby("query_term"):
        ax.plot(group["week_start"], group["svi"], label=query_term)
    ax.set_xlabel("Woche")
    ax.set_ylabel("Google-Trends-SVI")
    ax.set_title(f"{ticker}: aufbereitete wöchentliche Suchintensität")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    try:
        if args.baseline_weeks < 2:
            raise GoogleTrendsError("--baseline-weeks muss mindestens 2 sein.")
        if not 2 <= args.minimum_fallback_weeks <= args.baseline_weeks:
            raise GoogleTrendsError(
                "--minimum-fallback-weeks muss zwischen 2 und "
                "--baseline-weeks liegen."
            )
        if args.floor_value <= 0:
            raise GoogleTrendsError("--floor-value muss positiv sein.")

        raw_dir = Path(args.raw_dir)
        master_path = Path(args.master_file)
        output_dir = Path(args.output_dir)
        configure_logging(output_dir)

        if not raw_dir.exists():
            raise GoogleTrendsError(f"Rohdatenordner nicht gefunden: {raw_dir}")

        weekly, stitch_diagnostics, source_paths = load_weekly_series(
            raw_dir=raw_dir,
            floor_value=args.floor_value,
        )
        master = load_master(master_path)
        event_attention = construct_event_attention(
            master=master,
            weekly=weekly,
            baseline_weeks=args.baseline_weeks,
            minimum_fallback_weeks=args.minimum_fallback_weeks,
            floor_value=args.floor_value,
        )
        merged = merge_master(master, event_attention, args.strict)

        regression_results, regression_summaries = run_regressions(merged)
        focus_results = regression_results.loc[
            regression_results["is_focus_term"]
        ].sort_values(
            [
                "benchmark",
                "dependent_variable",
                "model",
                "covariance",
            ]
        )
        correlations = make_correlations(merged)

        weekly_path = output_dir / "google_trends_weekly_processed.csv"
        stitch_path = output_dir / "google_trends_stitch_diagnostics.csv"
        event_path = output_dir / "google_trends_event_attention.csv"
        master_output = output_dir / "event_analysis_master_with_google_trends.csv"
        regression_path = output_dir / "google_trends_regression_results.csv"
        focus_path = output_dir / "google_trends_model_focus_results.csv"
        summaries_path = output_dir / "google_trends_regression_summaries.txt"
        correlations_path = output_dir / "google_trends_correlations.csv"
        diagnostics_path = output_dir / "google_trends_event_diagnostics.csv"
        metadata_path = output_dir / "google_trends_metadata.json"

        weekly.to_csv(weekly_path, index=False, encoding="utf-8-sig")
        stitch_diagnostics.to_csv(
            stitch_path, index=False, encoding="utf-8-sig"
        )
        event_attention.to_csv(event_path, index=False, encoding="utf-8-sig")
        merged.to_csv(master_output, index=False, encoding="utf-8-sig")
        regression_results.to_csv(
            regression_path, index=False, encoding="utf-8-sig"
        )
        focus_results.to_csv(focus_path, index=False, encoding="utf-8-sig")
        summaries_path.write_text(regression_summaries, encoding="utf-8")
        correlations.to_csv(
            correlations_path, index=False, encoding="utf-8-sig"
        )

        diagnostics = event_attention[
            [
                "event_id",
                "ticker",
                "reported_date",
                "google_trends_query_term",
                "google_trends_baseline_weeks_available",
                "google_trends_uses_fallback_baseline",
                "google_trends_full_sample_usable",
                "google_trends_exact8_usable",
            ]
        ].copy()
        diagnostics["status"] = np.select(
            [
                ~diagnostics["google_trends_full_sample_usable"],
                diagnostics["google_trends_uses_fallback_baseline"],
            ],
            ["error", "warning"],
            default="ok",
        )
        diagnostics["issues"] = np.select(
            [
                ~diagnostics["google_trends_full_sample_usable"],
                diagnostics["google_trends_uses_fallback_baseline"],
            ],
            [
                "missing_full_sample_attention",
                "fallback_baseline_fewer_than_8_weeks",
            ],
            default="",
        )
        diagnostics.to_csv(
            diagnostics_path, index=False, encoding="utf-8-sig"
        )

        plot_dir = output_dir / "plots"
        plot_dir.mkdir(exist_ok=True)
        create_distribution_plot(
            event_attention,
            plot_dir / "google_trends_attention_distribution.png",
        )
        for ticker in sorted(master["ticker"].unique()):
            create_weekly_plot(
                weekly,
                ticker,
                plot_dir / f"{ticker}_weekly_google_trends.png",
            )

        fallback_count = int(
            event_attention["google_trends_uses_fallback_baseline"].sum()
        )
        exact_count = int(
            event_attention["google_trends_exact8_usable"].sum()
        )
        full_count = int(
            event_attention["google_trends_full_sample_usable"].sum()
        )
        primary = event_attention["google_trends_attention"]

        metadata = {
            "created_at_utc": utc_now(),
            "script": "integrate_google_trends.py",
            "script_version": SCRIPT_VERSION,
            "raw_directory": str(raw_dir),
            "master_file": str(master_path),
            "master_file_sha256": sha256_file(master_path),
            "source_files": [
                {
                    "path": str(path),
                    "sha256": sha256_file(path),
                }
                for path in source_paths
            ],
            "events_input": int(len(master)),
            "events_with_full_sample_attention": full_count,
            "events_with_exact_8_week_attention": exact_count,
            "events_using_4_to_7_week_fallback": fallback_count,
            "baseline_weeks_target": int(args.baseline_weeks),
            "minimum_fallback_weeks": int(args.minimum_fallback_weeks),
            "less_than_one_encoding": float(args.floor_value),
            "event_week_definition": (
                "Sunday-starting Google Trends week containing reported_date"
            ),
            "primary_variable": "google_trends_attention",
            "primary_definition": (
                "log(event-week SVI) minus log(median SVI of up to 8 "
                "previous weeks; minimum 4 weeks for five earliest events)"
            ),
            "strict_robustness_variable": (
                "google_trends_attention_exact8"
            ),
            "pre_event_variable": "google_trends_pre_event_attention",
            "primary_summary": {
                "count": int(primary.count()),
                "mean": float(primary.mean()),
                "std": float(primary.std(ddof=1)),
                "min": float(primary.min()),
                "q25": float(primary.quantile(0.25)),
                "median": float(primary.median()),
                "q75": float(primary.quantile(0.75)),
                "max": float(primary.max()),
            },
            "same_query_stitching": (
                "Second export scaled to first using median overlap ratio; "
                "overlap weeks averaged after scaling."
            ),
            "meta_query_rule": (
                "Facebook stock for reports on or before 2021-10-28; "
                "Meta stock afterwards. Query-specific baselines are used."
            ),
            "methodological_notes": [
                (
                    "Google Trends values are relative indices, not absolute "
                    "search counts."
                ),
                (
                    "The log ratio to a firm- and query-specific prior median "
                    "is invariant to constant rescaling of a series."
                ),
                (
                    "The event-week measure may include searches after the "
                    "earnings release and is therefore an attention proxy, "
                    "not a predetermined causal treatment."
                ),
                (
                    "The first five 2018 events have only four prior weeks; "
                    "the exact-eight-week robustness sample contains 165 events."
                ),
            ],
            "outputs": {
                "weekly": str(weekly_path),
                "event_attention": str(event_path),
                "master": str(master_output),
                "regressions": str(regression_path),
                "focus_results": str(focus_path),
                "diagnostics": str(diagnostics_path),
            },
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logging.info("Google-Trends-Rohdateien: %d", len(source_paths))
        logging.info("Events integriert: %d", len(merged))
        logging.info("Full-Sample-Werte: %d", full_count)
        logging.info("Strenge 8-Wochen-Werte: %d", exact_count)
        logging.info("Fallbackfälle: %d", fallback_count)
        logging.info("Regressionskoeffizienten: %d", len(regression_results))
        logging.info("Masterdatei: %s", master_output)

        print("\n" + "=" * 92)
        print("GOOGLE TRENDS: INTEGRATION UND ANALYSE")
        print("=" * 92)
        print(f"Rohdateien:                         {len(source_paths)}")
        print(f"Events im Masterdatensatz:          {len(master)}")
        print(f"Full-Sample-Aufmerksamkeit:         {full_count}")
        print(f"Strenge 8-Wochen-Aufmerksamkeit:    {exact_count}")
        print(f"Fallback mit 4-7 Vorwochen:         {fallback_count}")
        print(f"Regressionskoeffizienten:           {len(regression_results)}")
        print(f"Hauptvariable:                      google_trends_attention")
        print(f"Ausgabeordner:                      {output_dir.resolve()}")
        return 0

    except GoogleTrendsError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
