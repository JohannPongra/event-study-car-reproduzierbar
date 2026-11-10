#!/usr/bin/env python3
"""
Lädt historische Quartals-Earnings von Alpha Vantage und erzeugt einen
reproduzierbaren Event-Study-Datensatz mit:

- EPS-Überraschung (actual - estimate)
- prozentualer EPS-Überraschung
- Report-Time aus Alpha Vantage und transparenten manuellen Overrides
- Release-Klassifikation (BMO / AMC / DURING)
- korrektem NYSE-Eventhandelstag
- SUE auf Basis der Standardabweichung der vorherigen Quartalsüberraschungen

Standardstichprobe:
    AAPL AMZN GOOGL META MSFT
    reportedDate >= 2018-01-01

Installation:
    pip install -r requirements_alpha_vantage_earnings.txt

PowerShell:
    $env:ALPHA_VANTAGE_API_KEY="DEIN_API_KEY"
    python fetch_alpha_vantage_earnings.py

Wichtig:
- Liegen alle Rohdateien bereits unter <output-dir>/raw, ist kein API-Key nötig.
- Standardmäßig werden für SUE die 8 vorherigen gültigen Quartale verwendet.
- Manuelle Zeitkorrekturen stehen in report_time_overrides.csv und bleiben
  dadurch vollständig nachvollziehbar.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas_market_calendars as mcal
import requests


API_URL = "https://www.alphavantage.co/query"
DEFAULT_TICKERS = ["AAPL", "AMZN", "GOOGL", "META", "MSFT"]
DEFAULT_START_DATE = "2018-01-01"
DEFAULT_TARGET_TOTAL = 180
DEFAULT_SUE_WINDOW = 8

COMPANY_NAMES = {
    "AAPL": "Apple Inc.",
    "AMZN": "Amazon.com, Inc.",
    "GOOGL": "Alphabet Inc.",
    "GOOG": "Alphabet Inc.",
    "META": "Meta Platforms, Inc.",
    "MSFT": "Microsoft Corporation",
    "NVDA": "NVIDIA Corporation",
}

# Diese Einträge sind bewusst zusätzlich im Skript hinterlegt. So funktioniert
# die Korrektur auch dann, wenn die CSV-Datei versehentlich nicht mitkopiert
# wurde. Die CSV wird dennoch automatisch erzeugt und bleibt die sichtbare,
# editierbare Dokumentation der Overrides.
DEFAULT_REPORT_TIME_OVERRIDES = [
    {
        "ticker": "MSFT",
        "reported_date": "2021-07-27",
        "report_time": "post-market",
        "verified": "true",
        "source_note": "Manuell anhand Microsoft Investor Relations korrigiert",
    },
    {
        "ticker": "MSFT",
        "reported_date": "2022-07-26",
        "report_time": "post-market",
        "verified": "true",
        "source_note": "Manuell anhand Microsoft Investor Relations korrigiert",
    },
    {
        "ticker": "MSFT",
        "reported_date": "2023-07-25",
        "report_time": "post-market",
        "verified": "true",
        "source_note": "Manuell anhand Microsoft Investor Relations korrigiert",
    },
    {
        "ticker": "AMZN",
        "reported_date": "2025-02-06",
        "report_time": "post-market",
        "verified": "true",
        "source_note": "Manuell anhand Amazon Investor Relations korrigiert",
    },
]

BASE_FIELDS = [
    "event_id",
    "ticker",
    "company_name",
    "fiscal_date_ending",
    "reported_date",
    "reported_year",
    "reported_calendar_quarter",
    "reported_eps",
    "estimated_eps",
    "surprise_api",
    "surprise_percentage_api",
    "surprise_calculated",
    "surprise_percentage_calculated",
    "surprise_difference",
    "has_reported_eps",
    "has_estimated_eps",
    "analysis_ready",
    "validation_status",
    "source",
    "retrieved_at_utc",
]

EVENT_FIELDS = [
    *BASE_FIELDS,
    "report_time_api",
    "report_time_final",
    "report_time_source",
    "report_time_verified",
    "report_time_note",
    "release_timing",
    "event_trading_day",
    "event_day_rule",
    "sue_history_window",
    "sue_history_n",
    "sue_history_mean",
    "sue_history_std",
    "sue",
    "has_sue",
    "has_event_trading_day",
    "event_study_ready",
]

SUMMARY_FIELDS = [
    "ticker",
    "company_name",
    "all_rows_since_start",
    "analysis_ready_rows",
    "sue_ready_rows",
    "event_day_ready_rows",
    "event_study_ready_rows",
    "overrides_applied",
    "missing_reported_eps",
    "missing_estimated_eps",
    "api_validation_warnings",
    "earliest_reported_date",
    "latest_reported_date",
]

OVERRIDE_FIELDS = [
    "ticker",
    "reported_date",
    "report_time",
    "verified",
    "source_note",
]

REVIEW_FIELDS = [
    "event_id",
    "ticker",
    "reported_date",
    "report_time_api",
    "report_time_final",
    "report_time_source",
    "report_time_verified",
    "report_time_note",
    "release_timing",
    "event_trading_day",
    "event_day_rule",
]


class AlphaVantageError(RuntimeError):
    """Fehler bei Abruf oder Verarbeitung der Earnings-Daten."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Alpha-Vantage-Earnings laden und Event-Study-Masterdatei "
            "mit Report-Time, NYSE-Eventtag und SUE erzeugen."
        )
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=DEFAULT_TICKERS,
        help="Tickerliste, z. B. AAPL AMZN GOOGL META MSFT",
    )
    parser.add_argument(
        "--start-date",
        default=DEFAULT_START_DATE,
        help="Frühestes reportedDate (YYYY-MM-DD); Standard: 2018-01-01",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Optionales spätestes reportedDate (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--target-total",
        type=int,
        default=DEFAULT_TARGET_TOTAL,
        help="Nur Kontrollwert; Standard: 180",
    )
    parser.add_argument(
        "--strict-target",
        action="store_true",
        help="Fehlercode 2, wenn target-total nicht exakt erreicht wird.",
    )
    parser.add_argument(
        "--sue-window",
        type=int,
        default=DEFAULT_SUE_WINDOW,
        help="Zahl vorheriger Quartale für SUE; Standard: 8",
    )
    parser.add_argument(
        "--output-dir",
        default="alpha_vantage_earnings",
        help="Ausgabeordner; Standard: alpha_vantage_earnings",
    )
    parser.add_argument(
        "--overrides-file",
        default=None,
        help=(
            "Optionale CSV mit Report-Time-Korrekturen. Ohne Angabe wird "
            "<output-dir>/report_time_overrides.csv verwendet."
        ),
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Optionaler API-Key; bevorzugt ALPHA_VANTAGE_API_KEY verwenden.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Pause zwischen neuen API-Anfragen in Sekunden; Standard: 1.0",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP-Timeout in Sekunden; Standard: 30",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Maximale Anzahl Versuche je Ticker; Standard: 4",
    )
    parser.add_argument(
        "--overwrite-raw",
        action="store_true",
        help="Vorhandene Roh-JSON-Dateien erneut herunterladen.",
    )
    return parser.parse_args()


def configure_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "fetch_earnings.log"

    # Verhindert doppelte Handler bei Import/erneutem Aufruf in derselben Session.
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    root.addHandler(file_handler)


def validate_iso_date(value: str | None, field_name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AlphaVantageError(
            f"{field_name} muss im Format YYYY-MM-DD angegeben werden: {value!r}"
        ) from exc


def normalize_tickers(values: Iterable[str]) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for value in values:
        ticker = value.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    if not tickers:
        raise AlphaVantageError("Es wurde kein gültiger Ticker angegeben.")
    return tickers


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "-", "n/a"}:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_float(value: float | None, decimals: int = 10) -> str:
    if value is None:
        return ""
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "ja"}


