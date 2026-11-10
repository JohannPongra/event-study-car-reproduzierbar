#!/usr/bin/env python3
"""
Schritt 11: FinBERT-Sentiment in den bisherigen Eventdatensatz integrieren
und vorläufige Regressionsmodelle schätzen.

Eingaben
--------
1. volume_regression_results/event_car_volume_merged.csv
   340 Zeilen: 170 Events × SPY/QQQ
2. finbert_results/finbert_event_sentiment.csv
   170 Zeilen: eine Zeile je Event
3. optional finbert_results/finbert_event_diagnostics.csv

Ausgaben
--------
- event_analysis_master_long.csv     (340 Zeilen)
- event_analysis_master_wide.csv     (170 Zeilen)
- sentiment_regression_results.csv
- sentiment_model_focus_results.csv
- sentiment_regression_summaries.txt
- sentiment_correlations.csv
- integration_diagnostics.csv
- sentiment_integration_metadata.json
- plots/

Hauptmodell
-----------
CAR = b0 + b1*SUE + b2*Attention + b3*Sentiment
      + Unternehmens-Fixed-Effects + Fehler

Robustheit
----------
- rohe und winsorisierte SUE
- gesamtes Management-Sentiment
- Q&A-Sentiment als vollständig vergleichbare Alternative
- Prepared-Remarks-Sentiment auf der verfügbaren Teilstichprobe
- SUE×Aufmerksamkeit
- SPY und QQQ
- HC3 sowie Eventtag-Clusterung
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf


SCRIPT_VERSION = "1.0.0"
CAR_COLUMNS = [
    "car_0_0",
    "car_0_p1",
    "car_m1_p1",
    "car_m2_p2",
    "car_m5_p5",
]
SENTIMENT_COLUMNS = [
    "sentiment_finbert_management",
    "sentiment_finbert_call_ex_operator",
    "sentiment_finbert_management_prepared",
    "sentiment_finbert_management_qa",
    "sentiment_finbert_analyst",
    "sentiment_prepared_minus_qa",
    "sentiment_management_minus_analyst",
]


class IntegrationError(RuntimeError):
    """Fehler bei Integration oder Regressionsanalyse."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "FinBERT-Sentiment mit CAR, SUE und abnormalem Volumen "
            "verbinden."
        )
    )
    parser.add_argument(
        "--event-file",
        default=(
            "volume_regression_results/"
            "event_car_volume_merged.csv"
        ),
        help="Bisheriger Eventdatensatz mit CAR und Volumen.",
    )
    parser.add_argument(
        "--sentiment-file",
        default="finbert_results/finbert_event_sentiment.csv",
        help="FinBERT-Eventdatei.",
    )
    parser.add_argument(
        "--diagnostics-file",
        default="finbert_results/finbert_event_diagnostics.csv",
        help="Optionale FinBERT-Diagnosedatei.",
    )
    parser.add_argument(
        "--output-dir",
        default="sentiment_integration_results",
        help="Ausgabeordner.",
    )
    parser.add_argument(
        "--main-window",
        default="car_0_p1",
        choices=CAR_COLUMNS,
        help="CAR-Fenster für Grafiken.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Bei unvollständiger Integration abbrechen.",
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
                output_dir / "run_sentiment_integration.log",
                encoding="utf-8",
            ),
        ],
        force=True,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        raise IntegrationError(f"{label} nicht gefunden: {path}")
    return pd.read_csv(path, encoding="utf-8-sig")


def validate_event_data(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "event_id",
        "ticker",
        "event_trading_day",
        "benchmark",
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        *CAR_COLUMNS,
    }
    missing = required - set(frame.columns)
    if missing:
        raise IntegrationError(
            "Fehlende Spalten im Eventdatensatz: "
            + ", ".join(sorted(missing))
        )

    frame = frame.copy()
    frame["event_id"] = frame["event_id"].astype(str).str.strip()
    frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()
    frame["benchmark"] = (
        frame["benchmark"].astype(str).str.strip().str.upper()
    )
    frame["event_trading_day"] = pd.to_datetime(
        frame["event_trading_day"], errors="coerce"
    )

    numeric = [
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        *CAR_COLUMNS,
    ]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if frame["event_trading_day"].isna().any():
        raise IntegrationError("Ungültige Eventhandelstage.")
    if frame.duplicated(["event_id", "benchmark"]).any():
        raise IntegrationError(
            "Doppelte Event-Benchmark-Kombinationen."
        )
    if frame[numeric].isna().any().any():
        raise IntegrationError(
            "Fehlende numerische Werte im Eventdatensatz."
        )
    return frame


