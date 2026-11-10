#!/usr/bin/env python3
"""
Lädt tägliche historische Kurs- und Volumendaten für eine Earnings-Event-Study.

Standardworkflow:
- liest events_master.csv bzw. event_study_ready.csv
- lädt die fünf Aktien aus der Eventdatei
- lädt SPY als Hauptmarktbenchmark und QQQ als Robustheitsbenchmark
- verwendet explizit split- und dividendenbereinigte Schlusskurse (Adj Close)
- berechnet einfache und logarithmische Tagesrenditen
- prüft für jedes Event die Abdeckung des Schätz- und Ereignisfensters
- speichert Rohdaten und aufbereitete Dateien getrennt

Installation:
    pip install -r requirements_market_data.txt

Aufruf:
    python fetch_market_data.py --events-file alpha_vantage_earnings/events_master.csv

Strenge Abdeckungsprüfung:
    python fetch_market_data.py \
        --events-file alpha_vantage_earnings/events_master.csv \
        --strict-coverage

Hinweis:
Yahoo Finance wird über das Open-Source-Paket yfinance abgerufen. Die Dateien
werden lokal archiviert, damit die konkrete Datengrundlage reproduzierbar bleibt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


DEFAULT_MAIN_BENCHMARK = "SPY"
DEFAULT_ROBUSTNESS_BENCHMARKS = ["QQQ"]
DEFAULT_ESTIMATION_START = -250
DEFAULT_ESTIMATION_END = -30
DEFAULT_EVENT_WINDOW_START = -5
DEFAULT_EVENT_WINDOW_END = 5
DEFAULT_CALENDAR_BUFFER_DAYS = 550
DEFAULT_POST_EVENT_BUFFER_DAYS = 20

MASTER_FIELDS = [
    "date",
    "symbol",
    "role",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividends",
    "stock_splits",
    "simple_return",
    "log_return",
    "dollar_volume",
    "log_volume",
    "source",
    "retrieved_at_utc",
]


class MarketDataError(RuntimeError):
    """Fehler bei Kursabruf, Verarbeitung oder Validierung."""


@dataclass(frozen=True)
class WindowConfig:
    estimation_start: int
    estimation_end: int
    event_start: int
    event_end: int

    @property
    def estimation_required(self) -> int:
        return self.estimation_end - self.estimation_start + 1

    @property
    def event_required(self) -> int:
        return self.event_end - self.event_start + 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Tägliche bereinigte Kurse, Volumen und Renditen für eine "
            "Earnings-Event-Study herunterladen."
        )
    )
    parser.add_argument(
        "--events-file",
        default="alpha_vantage_earnings/events_master.csv",
        help="Eventdatei mit ticker und event_trading_day.",
    )
    parser.add_argument(
        "--output-dir",
        default="market_data",
        help="Ausgabeordner; Standard: market_data",
    )
    parser.add_argument(
        "--main-benchmark",
        default=DEFAULT_MAIN_BENCHMARK,
        help="Hauptbenchmark für das Marktmodell; Standard: SPY",
    )
    parser.add_argument(
        "--robustness-benchmarks",
        nargs="*",
        default=DEFAULT_ROBUSTNESS_BENCHMARKS,
        help="Zusätzliche Benchmarks; Standard: QQQ",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Optionaler manueller Downloadbeginn YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help=(
            "Optionales manuelles Downloadende YYYY-MM-DD, inklusive. "
            "Ohne Angabe wird es aus den Events abgeleitet."
        ),
    )
    parser.add_argument(
        "--calendar-buffer-days",
        type=int,
        default=DEFAULT_CALENDAR_BUFFER_DAYS,
        help=(
            "Kalendertage vor dem frühesten Event; Standard: 550. "
            "Dies deckt das Schätzfenster [-250,-30] mit Reserve ab."
        ),
    )
    parser.add_argument(
        "--post-event-buffer-days",
        type=int,
        default=DEFAULT_POST_EVENT_BUFFER_DAYS,
        help="Kalendertage nach dem letzten Event; Standard: 20.",
    )
    parser.add_argument(
        "--estimation-start",
        type=int,
        default=DEFAULT_ESTIMATION_START,
        help="Beginn des Schätzfensters in Handelstagen; Standard: -250.",
    )
    parser.add_argument(
        "--estimation-end",
        type=int,
        default=DEFAULT_ESTIMATION_END,
        help="Ende des Schätzfensters in Handelstagen; Standard: -30.",
    )
    parser.add_argument(
        "--event-window-start",
        type=int,
        default=DEFAULT_EVENT_WINDOW_START,
        help="Früheste zu prüfende Eventfenstergrenze; Standard: -5.",
    )
    parser.add_argument(
        "--event-window-end",
        type=int,
        default=DEFAULT_EVENT_WINDOW_END,
        help="Späteste zu prüfende Eventfenstergrenze; Standard: +5.",
    )
    parser.add_argument(
        "--overwrite-raw",
        action="store_true",
        help="Vorhandene Roh-CSV-Dateien erneut herunterladen.",
    )
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help=(
            "Mit Fehlercode 2 beenden, wenn nicht jedes Event vollständig "
            "durch Aktien- und Benchmarkrenditen abgedeckt ist."
        ),
    )
    return parser.parse_args()


def configure_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "fetch_market_data.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )


def parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise MarketDataError(
            f"{field_name} muss im Format YYYY-MM-DD vorliegen: {value!r}"
        ) from exc


def normalize_symbols(values: Iterable[str]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value).strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def safe_filename(symbol: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", symbol).strip("_")
    return cleaned or "symbol"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_events(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise MarketDataError(f"Eventdatei nicht gefunden: {path}")

    try:
        events = pd.read_csv(path)
    except Exception as exc:
        raise MarketDataError(f"Eventdatei konnte nicht gelesen werden: {path}") from exc

    required = {"event_id", "ticker", "event_trading_day"}
    missing = sorted(required - set(events.columns))
    if missing:
        raise MarketDataError(
            "Eventdatei enthält nicht alle benötigten Spalten: " + ", ".join(missing)
        )

    events = events.copy()
    events["ticker"] = events["ticker"].astype(str).str.strip().str.upper()
    events["event_trading_day"] = pd.to_datetime(
        events["event_trading_day"], errors="coerce"
    ).dt.normalize()

    if events["event_id"].duplicated().any():
        duplicates = events.loc[events["event_id"].duplicated(), "event_id"].tolist()
        raise MarketDataError(
            f"Doppelte event_id in der Eventdatei, Beispiele: {duplicates[:5]}"
        )

    if events["event_trading_day"].isna().any():
        bad = events.loc[events["event_trading_day"].isna(), "event_id"].tolist()
        raise MarketDataError(
            f"Ungültige oder fehlende Eventhandelstage, Beispiele: {bad[:5]}"
        )

    if (events["ticker"] == "").any():
        raise MarketDataError("Mindestens ein Event besitzt keinen gültigen Ticker.")

    return events.sort_values(["event_trading_day", "ticker"]).reset_index(drop=True)


def derive_download_range(
    events: pd.DataFrame,
    start_override: str | None,
    end_override: str | None,
    calendar_buffer_days: int,
    post_event_buffer_days: int,
) -> tuple[date, date]:
    earliest = events["event_trading_day"].min().date()
    latest = events["event_trading_day"].max().date()

    start = (
        parse_iso_date(start_override, "--start-date")
        if start_override
        else earliest - timedelta(days=calendar_buffer_days)
    )
    end = (
        parse_iso_date(end_override, "--end-date")
        if end_override
        else latest + timedelta(days=post_event_buffer_days)
    )

    if end <= start:
        raise MarketDataError("Das Downloadende muss nach dem Downloadbeginn liegen.")
    if start >= earliest:
        logging.warning(
            "Downloadbeginn %s liegt nicht vor dem frühesten Event %s.", start, earliest
        )
    if end < latest:
        logging.warning(
            "Downloadende %s liegt vor dem letzten Event %s.", end, latest
        )
    return start, end


def validate_windows(config: WindowConfig) -> None:
    if config.estimation_start > config.estimation_end:
        raise MarketDataError("estimation-start darf nicht nach estimation-end liegen.")
    if config.estimation_end >= 0:
        raise MarketDataError("Das Schätzfenster muss vor dem Eventtag enden.")
    if config.event_start > config.event_end:
        raise MarketDataError("event-window-start darf nicht nach event-window-end liegen.")
    if not (config.event_start <= 0 <= config.event_end):
        raise MarketDataError("Das geprüfte Eventfenster muss den Eventtag 0 enthalten.")


def normalize_yfinance_frame(frame: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if frame.empty:
        raise MarketDataError(f"{symbol}: Yahoo Finance lieferte keine Kursdaten.")

    data = frame.copy()
    if isinstance(data.columns, pd.MultiIndex):
        levels = [set(map(str, data.columns.get_level_values(i))) for i in range(data.columns.nlevels)]
        selected = None
        for level_index, values in enumerate(levels):
            if symbol in values:
                selected = data.xs(symbol, axis=1, level=level_index, drop_level=True)
                break
        if selected is not None:
            data = selected
        else:
            data.columns = [str(col[0]) for col in data.columns]

    data.columns = [str(column).strip() for column in data.columns]
    data.index = pd.to_datetime(data.index, errors="coerce")
    if getattr(data.index, "tz", None) is not None:
        data.index = data.index.tz_localize(None)
    data.index = data.index.normalize()
    data = data.loc[~data.index.isna()].copy()
    data = data.loc[~data.index.duplicated(keep="last")].sort_index()

    required = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise MarketDataError(
            f"{symbol}: Erwartete yfinance-Spalten fehlen: {', '.join(missing)}. "
            "Das Skript setzt auto_adjust=False ausdrücklich voraus."
        )

    if "Dividends" not in data.columns:
        data["Dividends"] = 0.0
    if "Stock Splits" not in data.columns:
        data["Stock Splits"] = 0.0

    rename = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Volume": "volume",
        "Dividends": "dividends",
        "Stock Splits": "stock_splits",
    }
    data = data.rename(columns=rename)[list(rename.values())]
    for column in data.columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")

    data = data.dropna(subset=["close", "adj_close"]).copy()
    if data.empty:
        raise MarketDataError(f"{symbol}: Keine gültigen Schlusskurse vorhanden.")

    if (data["adj_close"] <= 0).any():
        raise MarketDataError(f"{symbol}: Nichtpositive bereinigte Schlusskurse gefunden.")
    if (data["volume"].dropna() < 0).any():
        raise MarketDataError(f"{symbol}: Negative Handelsvolumina gefunden.")

    data.index.name = "date"
    return data


def download_symbol(symbol: str, start: date, end_inclusive: date) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise MarketDataError(
            "Das Paket yfinance ist nicht installiert. "
            "Führe zuerst 'pip install -r requirements_market_data.txt' aus."
        ) from exc

    # yfinance behandelt end als exklusiv.
    end_exclusive = end_inclusive + timedelta(days=1)
    logging.info("%s: Download %s bis %s", symbol, start, end_inclusive)
    try:
        raw = yf.download(
            tickers=symbol,
            start=start.isoformat(),
            end=end_exclusive.isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=True,
            progress=False,
            threads=False,
            group_by="column",
            timeout=30,
        )
    except Exception as exc:
        raise MarketDataError(f"{symbol}: Kursabruf fehlgeschlagen: {exc}") from exc
    return normalize_yfinance_frame(raw, symbol)


def write_raw_csv(path: Path, data: pd.DataFrame) -> None:
    out = data.reset_index().copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    out.to_csv(path, index=False, encoding="utf-8-sig")


def read_raw_csv(path: Path, symbol: str) -> pd.DataFrame:
    try:
        data = pd.read_csv(path)
    except Exception as exc:
        raise MarketDataError(f"{symbol}: Rohdatei konnte nicht gelesen werden: {path}") from exc

    required = {
        "date",
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "volume",
        "dividends",
        "stock_splits",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise MarketDataError(
            f"{symbol}: Rohdatei enthält nicht alle Spalten: {', '.join(missing)}"
        )

    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data = data.dropna(subset=["date"]).set_index("date").sort_index()
    return normalize_yfinance_frame(
        data.rename(
            columns={
                "open": "Open",
                "high": "High",
                "low": "Low",
                "close": "Close",
                "adj_close": "Adj Close",
                "volume": "Volume",
                "dividends": "Dividends",
                "stock_splits": "Stock Splits",
            }
        ),
        symbol,
    )


def enrich_symbol_data(
    data: pd.DataFrame,
    symbol: str,
    role: str,
    retrieved_at_utc: str,
) -> pd.DataFrame:
    out = data.copy()
    out["simple_return"] = out["adj_close"].pct_change(fill_method=None)
    out["log_return"] = np.log(out["adj_close"] / out["adj_close"].shift(1))
    out["dollar_volume"] = out["close"] * out["volume"]
    out["log_volume"] = np.where(
        out["volume"].fillna(0) > 0,
        np.log(out["volume"]),
        np.nan,
    )
    out["symbol"] = symbol
    out["role"] = role
    out["source"] = "Yahoo Finance via yfinance"
    out["retrieved_at_utc"] = retrieved_at_utc
    out = out.reset_index()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    return out[MASTER_FIELDS]


def build_wide(master: pd.DataFrame, value_column: str) -> pd.DataFrame:
    wide = master.pivot(index="date", columns="symbol", values=value_column)
    wide = wide.sort_index().reset_index()
    return wide


def window_available_count(
    merged: pd.DataFrame,
    event_position: int,
    start_offset: int,
    end_offset: int,
) -> int:
    start_position = event_position + start_offset
    end_position = event_position + end_offset
    if end_position < 0 or start_position >= len(merged):
        return 0

    clipped_start = max(0, start_position)
    clipped_end = min(len(merged) - 1, end_position)
    if clipped_start > clipped_end:
        return 0

    window = merged.iloc[clipped_start : clipped_end + 1]
    return int(window[["stock_return", "market_return"]].notna().all(axis=1).sum())


def build_event_coverage(
    events: pd.DataFrame,
    master: pd.DataFrame,
    main_benchmark: str,
    config: WindowConfig,
) -> pd.DataFrame:
    returns = master[["date", "symbol", "simple_return"]].copy()
    returns["date"] = pd.to_datetime(returns["date"])

    market = returns.loc[returns["symbol"] == main_benchmark, ["date", "simple_return"]]
    market = market.rename(columns={"simple_return": "market_return"})
    if market.empty:
        raise MarketDataError(
            f"Für den Hauptbenchmark {main_benchmark} fehlen Renditedaten."
        )

    records: list[dict[str, object]] = []
    for row in events.itertuples(index=False):
        ticker = str(row.ticker)
        event_day = pd.Timestamp(row.event_trading_day).normalize()
        stock = returns.loc[returns["symbol"] == ticker, ["date", "simple_return"]]
        stock = stock.rename(columns={"simple_return": "stock_return"})

        merged = stock.merge(market, on="date", how="outer").sort_values("date")
        merged = merged.drop_duplicates("date", keep="last").reset_index(drop=True)
        matching = merged.index[merged["date"] == event_day].tolist()
        event_day_present = bool(matching)

        estimation_available = 0
        event_window_available = 0
        if event_day_present:
            position = matching[0]
            estimation_available = window_available_count(
                merged,
                position,
                config.estimation_start,
                config.estimation_end,
            )
            event_window_available = window_available_count(
                merged,
                position,
                config.event_start,
                config.event_end,
            )

        estimation_complete = (
            estimation_available == config.estimation_required
        )
        event_window_complete = event_window_available == config.event_required
        coverage_ready = event_day_present and estimation_complete and event_window_complete

        records.append(
            {
                "event_id": row.event_id,
                "ticker": ticker,
                "event_trading_day": event_day.strftime("%Y-%m-%d"),
                "main_benchmark": main_benchmark,
                "event_day_present_in_stock_and_market": str(event_day_present).lower(),
                "estimation_window_start": config.estimation_start,
                "estimation_window_end": config.estimation_end,
                "estimation_observations_required": config.estimation_required,
                "estimation_paired_returns_available": estimation_available,
                "estimation_window_complete": str(estimation_complete).lower(),
                "event_window_start": config.event_start,
                "event_window_end": config.event_end,
                "event_observations_required": config.event_required,
                "event_paired_returns_available": event_window_available,
                "event_window_complete": str(event_window_complete).lower(),
                "price_coverage_ready": str(coverage_ready).lower(),
            }
        )

    return pd.DataFrame(records)


def build_symbol_summary(master: pd.DataFrame) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for symbol, group in master.groupby("symbol", sort=True):
        group = group.sort_values("date")
        records.append(
            {
                "symbol": symbol,
                "role": group["role"].iloc[0],
                "rows": len(group),
                "first_date": group["date"].iloc[0],
                "last_date": group["date"].iloc[-1],
                "nonmissing_adj_close": int(group["adj_close"].notna().sum()),
                "nonmissing_returns": int(group["simple_return"].notna().sum()),
                "nonmissing_volume": int(group["volume"].notna().sum()),
                "duplicate_dates": int(group["date"].duplicated().sum()),
                "nonpositive_adj_close": int((group["adj_close"] <= 0).sum()),
                "negative_volume": int((group["volume"].dropna() < 0).sum()),
            }
        )
    return pd.DataFrame(records)


def format_float_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in [
        "open",
        "high",
        "low",
        "close",
        "adj_close",
        "dividends",
        "stock_splits",
        "simple_return",
        "log_return",
        "dollar_volume",
        "log_volume",
    ]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    return out


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(output_dir)

    try:
        if args.calendar_buffer_days < 1:
            raise MarketDataError("--calendar-buffer-days muss positiv sein.")
        if args.post_event_buffer_days < 0:
            raise MarketDataError("--post-event-buffer-days darf nicht negativ sein.")

        config = WindowConfig(
            estimation_start=args.estimation_start,
            estimation_end=args.estimation_end,
            event_start=args.event_window_start,
            event_end=args.event_window_end,
        )
        validate_windows(config)

        events_path = Path(args.events_file)
        events = load_events(events_path)
        stock_symbols = normalize_symbols(events["ticker"].tolist())
        main_benchmark = str(args.main_benchmark).strip().upper()
        robustness = normalize_symbols(args.robustness_benchmarks)
        benchmark_symbols = normalize_symbols([main_benchmark, *robustness])
        all_symbols = normalize_symbols([*stock_symbols, *benchmark_symbols])

        if main_benchmark in stock_symbols:
            raise MarketDataError("Der Hauptbenchmark darf nicht einer der Aktienticker sein.")

        start_date, end_date = derive_download_range(
            events=events,
            start_override=args.start_date,
            end_override=args.end_date,
            calendar_buffer_days=args.calendar_buffer_days,
            post_event_buffer_days=args.post_event_buffer_days,
        )

        retrieved_at_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        logging.info("Eventdatei: %s", events_path)
        logging.info("Events: %d", len(events))
        logging.info("Aktien: %s", ", ".join(stock_symbols))
        logging.info("Hauptbenchmark: %s", main_benchmark)
        logging.info("Robustheitsbenchmarks: %s", ", ".join(robustness) or "keine")
        logging.info("Downloadzeitraum: %s bis %s", start_date, end_date)
        logging.info(
            "Schätzfenster: [%d,%d], geprüftes Eventfenster: [%d,%d]",
            config.estimation_start,
            config.estimation_end,
            config.event_start,
            config.event_end,
        )

        frames: list[pd.DataFrame] = []
        raw_manifest: list[dict[str, object]] = []
        downloaded_count = 0

        for index, symbol in enumerate(all_symbols, start=1):
            role = "stock" if symbol in stock_symbols else (
                "main_benchmark" if symbol == main_benchmark else "robustness_benchmark"
            )
            raw_path = raw_dir / f"{safe_filename(symbol)}_daily_raw.csv"
            logging.info("Verarbeite %d/%d: %s", index, len(all_symbols), symbol)

            if raw_path.exists() and not args.overwrite_raw:
                logging.info("%s: Verwende vorhandene Rohdatei %s", symbol, raw_path)
                data = read_raw_csv(raw_path, symbol)
                was_downloaded = False
            else:
                data = download_symbol(symbol, start_date, end_date)
                write_raw_csv(raw_path, data)
                was_downloaded = True
                downloaded_count += 1
                logging.info("%s: Rohdaten gespeichert: %s", symbol, raw_path)

            # Begrenze auch wiederverwendete Rohdateien auf den aktuellen Lauf.
            mask = (data.index.date >= start_date) & (data.index.date <= end_date)
            data = data.loc[mask].copy()
            if data.empty:
                raise MarketDataError(
                    f"{symbol}: Keine Daten innerhalb {start_date} bis {end_date}."
                )

            frames.append(enrich_symbol_data(data, symbol, role, retrieved_at_utc))
            raw_manifest.append(
                {
                    "symbol": symbol,
                    "role": role,
                    "raw_file": str(raw_path),
                    "sha256": sha256_file(raw_path),
                    "rows_used": len(data),
                    "first_date": data.index.min().strftime("%Y-%m-%d"),
                    "last_date": data.index.max().strftime("%Y-%m-%d"),
                    "downloaded_in_this_run": was_downloaded,
                }
            )

        master = pd.concat(frames, ignore_index=True)
        master = format_float_columns(master)
        master = master.sort_values(["date", "symbol"]).reset_index(drop=True)

        if master.duplicated(["date", "symbol"]).any():
            raise MarketDataError("Doppelte Kombinationen aus Datum und Symbol gefunden.")

        symbol_summary = build_symbol_summary(master)
        coverage = build_event_coverage(events, master, main_benchmark, config)
        coverage_ready_count = int((coverage["price_coverage_ready"] == "true").sum())

        prices_master_path = output_dir / "prices_master.csv"
        adjusted_wide_path = output_dir / "adjusted_close_wide.csv"
        returns_wide_path = output_dir / "returns_wide.csv"
        volume_wide_path = output_dir / "volume_wide.csv"
        symbol_summary_path = output_dir / "price_coverage_by_symbol.csv"
        event_coverage_path = output_dir / "event_price_coverage.csv"
        metadata_path = output_dir / "market_data_metadata.json"

        master.to_csv(prices_master_path, index=False, encoding="utf-8-sig")
        build_wide(master, "adj_close").to_csv(
            adjusted_wide_path, index=False, encoding="utf-8-sig"
        )
        build_wide(master, "simple_return").to_csv(
            returns_wide_path, index=False, encoding="utf-8-sig"
        )
        build_wide(master, "volume").to_csv(
            volume_wide_path, index=False, encoding="utf-8-sig"
        )
        symbol_summary.to_csv(symbol_summary_path, index=False, encoding="utf-8-sig")
        coverage.to_csv(event_coverage_path, index=False, encoding="utf-8-sig")

        metadata = {
            "created_at_utc": retrieved_at_utc,
            "source": "Yahoo Finance via yfinance",
            "source_note": (
                "yfinance ist ein nicht von Yahoo autorisiertes Open-Source-Werkzeug, "
                "das öffentlich zugängliche Yahoo-Finance-Schnittstellen verwendet."
            ),
            "events_file": str(events_path),
            "events_file_sha256": sha256_file(events_path),
            "event_count": len(events),
            "stock_symbols": stock_symbols,
            "main_benchmark": main_benchmark,
            "robustness_benchmarks": robustness,
            "download_start_inclusive": start_date.isoformat(),
            "download_end_inclusive": end_date.isoformat(),
            "price_for_returns": "Adj Close",
            "return_definitions": {
                "simple_return": "adj_close_t / adj_close_t_minus_1 - 1",
                "log_return": "ln(adj_close_t / adj_close_t_minus_1)",
            },
            "estimation_window": [config.estimation_start, config.estimation_end],
            "event_coverage_window": [config.event_start, config.event_end],
            "events_with_complete_price_coverage": coverage_ready_count,
            "events_without_complete_price_coverage": len(events) - coverage_ready_count,
            "new_downloads_in_this_run": downloaded_count,
            "raw_files": raw_manifest,
            "notes": [
                "Bereinigte Schlusskurse werden für Renditen verwendet, um Splits und Dividenden zu berücksichtigen.",
                "SPY dient als breiter Marktbenchmark im Hauptmodell.",
                "QQQ wird als technologielastiger Robustheitsbenchmark mitgeführt.",
                "Volumen wird unverändert gespeichert; eine Normalisierung oder Standardisierung erfolgt erst in der Analyse.",
                "Das Eventfenster [-5,+5] wird nur auf Datenabdeckung geprüft; AR und CAR werden in einem separaten Analyseschritt berechnet.",
            ],
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print("\n" + "=" * 78)
        print("MARKTDATEN: ERGEBNIS")
        print("=" * 78)
        print(f"Events:                         {len(events)}")
        print(f"Aktien:                         {', '.join(stock_symbols)}")
        print(f"Hauptbenchmark:                 {main_benchmark}")
        print(f"Robustheitsbenchmarks:          {', '.join(robustness) or 'keine'}")
        print(f"Downloadzeitraum:               {start_date} bis {end_date}")
        print(f"Neue Downloads:                 {downloaded_count}")
        print(f"Vollständig kursabgedeckt:      {coverage_ready_count}/{len(events)}")
        print(f"Zentraler Kursdatensatz:        {prices_master_path}")
        print(f"Renditen im breiten Format:     {returns_wide_path}")
        print(f"Event-Abdeckungsprüfung:        {event_coverage_path}")

        logging.info("Neue Downloads in diesem Lauf: %d", downloaded_count)
        logging.info("Zentraler Kursdatensatz: %s", prices_master_path)
        logging.info("Event-Abdeckungsprüfung: %s", event_coverage_path)

        if args.strict_coverage and coverage_ready_count != len(events):
            logging.error(
                "Strenge Abdeckungsprüfung fehlgeschlagen: %d von %d Events vollständig.",
                coverage_ready_count,
                len(events),
            )
            return 2
        return 0

    except MarketDataError as exc:
        logging.error("%s", exc)
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.exception("Unerwarteter Fehler")
        print(f"UNERWARTETER FEHLER: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