def calendar_quarter(d: date) -> str:
    quarter = ((d.month - 1) // 3) + 1
    return f"{d.year}Q{quarter}"


def normalize_report_time(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "pre-market": "pre-market",
        "premarket": "pre-market",
        "before-market": "pre-market",
        "before market": "pre-market",
        "bmo": "pre-market",
        "post-market": "post-market",
        "postmarket": "post-market",
        "after-market": "post-market",
        "after market": "post-market",
        "after-hours": "post-market",
        "amc": "post-market",
        "during-market": "during-market",
        "during market": "during-market",
        "market-hours": "during-market",
        "during": "during-market",
    }
    return aliases.get(text, "")


def release_timing_from_report_time(report_time: str) -> str:
    return {
        "pre-market": "BMO",
        "post-market": "AMC",
        "during-market": "DURING",
    }.get(report_time, "UNKNOWN")


def write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ensure_default_overrides_file(path: Path) -> None:
    if path.exists():
        return
    write_csv(path, DEFAULT_REPORT_TIME_OVERRIDES, OVERRIDE_FIELDS)
    logging.info("Standard-Override-Datei erzeugt: %s", path)


def load_report_time_overrides(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    ensure_default_overrides_file(path)
    overrides: dict[tuple[str, str], dict[str, str]] = {}

    try:
        with path.open("r", newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            missing = set(OVERRIDE_FIELDS) - set(reader.fieldnames or [])
            if missing:
                raise AlphaVantageError(
                    f"Override-Datei {path} enthält nicht alle Spalten: "
                    + ", ".join(sorted(missing))
                )
            for line_no, row in enumerate(reader, start=2):
                ticker = str(row.get("ticker") or "").strip().upper()
                reported_date = str(row.get("reported_date") or "").strip()
                report_time = normalize_report_time(row.get("report_time"))
                if not ticker and not reported_date and not report_time:
                    continue
                if not ticker or not reported_date or not report_time:
                    logging.warning(
                        "Ungültige Override-Zeile %d in %s übersprungen.",
                        line_no,
                        path,
                    )
                    continue
                validate_iso_date(reported_date, f"Override-Zeile {line_no}")
                overrides[(ticker, reported_date)] = {
                    "report_time": report_time,
                    "verified": str(parse_bool(row.get("verified"))).lower(),
                    "source_note": str(row.get("source_note") or "").strip(),
                }
    except OSError as exc:
        raise AlphaVantageError(f"Override-Datei konnte nicht gelesen werden: {path}") from exc

    # Eingebaute Defaults dienen als Fallback, externe CSV hat Vorrang.
    for row in DEFAULT_REPORT_TIME_OVERRIDES:
        key = (row["ticker"], row["reported_date"])
        overrides.setdefault(
            key,
            {
                "report_time": row["report_time"],
                "verified": row["verified"],
                "source_note": row["source_note"],
            },
        )
    return overrides


def fetch_json(
    session: requests.Session,
    ticker: str,
    api_key: str,
    timeout: float,
    retries: int,
) -> dict[str, Any]:
    params = {"function": "EARNINGS", "symbol": ticker, "apikey": api_key}
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(API_URL, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise AlphaVantageError(
                    f"{ticker}: Unerwartetes Antwortformat: {type(payload).__name__}"
                )
            if "Error Message" in payload:
                raise AlphaVantageError(
                    f"{ticker}: Alpha Vantage meldet: {payload['Error Message']}"
                )
            if "Note" in payload or "Information" in payload:
                message = payload.get("Note") or payload.get("Information")
                raise AlphaVantageError(f"{ticker}: API-Hinweis: {message}")
            if not isinstance(payload.get("quarterlyEarnings"), list):
                keys = ", ".join(sorted(payload.keys())) or "<keine>"
                raise AlphaVantageError(
                    f"{ticker}: quarterlyEarnings fehlt. Antwortfelder: {keys}"
                )
            return payload
        except (requests.RequestException, ValueError, AlphaVantageError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            wait_seconds = min(60.0, 2.0**attempt)
            logging.warning(
                "%s: Versuch %d/%d fehlgeschlagen: %s. Neuer Versuch in %.1f s.",
                ticker,
                attempt,
                retries,
                exc,
                wait_seconds,
            )
            time.sleep(wait_seconds)

    raise AlphaVantageError(
        f"{ticker}: Abruf nach {retries} Versuchen fehlgeschlagen: {last_error}"
    )


def load_or_fetch_ticker(
    session: requests.Session,
    ticker: str,
    api_key: str | None,
    raw_dir: Path,
    overwrite_raw: bool,
    timeout: float,
    retries: int,
) -> tuple[dict[str, Any], bool]:
    raw_path = raw_dir / f"{ticker}_earnings_raw.json"

    if raw_path.exists() and not overwrite_raw:
        logging.info("%s: Verwende vorhandene Rohdatei %s", ticker, raw_path)
        try:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AlphaVantageError(
                f"{ticker}: Rohdatei konnte nicht gelesen werden: {raw_path}"
            ) from exc
        if not isinstance(payload.get("quarterlyEarnings"), list):
            raise AlphaVantageError(
                f"{ticker}: Ungültige Rohdatei; quarterlyEarnings fehlt: {raw_path}"
            )
        return payload, False

    if not api_key:
        raise AlphaVantageError(
            f"{ticker}: Rohdatei fehlt ({raw_path}) und kein API-Key ist gesetzt."
        )

    logging.info("%s: Lade EARNINGS von Alpha Vantage ...", ticker)
    payload = fetch_json(session, ticker, api_key, timeout, retries)
    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logging.info("%s: Rohantwort gespeichert: %s", ticker, raw_path)
    return payload, True


def transform_all_quarterly_history(
    ticker: str,
    payload: dict[str, Any],
    retrieved_at_utc: str,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    rows: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    quarterly_rows = payload.get("quarterlyEarnings", [])

    for source_row in quarterly_rows:
        if not isinstance(source_row, dict):
            counters["invalid_source_row"] += 1
            continue

        reported_date_text = str(source_row.get("reportedDate") or "").strip()
        if not reported_date_text:
            counters["missing_reported_date"] += 1
            continue
        try:
            reported_date = date.fromisoformat(reported_date_text)
        except ValueError:
            counters["invalid_reported_date"] += 1
            logging.warning(
                "%s: Ungültiges reportedDate übersprungen: %r",
                ticker,
                reported_date_text,
            )
            continue

        reported_eps = safe_float(source_row.get("reportedEPS"))
        estimated_eps = safe_float(source_row.get("estimatedEPS"))
        surprise_api = safe_float(source_row.get("surprise"))
        surprise_pct_api = safe_float(source_row.get("surprisePercentage"))

        surprise_calculated: float | None = None
        surprise_pct_calculated: float | None = None
        surprise_difference: float | None = None
        validation_messages: list[str] = []

        if reported_eps is not None and estimated_eps is not None:
            surprise_calculated = reported_eps - estimated_eps
            if abs(estimated_eps) > 1e-12:
                surprise_pct_calculated = surprise_calculated / abs(estimated_eps) * 100.0
            else:
                validation_messages.append("estimated_eps_zero")

        if surprise_api is not None and surprise_calculated is not None:
            surprise_difference = surprise_api - surprise_calculated
            if abs(surprise_difference) > 0.011:
                validation_messages.append("api_surprise_mismatch")

        has_reported_eps = reported_eps is not None
        has_estimated_eps = estimated_eps is not None
        analysis_ready = has_reported_eps and has_estimated_eps

        if not has_reported_eps:
            counters["missing_reported_eps"] += 1
            validation_messages.append("missing_reported_eps")
        if not has_estimated_eps:
            counters["missing_estimated_eps"] += 1
            validation_messages.append("missing_estimated_eps")
        if validation_messages:
            counters["api_validation_warnings"] += 1

        rows.append(
            {
                "event_id": f"{ticker}_{reported_date.isoformat()}",
                "ticker": ticker,
                "company_name": COMPANY_NAMES.get(ticker, ""),
                "fiscal_date_ending": str(
                    source_row.get("fiscalDateEnding") or ""
                ).strip(),
                "reported_date": reported_date.isoformat(),
                "reported_year": str(reported_date.year),
                "reported_calendar_quarter": calendar_quarter(reported_date),
                "reported_eps": reported_eps,
                "estimated_eps": estimated_eps,
                "surprise_api": surprise_api,
                "surprise_percentage_api": surprise_pct_api,
                "surprise_calculated": surprise_calculated,
                "surprise_percentage_calculated": surprise_pct_calculated,
                "surprise_difference": surprise_difference,
                "has_reported_eps": has_reported_eps,
                "has_estimated_eps": has_estimated_eps,
                "analysis_ready": analysis_ready,
                "validation_status": (
                    "ok" if not validation_messages else ";".join(validation_messages)
                ),
                "source": "Alpha Vantage EARNINGS",
                "retrieved_at_utc": retrieved_at_utc,
                "report_time_api": normalize_report_time(source_row.get("reportTime")),
            }
        )

    rows.sort(key=lambda row: row["reported_date"])
    return rows, counters


def add_sue_to_history(rows: list[dict[str, Any]], window: int) -> None:
    """Berechnet SUE nur mit Daten, die vor dem jeweiligen Event lagen."""
    previous_surprises: list[float] = []

    for row in sorted(rows, key=lambda item: item["reported_date"]):
        history = previous_surprises[-window:]
        history_n = len(history)
        history_mean: float | None = None
        history_std: float | None = None
        sue: float | None = None

        if history_n > 0:
            history_mean = statistics.fmean(history)
        if history_n >= window and history_n >= 2:
            history_std = statistics.stdev(history)
            current = row.get("surprise_calculated")
            if current is not None and history_std > 1e-12:
                sue = current / history_std

        row["sue_history_window"] = window
        row["sue_history_n"] = history_n
        row["sue_history_mean"] = history_mean
        row["sue_history_std"] = history_std
        row["sue"] = sue
        row["has_sue"] = sue is not None

        current_surprise = row.get("surprise_calculated")
        if current_surprise is not None:
            previous_surprises.append(float(current_surprise))


def build_nyse_sessions(min_date: date, max_date: date) -> list[date]:
    # Sicherheitsrand, damit AMC am letzten Berichtstag sicher den Folgetag findet.
    calendar = mcal.get_calendar("NYSE")
    schedule = calendar.schedule(
        start_date=(min_date - timedelta(days=10)).isoformat(),
        end_date=(max_date + timedelta(days=20)).isoformat(),
    )
    return sorted(timestamp.date() for timestamp in schedule.index)


def first_session_on_or_after(sessions: Sequence[date], d: date) -> date | None:
    for session_date in sessions:
        if session_date >= d:
            return session_date
    return None


def first_session_after(sessions: Sequence[date], d: date) -> date | None:
    for session_date in sessions:
        if session_date > d:
            return session_date
    return None


def apply_report_times_and_event_days(
    rows: list[dict[str, Any]],
    overrides: dict[tuple[str, str], dict[str, str]],
    sessions: Sequence[date],
) -> None:
    for row in rows:
        key = (row["ticker"], row["reported_date"])
        api_time = normalize_report_time(row.get("report_time_api"))
        override = overrides.get(key)

        if override:
            final_time = override["report_time"]
            source = "override"
            verified = override.get("verified", "false")
            note = override.get("source_note", "")
        elif api_time:
            final_time = api_time
            source = "alpha_vantage"
            verified = "false"
            note = ""
        else:
            final_time = ""
            source = "unknown"
            verified = "false"
            note = "Keine verwertbare Report-Time vorhanden"

        timing = release_timing_from_report_time(final_time)
        report_date = date.fromisoformat(row["reported_date"])
        event_day: date | None = None
        event_rule = ""

        if timing == "AMC":
            event_day = first_session_after(sessions, report_date)
            event_rule = "first_nyse_session_strictly_after_reported_date"
        elif timing in {"BMO", "DURING"}:
            event_day = first_session_on_or_after(sessions, report_date)
            event_rule = "first_nyse_session_on_or_after_reported_date"
        else:
            event_rule = "unknown_report_time_no_event_day"

        row["report_time_api"] = api_time
        row["report_time_final"] = final_time
        row["report_time_source"] = source
        row["report_time_verified"] = verified
        row["report_time_note"] = note
        row["release_timing"] = timing
        row["event_trading_day"] = event_day.isoformat() if event_day else ""
        row["event_day_rule"] = event_rule
        row["has_event_trading_day"] = event_day is not None
        row["event_study_ready"] = bool(
            row.get("analysis_ready") and row.get("has_sue") and event_day is not None
        )


def remove_duplicate_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item["reported_date"], item["ticker"])):
        event_id = str(row["event_id"])
        if event_id in seen:
            logging.warning("Doppeltes Event verworfen: %s", event_id)
            continue
        seen.add(event_id)
        unique.append(row)
    return unique


def serialize_row(row: dict[str, Any], fields: Sequence[str]) -> dict[str, str]:
    float_fields = {
        "reported_eps",
        "estimated_eps",
        "surprise_api",
        "surprise_percentage_api",
        "surprise_calculated",
        "surprise_percentage_calculated",
        "surprise_difference",
        "sue_history_mean",
        "sue_history_std",
        "sue",
    }
    bool_fields = {
        "has_reported_eps",
        "has_estimated_eps",
        "analysis_ready",
        "has_sue",
        "has_event_trading_day",
        "event_study_ready",
    }

    output: dict[str, str] = {}
    for field in fields:
        value = row.get(field, "")
        if field in float_fields:
            output[field] = format_float(value if isinstance(value, (int, float)) else None)
        elif field in bool_fields:
            output[field] = str(bool(value)).lower()
        elif value is None:
            output[field] = ""
        else:
            output[field] = str(value)
    return output


def create_summary(
    tickers: list[str],
    rows: list[dict[str, Any]],
    counters_by_ticker: dict[str, Counter[str]],
) -> list[dict[str, str]]:
    summary: list[dict[str, str]] = []
    for ticker in tickers:
        ticker_rows = [row for row in rows if row["ticker"] == ticker]
        dates = sorted(str(row["reported_date"]) for row in ticker_rows)
        counters = counters_by_ticker.get(ticker, Counter())
        summary.append(
            {
                "ticker": ticker,
                "company_name": COMPANY_NAMES.get(ticker, ""),
                "all_rows_since_start": str(len(ticker_rows)),
                "analysis_ready_rows": str(sum(bool(r["analysis_ready"]) for r in ticker_rows)),
                "sue_ready_rows": str(sum(bool(r["has_sue"]) for r in ticker_rows)),
                "event_day_ready_rows": str(
                    sum(bool(r["has_event_trading_day"]) for r in ticker_rows)
                ),
                "event_study_ready_rows": str(
                    sum(bool(r["event_study_ready"]) for r in ticker_rows)
                ),
                "overrides_applied": str(
                    sum(r["report_time_source"] == "override" for r in ticker_rows)
                ),
                "missing_reported_eps": str(counters["missing_reported_eps"]),
                "missing_estimated_eps": str(counters["missing_estimated_eps"]),
                "api_validation_warnings": str(counters["api_validation_warnings"]),
                "earliest_reported_date": dates[0] if dates else "",
                "latest_reported_date": dates[-1] if dates else "",
            }
        )
    return summary


def write_metadata(
    path: Path,
    *,
    tickers: list[str],
    start_date: date,
    end_date: date | None,
    target_total: int,
    sue_window: int,
    rows: list[dict[str, Any]],
    retrieved_at_utc: str,
    overrides_file: Path,
) -> None:
    metadata = {
        "created_at_utc": retrieved_at_utc,
        "source": "Alpha Vantage EARNINGS",
        "filter_date_field": "reportedDate",
        "start_date_inclusive": start_date.isoformat(),
        "end_date_inclusive": end_date.isoformat() if end_date else None,
        "tickers": tickers,
        "target_total": target_total,
        "all_reports_count": len(rows),
        "analysis_ready_count": sum(bool(r["analysis_ready"]) for r in rows),
        "sue_ready_count": sum(bool(r["has_sue"]) for r in rows),
        "event_day_ready_count": sum(bool(r["has_event_trading_day"]) for r in rows),
        "event_study_ready_count": sum(bool(r["event_study_ready"]) for r in rows),
        "target_difference_all_reports": len(rows) - target_total,
        "sue_definition": "current_unexpected_earnings / sample_std(previous_valid_unexpected_earnings)",
        "sue_window": sue_window,
        "nyse_calendar": "pandas_market_calendars:NYSE",
        "report_time_overrides_file": str(overrides_file),
        "overrides_applied": sum(r["report_time_source"] == "override" for r in rows),
        "notes": [
            "Die Stichprobe wird nach reportedDate abgegrenzt.",
            "AMC-Ereignisse erhalten den ersten NYSE-Handelstag nach dem Veröffentlichungsdatum.",
            "BMO- und DURING-Ereignisse erhalten den ersten NYSE-Handelstag am oder nach dem Veröffentlichungsdatum.",
            "SUE nutzt ausschließlich vor dem jeweiligen Event verfügbare Quartalsüberraschungen.",
            "Report-Time-Overrides bleiben in einer separaten CSV nachvollziehbar.",
        ],
    }
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def print_result_summary(
    summary_rows: list[dict[str, str]],
    rows: list[dict[str, Any]],
    target_total: int,
    output_dir: Path,
) -> None:
    all_count = len(rows)
    analysis_count = sum(bool(r["analysis_ready"]) for r in rows)
    sue_count = sum(bool(r["has_sue"]) for r in rows)
    event_day_count = sum(bool(r["has_event_trading_day"]) for r in rows)
    ready_count = sum(bool(r["event_study_ready"]) for r in rows)
    override_count = sum(r["report_time_source"] == "override" for r in rows)

    print("\n" + "=" * 96)
    print("ALPHA-VANTAGE-EARNINGS: EVENT-STUDY-DATENSATZ")
    print("=" * 96)
    print(
        f"{'Ticker':<9}{'Alle':>7}{'EPS-ready':>12}{'SUE-ready':>12}"
        f"{'Eventtag':>12}{'Final-ready':>14}{'Overrides':>11}"
    )
    print("-" * 96)
    for row in summary_rows:
        print(
            f"{row['ticker']:<9}{row['all_rows_since_start']:>7}"
            f"{row['analysis_ready_rows']:>12}{row['sue_ready_rows']:>12}"
            f"{row['event_day_ready_rows']:>12}{row['event_study_ready_rows']:>14}"
            f"{row['overrides_applied']:>11}"
        )
    print("-" * 96)
    print(f"Alle Berichte:                 {all_count}")
    print(f"EPS-analysebereit:             {analysis_count}")
    print(f"SUE vorhanden:                 {sue_count}")
    print(f"Eventhandelstag vorhanden:     {event_day_count}")
    print(f"Event-Study-bereit:            {ready_count}")
    print(f"Angewandte Zeit-Overrides:     {override_count}")
    print(f"Zielgröße:                     {target_total}")
    print(f"Differenz zum Ziel:            {all_count - target_total:+d}")
    print(f"Ausgabeordner:                 {output_dir.resolve()}")


def main() -> int:
    args = parse_args()

    try:
        tickers = normalize_tickers(args.tickers)
        start_date = validate_iso_date(args.start_date, "--start-date")
        end_date = validate_iso_date(args.end_date, "--end-date")
        assert start_date is not None

        if end_date is not None and end_date < start_date:
            raise AlphaVantageError("--end-date darf nicht vor --start-date liegen.")
        if args.target_total < 1:
            raise AlphaVantageError("--target-total muss positiv sein.")
        if args.sue_window < 2:
            raise AlphaVantageError("--sue-window muss mindestens 2 sein.")
        if args.delay < 0:
            raise AlphaVantageError("--delay darf nicht negativ sein.")
        if args.retries < 1:
            raise AlphaVantageError("--retries muss mindestens 1 sein.")

        output_dir = Path(args.output_dir)
        raw_dir = output_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(output_dir)

        overrides_file = (
            Path(args.overrides_file)
            if args.overrides_file
            else output_dir / "report_time_overrides.csv"
        )
        overrides_file.parent.mkdir(parents=True, exist_ok=True)
        overrides = load_report_time_overrides(overrides_file)

        api_key = (
            args.api_key
            or os.getenv("ALPHA_VANTAGE_API_KEY")
            or os.getenv("ALPHAVANTAGE_API_KEY")
        )

        logging.info("Ticker: %s", ", ".join(tickers))
        logging.info("Stichprobe: reportedDate >= %s", start_date)
        if end_date:
            logging.info("Stichprobe: reportedDate <= %s", end_date)
        logging.info("SUE-Fenster: %d vorherige Quartale", args.sue_window)

        retrieved_at_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        all_history_by_ticker: dict[str, list[dict[str, Any]]] = {}
        counters_by_ticker: dict[str, Counter[str]] = {}
        new_requests = 0

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Johann-Pongratz-W-Seminar-Event-Study/2.0 "
                    "(academic research)"
                ),
                "Accept": "application/json",
            }
        )

        try:
            for index, ticker in enumerate(tickers, start=1):
                logging.info("Verarbeite %d/%d: %s", index, len(tickers), ticker)
                payload, downloaded = load_or_fetch_ticker(
                    session=session,
                    ticker=ticker,
                    api_key=api_key,
                    raw_dir=raw_dir,
                    overwrite_raw=args.overwrite_raw,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                if downloaded:
                    new_requests += 1

                history_rows, counters = transform_all_quarterly_history(
                    ticker, payload, retrieved_at_utc
                )
                add_sue_to_history(history_rows, args.sue_window)
                all_history_by_ticker[ticker] = history_rows
                counters_by_ticker[ticker] = counters
                logging.info(
                    "%s: %d historische Quartale für SUE verfügbar.",
                    ticker,
                    len(history_rows),
                )
                if downloaded and index < len(tickers) and args.delay > 0:
                    time.sleep(args.delay)
        finally:
            session.close()

        study_rows: list[dict[str, Any]] = []
        for ticker in tickers:
            for row in all_history_by_ticker[ticker]:
                reported = date.fromisoformat(row["reported_date"])
                if reported < start_date:
                    continue
                if end_date is not None and reported > end_date:
                    continue
                study_rows.append(row)

        study_rows = remove_duplicate_events(study_rows)
        if not study_rows:
            raise AlphaVantageError("Nach der Datumsfilterung sind keine Events übrig.")

        study_dates = [date.fromisoformat(row["reported_date"]) for row in study_rows]
        sessions = build_nyse_sessions(min(study_dates), max(study_dates))
        apply_report_times_and_event_days(study_rows, overrides, sessions)

        analysis_rows = [row for row in study_rows if row["analysis_ready"]]
        event_ready_rows = [row for row in study_rows if row["event_study_ready"]]

        serialized_all = [serialize_row(row, EVENT_FIELDS) for row in study_rows]
        serialized_analysis = [serialize_row(row, EVENT_FIELDS) for row in analysis_rows]
        serialized_event_ready = [serialize_row(row, EVENT_FIELDS) for row in event_ready_rows]

        # events_master und earnings_all_since_start sind bewusst identisch aufgebaut.
        # Dadurch gibt es nur noch ein einheitliches Schema für alle Folgeprogramme.
        write_csv(output_dir / "events_master.csv", serialized_all, EVENT_FIELDS)
        write_csv(
            output_dir / "earnings_all_since_start.csv", serialized_all, EVENT_FIELDS
        )
        write_csv(
            output_dir / "earnings_analysis_ready.csv",
            serialized_analysis,
            EVENT_FIELDS,
        )
        write_csv(
            output_dir / "event_study_ready.csv",
            serialized_event_ready,
            EVENT_FIELDS,
        )

        summary_rows = create_summary(tickers, study_rows, counters_by_ticker)
        write_csv(
            output_dir / "earnings_summary_by_ticker.csv",
            summary_rows,
            SUMMARY_FIELDS,
        )

        review_rows = [serialize_row(row, REVIEW_FIELDS) for row in study_rows]
        write_csv(output_dir / "report_time_review.csv", review_rows, REVIEW_FIELDS)

        write_metadata(
            output_dir / "earnings_metadata.json",
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            target_total=args.target_total,
            sue_window=args.sue_window,
            rows=study_rows,
            retrieved_at_utc=retrieved_at_utc,
            overrides_file=overrides_file,
        )

        logging.info("Neue API-Anfragen in diesem Lauf: %d", new_requests)
        logging.info("Event-Masterdatei: %s", output_dir / "events_master.csv")
        logging.info("Event-Study-ready: %s", output_dir / "event_study_ready.csv")

        print_result_summary(summary_rows, study_rows, args.target_total, output_dir)

        if args.strict_target and len(study_rows) != args.target_total:
            logging.error(
                "Strenge Zielprüfung fehlgeschlagen: %d statt %d Berichte.",
                len(study_rows),
                args.target_total,
            )
            return 2
        return 0

    except AlphaVantageError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