def validate_sentiment(frame: pd.DataFrame) -> pd.DataFrame:
    required = {
        "event_id",
        "ticker",
        "sentiment_finbert_management",
        "sentiment_finbert_management_qa",
    }
    missing = required - set(frame.columns)
    if missing:
        raise IntegrationError(
            "Fehlende Sentimentspalten: "
            + ", ".join(sorted(missing))
        )

    frame = frame.copy()
    frame["event_id"] = frame["event_id"].astype(str).str.strip()
    frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()

    if frame["event_id"].duplicated().any():
        raise IntegrationError(
            "Doppelte Event-IDs in der Sentimentdatei."
        )

    available_sentiment = [
        column
        for column in SENTIMENT_COLUMNS
        if column in frame.columns
    ]
    for column in available_sentiment:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if frame["sentiment_finbert_management"].isna().any():
        raise IntegrationError(
            "Fehlende Management-Sentimentwerte."
        )
    if frame["sentiment_finbert_management_qa"].isna().any():
        raise IntegrationError(
            "Fehlende Management-Q&A-Sentimentwerte."
        )

    out_of_range = (
        frame[available_sentiment]
        .dropna()
        .abs()
        .gt(1 + 1e-8)
        .any()
        .any()
    )
    if out_of_range:
        raise IntegrationError(
            "Mindestens ein Sentimentwert liegt außerhalb [-1,1]."
        )

    keep = [
        "event_id",
        "ticker",
        *available_sentiment,
    ]
    for column in [
        "management_all_prob_positive",
        "management_all_prob_negative",
        "management_all_prob_neutral",
        "management_all_token_count",
        "management_prepared_token_count",
        "management_qa_token_count",
        "analyst_qa_token_count",
    ]:
        if column in frame.columns:
            keep.append(column)
    return frame[keep]


def load_optional_diagnostics(path: Path) -> pd.DataFrame:
    if not path.exists():
        logging.warning(
            "Diagnosedatei nicht gefunden; Integration läuft ohne "
            "Warnungskennzeichen weiter: %s",
            path,
        )
        return pd.DataFrame(columns=["event_id"])

    diagnostics = pd.read_csv(path, encoding="utf-8-sig")
    if "event_id" not in diagnostics.columns:
        logging.warning(
            "Diagnosedatei enthält keine event_id und wird ignoriert."
        )
        return pd.DataFrame(columns=["event_id"])

    keep = ["event_id"]
    rename = {}
    for column in ["status", "issues", "section_method"]:
        if column in diagnostics.columns:
            keep.append(column)
            rename[column] = f"finbert_{column}"

    diagnostics = diagnostics[keep].copy()
    diagnostics["event_id"] = (
        diagnostics["event_id"].astype(str).str.strip()
    )
    return diagnostics.rename(columns=rename)


def merge_data(
    event_data: pd.DataFrame,
    sentiment: pd.DataFrame,
    diagnostics: pd.DataFrame,
    strict: bool,
) -> pd.DataFrame:
    merged = event_data.merge(
        sentiment,
        on="event_id",
        how="left",
        suffixes=("", "_sentiment"),
        validate="many_to_one",
        indicator=True,
    )

    missing_events = merged.loc[
        merged["_merge"] != "both", "event_id"
    ].unique()
    if len(missing_events):
        message = (
            f"{len(missing_events)} Events ohne Sentiment: "
            + ", ".join(missing_events[:10])
        )
        if strict:
            raise IntegrationError(message)
        logging.warning(message)

    merged = merged.loc[
        merged["_merge"] == "both"
    ].drop(columns="_merge")

    if "ticker_sentiment" in merged.columns:
        mismatch = (
            merged["ticker"] != merged["ticker_sentiment"]
        )
        if mismatch.any():
            raise IntegrationError(
                "Ticker stimmen zwischen Event- und "
                "Sentimentdatei nicht überein."
            )
        merged = merged.drop(columns="ticker_sentiment")

    if not diagnostics.empty:
        merged = merged.merge(
            diagnostics,
            on="event_id",
            how="left",
            validate="many_to_one",
        )

    return merged


def center_variables(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()

    # Mittelwerte nur einmal je Event bestimmen, nicht doppelt durch
    # SPY und QQQ gewichten.
    event_unique = frame.drop_duplicates("event_id")

    center_columns = [
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        "sentiment_finbert_management",
        "sentiment_finbert_management_qa",
    ]
    if (
        "sentiment_finbert_management_prepared"
        in frame.columns
    ):
        center_columns.append(
            "sentiment_finbert_management_prepared"
        )

    centering_means: dict[str, float] = {}
    for column in center_columns:
        mean = float(event_unique[column].mean())
        centering_means[column] = mean
        frame[f"{column}_centered"] = frame[column] - mean

    frame["sue_attention_interaction"] = (
        frame["sue_winsor_1_99_centered"]
        * frame["attention_abnormal_volume_centered"]
    )

    for car_column in CAR_COLUMNS:
        frame[f"abs_{car_column}"] = frame[car_column].abs()

    frame.attrs["centering_means"] = centering_means
    return frame


def make_wide_master(frame: pd.DataFrame) -> pd.DataFrame:
    shared_candidates = [
        "event_id",
        "ticker",
        "company_name",
        "reported_date",
        "reported_year",
        "reported_quarter",
        "event_trading_day",
        "reported_eps",
        "estimated_eps",
        "surprise_calculated",
        "surprise_percentage_calculated",
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        "standardized_abnormal_volume_mean_robust_0_p1",
        "volume_ratio_mean_0_0",
        "volume_ratio_mean_0_p1",
        *SENTIMENT_COLUMNS,
        "finbert_status",
        "finbert_issues",
        "finbert_section_method",
    ]
    shared = [
        column
        for column in shared_candidates
        if column in frame.columns
    ]

    base = (
        frame[shared]
        .drop_duplicates("event_id")
        .sort_values(["reported_date", "ticker"])
    )

    value_columns = [
        *CAR_COLUMNS,
        "alpha",
        "beta",
        "r_squared",
        "residual_std",
    ]
    value_columns = [
        column for column in value_columns if column in frame.columns
    ]

    wide = frame.pivot(
        index="event_id",
        columns="benchmark",
        values=value_columns,
    )
    wide.columns = [
        f"{value}_{benchmark.lower()}"
        for value, benchmark in wide.columns
    ]
    wide = wide.reset_index()

    return base.merge(
        wide,
        on="event_id",
        how="left",
        validate="one_to_one",
    )


def model_specs(car_column: str) -> list[dict[str, str]]:
    specs = [
        {
            "model": "01_raw_sue",
            "formula": f"{car_column} ~ sue_centered",
            "focus": "sue_centered",
        },
        {
            "model": "02_raw_sue_attention",
            "formula": (
                f"{car_column} ~ sue_centered "
                "+ attention_abnormal_volume_centered"
            ),
            "focus": "attention_abnormal_volume_centered",
        },
        {
            "model": "03_full_management_fe",
            "formula": (
                f"{car_column} ~ sue_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered "
                "+ C(ticker)"
            ),
            "focus": "sentiment_finbert_management_centered",
        },
        {
            "model": "04_full_winsor_management_fe",
            "formula": (
                f"{car_column} ~ sue_winsor_1_99_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered "
                "+ C(ticker)"
            ),
            "focus": "sentiment_finbert_management_centered",
        },
        {
            "model": "05_interaction_management_fe",
            "formula": (
                f"{car_column} ~ "
                "sue_winsor_1_99_centered "
                "* attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered "
                "+ C(ticker)"
            ),
            "focus": (
                "sue_winsor_1_99_centered:"
                "attention_abnormal_volume_centered"
            ),
        },
        {
            "model": "06_qa_sentiment_fe",
            "formula": (
                f"{car_column} ~ sue_winsor_1_99_centered "
                "+ attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_qa_centered "
                "+ C(ticker)"
            ),
            "focus": "sentiment_finbert_management_qa_centered",
        },
        {
            "model": "07_abs_car_attention_sentiment_fe",
            "formula": (
                f"abs_{car_column} ~ "
                "attention_abnormal_volume_centered "
                "+ sentiment_finbert_management_centered "
                "+ C(ticker)"
            ),
            "focus": "attention_abnormal_volume_centered",
        },
    ]

    if car_column and True:
        specs.append(
            {
                "model": "08_prepared_sentiment_fe",
                "formula": (
                    f"{car_column} ~ sue_winsor_1_99_centered "
                    "+ attention_abnormal_volume_centered "
                    "+ sentiment_finbert_management_prepared_centered "
                    "+ C(ticker)"
                ),
                "focus": (
                    "sentiment_finbert_management_prepared_centered"
                ),
            }
        )
    return specs


def fit_model(
    formula: str,
    data: pd.DataFrame,
    covariance: str,
):
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


def required_formula_columns(formula: str) -> list[str]:
    columns = [
        "sue_centered",
        "sue_winsor_1_99_centered",
        "attention_abnormal_volume_centered",
        "sentiment_finbert_management_centered",
        "sentiment_finbert_management_qa_centered",
        "sentiment_finbert_management_prepared_centered",
    ]
    return [column for column in columns if column in formula]


def extract_result_rows(
    result,
    benchmark: str,
    dependent_variable: str,
    model_name: str,
    formula: str,
    covariance: str,
    focus: str,
    cluster_count: int | None,
) -> list[dict[str, Any]]:
    conf = result.conf_int()
    rows: list[dict[str, Any]] = []

    for term in result.params.index:
        rows.append(
            {
                "benchmark": benchmark,
                "dependent_variable": dependent_variable,
                "model": model_name,
                "formula": formula,
                "covariance": covariance,
                "term": term,
                "is_focus_term": term == focus,
                "coefficient": float(result.params[term]),
                "standard_error": float(result.bse[term]),
                "test_statistic": float(result.tvalues[term]),
                "p_value": float(result.pvalues[term]),
                "ci_95_low": float(conf.loc[term, 0]),
                "ci_95_high": float(conf.loc[term, 1]),
                "n_obs": int(result.nobs),
                "r_squared": float(result.rsquared),
                "adj_r_squared": float(result.rsquared_adj),
                "aic": float(result.aic),
                "bic": float(result.bic),
                "event_day_cluster_count": cluster_count,
            }
        )
    return rows


def run_regressions(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, str]:
    rows: list[dict[str, Any]] = []
    summaries: list[str] = []

    for benchmark in sorted(frame["benchmark"].unique()):
        benchmark_data = frame.loc[
            frame["benchmark"] == benchmark
        ].copy()
        cluster_count = int(
            benchmark_data["event_trading_day"].nunique()
        )

        for car_column in CAR_COLUMNS:
            for spec in model_specs(car_column):
                required = required_formula_columns(
                    spec["formula"]
                )
                data = benchmark_data.dropna(
                    subset=required
                ).copy()

                if len(data) < 100:
                    logging.warning(
                        "%s %s %s: nur %d Beobachtungen; "
                        "Modell übersprungen.",
                        benchmark,
                        car_column,
                        spec["model"],
                        len(data),
                    )
                    continue

                for covariance in [
                    "HC3",
                    "cluster_event_day",
                ]:
                    result = fit_model(
                        spec["formula"],
                        data,
                        covariance,
                    )
                    rows.extend(
                        extract_result_rows(
                            result=result,
                            benchmark=benchmark,
                            dependent_variable=car_column,
                            model_name=spec["model"],
                            formula=spec["formula"],
                            covariance=covariance,
                            focus=spec["focus"],
                            cluster_count=(
                                cluster_count
                                if covariance
                                == "cluster_event_day"
                                else None
                            ),
                        )
                    )

                    summaries.extend(
                        [
                            "=" * 110,
                            f"Benchmark: {benchmark}",
                            f"Abhängige Variable: {car_column}",
                            f"Modell: {spec['model']}",
                            f"Formel: {spec['formula']}",
                            f"Kovarianz: {covariance}",
                            str(result.summary()),
                            "",
                        ]
                    )

    return pd.DataFrame(rows), "\n".join(summaries)


def make_correlations(frame: pd.DataFrame) -> pd.DataFrame:
    variables = [
        "sue",
        "sue_winsor_1_99",
        "attention_abnormal_volume",
        *SENTIMENT_COLUMNS,
        *CAR_COLUMNS,
        *[f"abs_{column}" for column in CAR_COLUMNS],
    ]
    variables = [
        column for column in variables if column in frame.columns
    ]

    rows: list[dict[str, Any]] = []
    for benchmark in sorted(frame["benchmark"].unique()):
        correlation = (
            frame.loc[frame["benchmark"] == benchmark, variables]
            .corr(method="pearson")
        )
        for variable_1 in correlation.index:
            for variable_2 in correlation.columns:
                rows.append(
                    {
                        "benchmark": benchmark,
                        "variable_1": variable_1,
                        "variable_2": variable_2,
                        "pearson_correlation": float(
                            correlation.loc[
                                variable_1, variable_2
                            ]
                        ),
                    }
                )
    return pd.DataFrame(rows)


def make_integration_diagnostics(
    long_frame: pd.DataFrame,
    wide_frame: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    event_unique = long_frame.drop_duplicates("event_id")
    checks = {
        "unique_events": event_unique["event_id"].nunique(),
        "long_rows": len(long_frame),
        "wide_rows": len(wide_frame),
        "benchmarks": long_frame["benchmark"].nunique(),
        "missing_main_sentiment": int(
            event_unique[
                "sentiment_finbert_management"
            ].isna().sum()
        ),
        "missing_qa_sentiment": int(
            event_unique[
                "sentiment_finbert_management_qa"
            ].isna().sum()
        ),
        "missing_prepared_sentiment": int(
            event_unique[
                "sentiment_finbert_management_prepared"
            ].isna().sum()
            if "sentiment_finbert_management_prepared"
            in event_unique.columns
            else -1
        ),
        "sentiment_outside_minus1_plus1": int(
            (
                event_unique[
                    "sentiment_finbert_management"
                ].abs()
                > 1 + 1e-8
            ).sum()
        ),
        "event_benchmark_duplicates": int(
            long_frame.duplicated(
                ["event_id", "benchmark"]
            ).sum()
        ),
    }
    for check, value in checks.items():
        rows.append(
            {
                "check": check,
                "value": value,
                "status": (
                    "ok"
                    if check
                    not in {
                        "missing_prepared_sentiment"
                    }
                    and value == 0
                    or check
                    in {
                        "unique_events",
                        "long_rows",
                        "wide_rows",
                        "benchmarks",
                    }
                    else "info"
                ),
            }
        )
    return pd.DataFrame(rows)


def create_sentiment_distribution(
    frame: pd.DataFrame,
    output_path: Path,
) -> None:
    event_unique = frame.drop_duplicates("event_id")

    fig, ax = plt.subplots(figsize=(9, 6))
    for ticker, group in event_unique.groupby("ticker"):
        ax.hist(
            group["sentiment_finbert_management"],
            bins=12,
            alpha=0.45,
            label=ticker,
        )
    ax.set_xlabel("FinBERT-Management-Sentiment")
    ax.set_ylabel("Anzahl Events")
    ax.set_title("Verteilung des Management-Sentiments")
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def create_sentiment_car_scatter(
    frame: pd.DataFrame,
    benchmark: str,
    car_column: str,
    output_path: Path,
) -> None:
    data = frame.loc[
        frame["benchmark"] == benchmark
    ].copy()

    fig, ax = plt.subplots(figsize=(9, 6))
    ax.scatter(
        data["sentiment_finbert_management"],
        data[car_column],
        alpha=0.65,
    )

    coefficients = np.polyfit(
        data["sentiment_finbert_management"],
        data[car_column],
        deg=1,
    )
    x_values = np.linspace(
        data["sentiment_finbert_management"].min(),
        data["sentiment_finbert_management"].max(),
        150,
    )
    ax.plot(
        x_values,
        coefficients[0] * x_values + coefficients[1],
    )
    ax.axhline(0, linewidth=1)
    ax.set_xlabel("FinBERT-Management-Sentiment")
    ax.set_ylabel(car_column)
    ax.set_title(
        f"{benchmark}: Sentiment und {car_column}"
    )
    fig.tight_layout()
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def main() -> int:
    args = parse_args()

    try:
        event_path = Path(args.event_file)
        sentiment_path = Path(args.sentiment_file)
        diagnostics_path = Path(args.diagnostics_file)
        output_dir = Path(args.output_dir)
        configure_logging(output_dir)

        event_data = validate_event_data(
            read_csv(event_path, "Eventdatensatz")
        )
        sentiment = validate_sentiment(
            read_csv(sentiment_path, "Sentimentdatei")
        )
        diagnostics = load_optional_diagnostics(
            diagnostics_path
        )

        merged = merge_data(
            event_data,
            sentiment,
            diagnostics,
            args.strict,
        )
        merged = center_variables(merged)
        wide = make_wide_master(merged)

        event_count = int(merged["event_id"].nunique())
        benchmark_count = int(
            merged["benchmark"].nunique()
        )
        expected_long_rows = event_count * benchmark_count

        if args.strict:
            if event_count != 170:
                raise IntegrationError(
                    f"Erwartet wurden 170 Events, gefunden: "
                    f"{event_count}."
                )
            if len(merged) != expected_long_rows:
                raise IntegrationError(
                    f"Erwartet wurden {expected_long_rows} "
                    f"Langformat-Zeilen, gefunden: {len(merged)}."
                )
            if len(wide) != event_count:
                raise IntegrationError(
                    "Wide-Masterdatei enthält nicht genau eine "
                    "Zeile je Event."
                )

        regression_results, summaries = run_regressions(
            merged
        )
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
        integration_diagnostics = (
            make_integration_diagnostics(merged, wide)
        )

        long_path = (
            output_dir / "event_analysis_master_long.csv"
        )
        wide_path = (
            output_dir / "event_analysis_master_wide.csv"
        )
        regression_path = (
            output_dir / "sentiment_regression_results.csv"
        )
        focus_path = (
            output_dir / "sentiment_model_focus_results.csv"
        )
        summary_path = (
            output_dir / "sentiment_regression_summaries.txt"
        )
        correlation_path = (
            output_dir / "sentiment_correlations.csv"
        )
        integration_diag_path = (
            output_dir / "integration_diagnostics.csv"
        )
        metadata_path = (
            output_dir / "sentiment_integration_metadata.json"
        )

        merged.to_csv(
            long_path, index=False, encoding="utf-8-sig"
        )
        wide.to_csv(
            wide_path, index=False, encoding="utf-8-sig"
        )
        regression_results.to_csv(
            regression_path,
            index=False,
            encoding="utf-8-sig",
        )
        focus_results.to_csv(
            focus_path, index=False, encoding="utf-8-sig"
        )
        correlation_path.parent.mkdir(
            parents=True, exist_ok=True
        )
        correlations.to_csv(
            correlation_path,
            index=False,
            encoding="utf-8-sig",
        )
        integration_diagnostics.to_csv(
            integration_diag_path,
            index=False,
            encoding="utf-8-sig",
        )
        summary_path.write_text(
            summaries, encoding="utf-8"
        )

        plot_dir = output_dir / "plots"
        plot_dir.mkdir(exist_ok=True)
        create_sentiment_distribution(
            merged,
            plot_dir / "management_sentiment_distribution.png",
        )
        for benchmark in sorted(
            merged["benchmark"].unique()
        ):
            create_sentiment_car_scatter(
                merged,
                benchmark,
                args.main_window,
                plot_dir
                / (
                    f"{benchmark}_{args.main_window}"
                    "_sentiment_scatter.png"
                ),
            )

        event_unique = merged.drop_duplicates("event_id")
        main_sentiment = event_unique[
            "sentiment_finbert_management"
        ]
        warning_events = (
            int(
                event_unique["finbert_status"]
                .eq("warning")
                .sum()
            )
            if "finbert_status" in event_unique.columns
            else None
        )

        metadata = {
            "created_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "script": "integrate_finbert_sentiment.py",
            "script_version": SCRIPT_VERSION,
            "event_file": str(event_path),
            "event_file_sha256": sha256_file(event_path),
            "sentiment_file": str(sentiment_path),
            "sentiment_file_sha256": sha256_file(
                sentiment_path
            ),
            "diagnostics_file": (
                str(diagnostics_path)
                if diagnostics_path.exists()
                else None
            ),
            "events_integrated": event_count,
            "benchmarks": sorted(
                merged["benchmark"].unique().tolist()
            ),
            "long_rows": int(len(merged)),
            "wide_rows": int(len(wide)),
            "unique_event_trading_days": int(
                event_unique["event_trading_day"].nunique()
            ),
            "finbert_warning_events": warning_events,
            "main_sentiment_variable": (
                "sentiment_finbert_management"
            ),
            "uniform_qa_robustness_variable": (
                "sentiment_finbert_management_qa"
            ),
            "prepared_sample_size": int(
                event_unique[
                    "sentiment_finbert_management_prepared"
                ].notna().sum()
            )
            if (
                "sentiment_finbert_management_prepared"
                in event_unique.columns
            )
            else None,
            "centering_means": merged.attrs.get(
                "centering_means", {}
            ),
            "main_sentiment_summary": {
                "count": int(main_sentiment.count()),
                "mean": float(main_sentiment.mean()),
                "std": float(main_sentiment.std(ddof=1)),
                "min": float(main_sentiment.min()),
                "median": float(main_sentiment.median()),
                "max": float(main_sentiment.max()),
            },
            "covariance_estimators": [
                "HC3",
                "cluster_event_day",
            ],
            "methodological_notes": [
                (
                    "Management sentiment is the main FinBERT measure."
                ),
                (
                    "Management Q&A sentiment is reported as a "
                    "uniform robustness measure because nine early "
                    "Amazon calls contain no prepared management text."
                ),
                (
                    "Prepared-remarks models use the available "
                    "161-event subsample."
                ),
                (
                    "Firm fixed effects account for persistent "
                    "differences in corporate language."
                ),
                (
                    "Associations are not interpreted as unambiguous "
                    "causal effects."
                ),
            ],
        }
        metadata_path.write_text(
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        logging.info("Events integriert: %d", event_count)
        logging.info("Langformat-Zeilen: %d", len(merged))
        logging.info("Wide-Zeilen: %d", len(wide))
        logging.info(
            "Regressionskoeffizienten: %d",
            len(regression_results),
        )
        logging.info("Ausgabeordner: %s", output_dir.resolve())

        print("\n" + "=" * 90)
        print("SENTIMENT-INTEGRATION: ERGEBNIS")
        print("=" * 90)
        print(f"Events integriert:             {event_count}")
        print(f"Benchmarks:                    {benchmark_count}")
        print(f"Langformat-Zeilen:             {len(merged)}")
        print(f"Wide-Master-Zeilen:            {len(wide)}")
        print(
            "Management-Sentiment vorhanden: "
            f"{int(main_sentiment.notna().sum())}"
        )
        print(
            "Q&A-Sentiment vorhanden:        "
            f"{int(event_unique['sentiment_finbert_management_qa'].notna().sum())}"
        )
        if (
            "sentiment_finbert_management_prepared"
            in event_unique.columns
        ):
            print(
                "Prepared-Sentiment vorhanden:   "
                f"{int(event_unique['sentiment_finbert_management_prepared'].notna().sum())}"
            )
        print(
            f"Regressionskoeffizienten:       "
            f"{len(regression_results)}"
        )
        print(f"Ausgabeordner:                 {output_dir.resolve()}")

        return 0

    except IntegrationError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
