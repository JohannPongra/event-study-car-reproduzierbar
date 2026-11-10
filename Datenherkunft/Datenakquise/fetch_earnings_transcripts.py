#!/usr/bin/env python3
"""
Fortsetzbare Akquise von Earnings-Call-Transkripten für eine Event Study.

Quelle:
    Alpha Vantage, Funktion EARNINGS_CALL_TRANSCRIPT

Besonderheit:
Der API-Parameter `quarter` bezeichnet das Geschäftsquartal. Das Skript
leitet es deshalb unternehmensspezifisch aus `fiscal_date_ending` ab:

- AMZN, GOOGL, META: Kalendergeschäftsjahr (31. Dezember)
- AAPL: Geschäftsjahr endet Ende September
- MSFT: Geschäftsjahr endet am 30. Juni

Für historische Meta-Events wird META zuerst und FB als Fallback versucht.

Das Skript ist fortsetzbar und verwendet standardmäßig höchstens 25 neue
API-Anfragen pro Lauf.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


SCRIPT_VERSION = "1.1.0"
API_URL = "https://www.alphavantage.co/query"
DEFAULT_KEY_ENV = "ALPHA_VANTAGE_API_KEY"
SUCCESS_STATUSES = {"success", "success_short"}
CALENDAR_YEAR_TICKERS = {"AMZN", "GOOGL", "META"}
META_TICKER_CHANGE_DATE = pd.Timestamp("2022-06-09")


class TranscriptAcquisitionError(RuntimeError):
    """Fehler bei der Transkriptakquise."""


@dataclass
class ParsedTranscript:
    turns: list[dict[str, Any]]
    full_text: str
    word_count: int
    character_count: int
    api_metadata: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Earnings-Call-Transkripte aus Alpha Vantage abrufen "
            "und eventbezogen speichern."
        )
    )
    parser.add_argument(
        "--events-file",
        default="alpha_vantage_earnings/events_master.csv",
        help="Pfad zu events_master.csv.",
    )
    parser.add_argument(
        "--output-dir",
        default="earnings_transcripts",
        help="Ausgabeordner.",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_KEY_ENV,
        help="Name der Umgebungsvariable mit dem API-Key.",
    )
    parser.add_argument(
        "--max-new-requests",
        type=int,
        default=25,
        help=(
            "Maximale Anzahl neuer API-Anfragen pro Lauf. "
            "0 bedeutet unbegrenzt. Standard: 25."
        ),
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=1.2,
        help="Pause zwischen API-Anfragen.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="HTTP-Timeout je Anfrage.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximale Zahl von HTTP-Versuchen je Anfrage.",
    )
    parser.add_argument(
        "--min-words",
        type=int,
        default=200,
        help=(
            "Transkripte unterhalb dieser Wortzahl erhalten "
            "den Status success_short."
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Vorhandene erfolgreiche Rohdateien erneut herunterladen.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help=(
            "Nur transcript_request_plan.csv erzeugen; "
            "kein API-Key und keine Internetverbindung nötig."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Mit Fehlercode 2 beenden, solange nicht alle Events "
            "ein verwendbares Transkript besitzen."
        ),
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
                output_dir / "fetch_earnings_transcripts.log",
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


def bool_series(series: pd.Series) -> pd.Series:
    return (
        series.astype(str)
        .str.strip()
        .str.lower()
        .isin({"true", "1", "yes", "ja"})
    )


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def quarter_of_date(date_value: pd.Timestamp) -> int:
    return (int(date_value.month) - 1) // 3 + 1


def derive_fiscal_quarter(
    ticker: str,
    fiscal_date_ending: pd.Timestamp,
) -> tuple[str, str]:
    """
    Liefert (YYYYQn, Regelbeschreibung).

    Die Alpha-Vantage-Earningsdatei normalisiert fiscal_date_ending auf
    Quartalsenden. Die Zuordnung wird daher über Monat und Geschäftsjahr
    vorgenommen.
    """
    ticker = ticker.upper()
    year = int(fiscal_date_ending.year)
    month = int(fiscal_date_ending.month)

    if month not in {3, 6, 9, 12}:
        raise TranscriptAcquisitionError(
            f"{ticker}: unerwarteter fiscal_date_ending-Monat {month}."
        )

    if ticker in CALENDAR_YEAR_TICKERS:
        quarter = quarter_of_date(fiscal_date_ending)
        return (
            f"{year}Q{quarter}",
            "calendar_fiscal_year_ends_december",
        )

    if ticker == "AAPL":
        mapping = {
            12: (year + 1, 1),
            3: (year, 2),
            6: (year, 3),
            9: (year, 4),
        }
        fiscal_year, fiscal_quarter = mapping[month]
        return (
            f"{fiscal_year}Q{fiscal_quarter}",
            "apple_fiscal_year_ends_september",
        )

    if ticker == "MSFT":
        mapping = {
            9: (year + 1, 1),
            12: (year + 1, 2),
            3: (year, 3),
            6: (year, 4),
        }
        fiscal_year, fiscal_quarter = mapping[month]
        return (
            f"{fiscal_year}Q{fiscal_quarter}",
            "microsoft_fiscal_year_ends_june",
        )

    raise TranscriptAcquisitionError(
        f"Keine Geschäftsquartalsregel für Ticker {ticker} definiert."
    )


def query_symbol_candidates(
    ticker: str,
    reported_date: pd.Timestamp,
) -> list[str]:
    ticker = ticker.upper()
    if ticker == "META" and reported_date < META_TICKER_CHANGE_DATE:
        return ["META", "FB"]
    return [ticker]


def load_events(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise TranscriptAcquisitionError(
            f"Eventdatei nicht gefunden: {path}"
        )

    events = pd.read_csv(path, encoding="utf-8-sig")
    required = {
        "event_id",
        "ticker",
        "fiscal_date_ending",
        "reported_date",
        "reported_calendar_quarter",
    }
    missing = required - set(events.columns)
    if missing:
        raise TranscriptAcquisitionError(
            "Fehlende Eventspalten: " + ", ".join(sorted(missing))
        )

    if "event_study_ready" in events.columns:
        events = events.loc[bool_series(events["event_study_ready"])].copy()

    events["event_id"] = events["event_id"].astype(str).str.strip()
    events["ticker"] = (
        events["ticker"].astype(str).str.strip().str.upper()
    )
    events["fiscal_date_ending"] = pd.to_datetime(
        events["fiscal_date_ending"], errors="coerce"
    )
    events["reported_date"] = pd.to_datetime(
        events["reported_date"], errors="coerce"
    )
    events["reported_calendar_quarter"] = (
        events["reported_calendar_quarter"]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    if events[
        ["fiscal_date_ending", "reported_date"]
    ].isna().any().any():
        raise TranscriptAcquisitionError(
            "Mindestens ein Eventdatum ist ungültig."
        )

    if events["event_id"].duplicated().any():
        duplicates = events.loc[
            events["event_id"].duplicated(keep=False), "event_id"
        ].unique()
        raise TranscriptAcquisitionError(
            "Doppelte Event-IDs: "
            + ", ".join(map(str, duplicates[:10]))
        )

    if events.duplicated(
        ["ticker", "reported_calendar_quarter"]
    ).any():
        raise TranscriptAcquisitionError(
            "Ticker und Veröffentlichungsquartal sind nicht eindeutig."
        )

    fiscal_quarters: list[str] = []
    fiscal_rules: list[str] = []
    symbol_candidates: list[str] = []

    for row in events.itertuples(index=False):
        quarter, rule = derive_fiscal_quarter(
            row.ticker,
            row.fiscal_date_ending,
        )
        fiscal_quarters.append(quarter)
        fiscal_rules.append(rule)
        symbol_candidates.append(
            "|".join(
                query_symbol_candidates(
                    row.ticker,
                    row.reported_date,
                )
            )
        )

    events["query_fiscal_quarter"] = fiscal_quarters
    events["query_quarter_rule"] = fiscal_rules
    events["query_symbol_candidates"] = symbol_candidates

    if events.duplicated(
        ["ticker", "query_fiscal_quarter"]
    ).any():
        duplicates = events.loc[
            events.duplicated(
                ["ticker", "query_fiscal_quarter"],
                keep=False,
            ),
            ["ticker", "query_fiscal_quarter"],
        ]
        examples = ", ".join(
            f"{row.ticker}:{row.query_fiscal_quarter}"
            for row in duplicates.head(10).itertuples()
        )
        raise TranscriptAcquisitionError(
            "Ticker-Geschäftsquartal ist nicht eindeutig: " + examples
        )

    return events.sort_values(
        ["reported_date", "ticker"]
    ).reset_index(drop=True)


def build_request_plan(events: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "event_id",
        "ticker",
        "company_name",
        "fiscal_date_ending",
        "reported_date",
        "reported_calendar_quarter",
        "query_fiscal_quarter",
        "query_quarter_rule",
        "query_symbol_candidates",
    ]
    columns = [column for column in columns if column in events.columns]
    plan = events[columns].copy()
    for column in ["fiscal_date_ending", "reported_date"]:
        if column in plan.columns:
            plan[column] = pd.to_datetime(plan[column]).dt.date.astype(str)
    return plan


def api_message(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None

    for key in [
        "Error Message",
        "Information",
        "Note",
        "message",
        "error",
    ]:
        value = payload.get(key)
        if value:
            message = str(value).strip()
            lowered = message.lower()
            if any(
                token in lowered
                for token in [
                    "rate limit",
                    "call frequency",
                    "requests per day",
                    "api call",
                    "standard api rate",
                    "premium",
                ]
            ):
                return "api_limit", message
            return "api_message", message

    return None, None


def first_present(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def normalize_turn(item: Any, index: int) -> dict[str, Any] | None:
    if isinstance(item, str):
        content = item.strip()
        if not content:
            return None
        return {
            "turn_index": index,
            "speaker": "",
            "speaker_title": "",
            "content": content,
            "api_sentiment": "",
            "api_sentiment_score": "",
        }

    if not isinstance(item, dict):
        return None

    content = first_present(
        item,
        [
            "content",
            "text",
            "utterance",
            "transcript",
            "statement",
        ],
    )
    if content is None or not str(content).strip():
        return None

    speaker = first_present(
        item,
        [
            "speaker",
            "speaker_name",
            "name",
            "person",
            "participant",
        ],
    )
    title = first_present(
        item,
        [
            "title",
            "speaker_title",
            "role",
            "position",
        ],
    )
    sentiment = first_present(
        item,
        [
            "sentiment",
            "sentiment_label",
            "overall_sentiment_label",
        ],
    )
    sentiment_score = first_present(
        item,
        [
            "sentiment_score",
            "overall_sentiment_score",
            "score",
        ],
    )

    return {
        "turn_index": index,
        "speaker": "" if speaker is None else str(speaker).strip(),
        "speaker_title": "" if title is None else str(title).strip(),
        "content": str(content).strip(),
        "api_sentiment": (
            "" if sentiment is None else str(sentiment).strip()
        ),
        "api_sentiment_score": (
            "" if sentiment_score is None else sentiment_score
        ),
    }


def transcript_container(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in [
            "transcript",
            "transcripts",
            "data",
            "content",
        ]:
            value = payload.get(key)
            if value is None:
                continue
            if key == "data" and isinstance(value, dict):
                nested = transcript_container(value)
                if nested is not None:
                    return nested
            else:
                return value
    return None


def parse_transcript(payload: Any) -> ParsedTranscript | None:
    message_status, _ = api_message(payload)
    if message_status is not None:
        return None

    container = transcript_container(payload)
    turns: list[dict[str, Any]] = []

    if isinstance(container, list):
        for index, item in enumerate(container):
            turn = normalize_turn(item, index)
            if turn is not None:
                turns.append(turn)
    elif isinstance(container, (str, dict)):
        if isinstance(container, dict):
            nested = transcript_container(container)
            if isinstance(nested, list):
                for index, item in enumerate(nested):
                    turn = normalize_turn(item, index)
                    if turn is not None:
                        turns.append(turn)
            else:
                turn = normalize_turn(container, 0)
                if turn is not None:
                    turns.append(turn)
        else:
            turn = normalize_turn(container, 0)
            if turn is not None:
                turns.append(turn)

    if not turns and isinstance(payload, list):
        for index, item in enumerate(payload):
            turn = normalize_turn(item, index)
            if turn is not None:
                turns.append(turn)

    if not turns:
        return None

    text_blocks: list[str] = []
    for turn in turns:
        speaker = turn["speaker"]
        title = turn["speaker_title"]
        prefix = speaker
        if title:
            prefix = f"{prefix} [{title}]" if prefix else f"[{title}]"
        text_blocks.append(
            f"{prefix}: {turn['content']}"
            if prefix
            else turn["content"]
        )

    full_text = "\n\n".join(text_blocks).strip()
    words = re.findall(r"\b[\w'-]+\b", full_text, flags=re.UNICODE)

    api_metadata: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key not in {
                "transcript",
                "transcripts",
                "data",
                "content",
            }:
                api_metadata[key] = value

    return ParsedTranscript(
        turns=turns,
        full_text=full_text,
        word_count=len(words),
        character_count=len(full_text),
        api_metadata=api_metadata,
    )


def request_payload(
    session: requests.Session,
    api_key: str,
    symbol: str,
    quarter: str,
    timeout_seconds: float,
    max_retries: int,
) -> tuple[Any, int]:
    params = {
        "function": "EARNINGS_CALL_TRANSCRIPT",
        "symbol": symbol,
        "quarter": quarter,
        "apikey": api_key,
    }

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(
                API_URL,
                params=params,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json(), response.status_code
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < max_retries:
                wait_seconds = min(2 ** (attempt - 1), 8)
                logging.warning(
                    "%s %s: Versuch %d/%d fehlgeschlagen: %s; "
                    "erneuter Versuch in %ds",
                    symbol,
                    quarter,
                    attempt,
                    max_retries,
                    exc,
                    wait_seconds,
                )
                time.sleep(wait_seconds)

    raise TranscriptAcquisitionError(
        f"{symbol} {quarter}: Anfrage fehlgeschlagen: {last_error}"
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def event_base(event: pd.Series) -> dict[str, Any]:
    columns = [
        "event_id",
        "ticker",
        "company_name",
        "fiscal_date_ending",
        "reported_date",
        "reported_calendar_quarter",
        "query_fiscal_quarter",
        "query_quarter_rule",
        "event_trading_day",
        "sue",
    ]
    output: dict[str, Any] = {}
    for column in columns:
        if column not in event.index:
            continue
        value = event[column]
        if isinstance(value, pd.Timestamp):
            output[column] = value.date().isoformat()
        elif pd.isna(value):
            output[column] = ""
        else:
            output[column] = value
    return output


def make_manifest_row(
    event: pd.Series,
    status: str,
    text_path: Path,
    source: str,
    query_symbol_used: str = "",
    raw_file: str = "",
    parsed: ParsedTranscript | None = None,
    api_status_code: int | None = None,
    message: str = "",
) -> dict[str, Any]:
    row = {
        **event_base(event),
        "query_symbol_candidates": event["query_symbol_candidates"],
        "query_symbol_used": query_symbol_used,
        "status": status,
        "usable_for_finbert": status in SUCCESS_STATUSES,
        "source": source,
        "source_endpoint": (
            "Alpha Vantage EARNINGS_CALL_TRANSCRIPT"
        ),
        "raw_file": raw_file,
        "text_file": str(text_path),
        "api_http_status": api_status_code,
        "message": message,
        "turn_count": 0,
        "word_count": 0,
        "character_count": 0,
        "processed_at_utc": utc_now(),
    }
    if parsed is not None:
        row["turn_count"] = len(parsed.turns)
        row["word_count"] = parsed.word_count
        row["character_count"] = parsed.character_count
    return row


def build_coverage(manifest: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for ticker, group in manifest.groupby("ticker"):
        usable = group["usable_for_finbert"].astype(bool)
        words = pd.to_numeric(
            group["word_count"], errors="coerce"
        ).fillna(0)
        rows.append(
            {
                "ticker": ticker,
                "event_count": int(len(group)),
                "usable_transcripts": int(usable.sum()),
                "missing_or_pending": int((~usable).sum()),
                "coverage_rate": float(usable.mean()),
                "total_words": int(words.sum()),
                "median_words": float(
                    words.loc[usable].median() if usable.any() else 0
                ),
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    args = parse_args()

    try:
        if args.max_new_requests < 0:
            raise TranscriptAcquisitionError(
                "--max-new-requests darf nicht negativ sein."
            )
        if args.pause_seconds < 0:
            raise TranscriptAcquisitionError(
                "--pause-seconds darf nicht negativ sein."
            )

        events_path = Path(args.events_file)
        output_dir = Path(args.output_dir)
        raw_dir = output_dir / "raw"
        text_dir = output_dir / "text"
        configure_logging(output_dir)

        events = load_events(events_path)
        request_plan = build_request_plan(events)
        request_plan_path = output_dir / "transcript_request_plan.csv"
        request_plan.to_csv(
            request_plan_path,
            index=False,
            encoding="utf-8-sig",
        )

        logging.info("Eventdatei: %s", events_path)
        logging.info("Event-study-bereite Events: %d", len(events))
        logging.info(
            "Tickerverteilung: %s",
            events["ticker"].value_counts().sort_index().to_dict(),
        )
        logging.info("Anfrageplan: %s", request_plan_path)

        if args.plan_only:
            print("\nAnfrageplan erstellt:")
            print(request_plan_path.resolve())
            return 0

        api_key = os.getenv(args.api_key_env, "").strip()
        if not api_key:
            raise TranscriptAcquisitionError(
                f"API-Key fehlt. Setze in PowerShell zuerst:\n"
                f'$env:{args.api_key_env}="DEIN_ALPHA_VANTAGE_KEY"'
            )

        logging.info(
            "Maximal neue API-Anfragen in diesem Lauf: %s",
            (
                "unbegrenzt"
                if args.max_new_requests == 0
                else args.max_new_requests
            ),
        )

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Johann-W-Seminar-Earnings-Transcript-Study/1.1"
                ),
                "Accept": "application/json",
            }
        )

        manifest_rows: list[dict[str, Any]] = []
        turn_rows: list[dict[str, Any]] = []
        new_requests = 0
        api_limit_reached = False

        for counter, (_, event) in enumerate(events.iterrows(), start=1):
            event_id = str(event["event_id"])
            quarter = str(event["query_fiscal_quarter"])
            candidates = str(
                event["query_symbol_candidates"]
            ).split("|")
            text_path = text_dir / f"{safe_name(event_id)}.txt"

            parsed: ParsedTranscript | None = None
            query_symbol_used = ""
            raw_file_used = ""
            source = ""
            http_status: int | None = None
            messages: list[str] = []
            pending_status = ""

            # Erfolgreichen Cache über alle Symbolkandidaten suchen.
            if not args.overwrite:
                for candidate in candidates:
                    raw_path = raw_dir / (
                        f"{safe_name(event_id)}__"
                        f"{safe_name(candidate)}_{quarter}.json"
                    )
                    if not raw_path.exists():
                        continue
                    try:
                        cached_payload = read_json(raw_path)
                    except (OSError, json.JSONDecodeError) as exc:
                        logging.warning(
                            "%s: Cache %s unlesbar: %s",
                            event_id,
                            raw_path,
                            exc,
                        )
                        continue

                    parsed_cached = parse_transcript(cached_payload)
                    if parsed_cached is not None:
                        parsed = parsed_cached
                        query_symbol_used = candidate
                        raw_file_used = str(raw_path)
                        source = "cache"
                        break

            # Falls kein erfolgreicher Cache existiert: Kandidaten anfragen.
            if parsed is None:
                for candidate in candidates:
                    if api_limit_reached:
                        pending_status = "pending_api_limit"
                        break

                    if (
                        args.max_new_requests > 0
                        and new_requests >= args.max_new_requests
                    ):
                        pending_status = "pending_run_limit"
                        break

                    raw_path = raw_dir / (
                        f"{safe_name(event_id)}__"
                        f"{safe_name(candidate)}_{quarter}.json"
                    )

                    logging.info(
                        "API-Anfrage %d: Event=%s, Symbol=%s, "
                        "Geschäftsquartal=%s",
                        new_requests + 1,
                        event_id,
                        candidate,
                        quarter,
                    )
                    try:
                        payload, http_status = request_payload(
                            session=session,
                            api_key=api_key,
                            symbol=candidate,
                            quarter=quarter,
                            timeout_seconds=args.timeout_seconds,
                            max_retries=args.max_retries,
                        )
                    except TranscriptAcquisitionError as exc:
                        messages.append(str(exc))
                        continue

                    new_requests += 1
                    write_json(raw_path, payload)

                    message_status, api_text = api_message(payload)
                    if message_status is not None:
                        messages.append(
                            f"{candidate}: {api_text or message_status}"
                        )
                        if message_status == "api_limit":
                            api_limit_reached = True
                            pending_status = "api_limit"
                            logging.warning(
                                "Alpha-Vantage-Limit erkannt; "
                                "weitere Events bleiben ausstehend."
                            )
                            break
                        continue

                    parsed_candidate = parse_transcript(payload)
                    if parsed_candidate is not None:
                        parsed = parsed_candidate
                        query_symbol_used = candidate
                        raw_file_used = str(raw_path)
                        source = "api"
                        break

                    messages.append(
                        f"{candidate}: kein erkennbares Transkript"
                    )
                    if args.pause_seconds:
                        time.sleep(args.pause_seconds)

                if parsed is None and pending_status:
                    manifest_rows.append(
                        make_manifest_row(
                            event,
                            status=pending_status,
                            text_path=text_path,
                            source="not_completed",
                            message=" | ".join(messages),
                        )
                    )
                    continue

            if parsed is None:
                manifest_rows.append(
                    make_manifest_row(
                        event,
                        status=(
                            "request_error"
                            if messages
                            and all(
                                "Anfrage fehlgeschlagen" in message
                                for message in messages
                            )
                            else "no_transcript"
                        ),
                        text_path=text_path,
                        source="api",
                        message=" | ".join(messages),
                    )
                )
                continue

            status = (
                "success"
                if parsed.word_count >= args.min_words
                else "success_short"
            )
            warning = ""
            if status == "success_short":
                warning = (
                    f"Nur {parsed.word_count} Wörter; "
                    "vor FinBERT manuell prüfen."
                )

            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(parsed.full_text, encoding="utf-8")

            manifest_rows.append(
                make_manifest_row(
                    event,
                    status=status,
                    text_path=text_path,
                    source=source,
                    query_symbol_used=query_symbol_used,
                    raw_file=raw_file_used,
                    parsed=parsed,
                    api_status_code=http_status,
                    message=warning,
                )
            )

            base = event_base(event)
            for turn in parsed.turns:
                turn_rows.append(
                    {
                        **base,
                        "query_symbol_used": query_symbol_used,
                        "turn_index": turn["turn_index"],
                        "speaker": turn["speaker"],
                        "speaker_title": turn["speaker_title"],
                        "content": turn["content"],
                        "api_sentiment": turn["api_sentiment"],
                        "api_sentiment_score": (
                            turn["api_sentiment_score"]
                        ),
                        "text_file": str(text_path),
                    }
                )

            if args.pause_seconds and source == "api":
                time.sleep(args.pause_seconds)

            if counter % 20 == 0 or counter == len(events):
                logging.info(
                    "Verarbeitet: %d/%d Events",
                    counter,
                    len(events),
                )

        manifest = pd.DataFrame(manifest_rows)
        turns = pd.DataFrame(turn_rows)

        if len(manifest) != len(events):
            raise TranscriptAcquisitionError(
                "Interner Fehler: Manifest enthält nicht genau "
                "eine Zeile pro Event."
            )

        manifest["usable_for_finbert"] = (
            manifest["usable_for_finbert"].astype(bool)
        )
        coverage = build_coverage(manifest)
        missing = manifest.loc[
            ~manifest["usable_for_finbert"]
        ].copy()

        manifest_path = output_dir / "transcript_manifest.csv"
        turns_path = output_dir / "transcript_turns.csv"
        coverage_path = (
            output_dir / "transcript_coverage_by_ticker.csv"
        )
        missing_path = output_dir / "transcript_missing.csv"
        metadata_path = output_dir / "transcript_metadata.json"

        manifest.to_csv(
            manifest_path,
            index=False,
            encoding="utf-8-sig",
        )
        turns.to_csv(
            turns_path,
            index=False,
            encoding="utf-8-sig",
        )
        coverage.to_csv(
            coverage_path,
            index=False,
            encoding="utf-8-sig",
        )
        missing.to_csv(
            missing_path,
            index=False,
            encoding="utf-8-sig",
        )

        usable_count = int(manifest["usable_for_finbert"].sum())
        status_counts = {
            str(key): int(value)
            for key, value in manifest["status"]
            .value_counts(dropna=False)
            .to_dict()
            .items()
        }

        metadata = {
            "created_at_utc": utc_now(),
            "script": "fetch_earnings_transcripts.py",
            "script_version": SCRIPT_VERSION,
            "source": "Alpha Vantage",
            "api_function": "EARNINGS_CALL_TRANSCRIPT",
            "events_file": str(events_path),
            "events_file_sha256": sha256_file(events_path),
            "events_input": int(len(events)),
            "events_with_usable_transcript": usable_count,
            "events_without_usable_transcript": int(
                len(events) - usable_count
            ),
            "new_api_requests_this_run": int(new_requests),
            "api_limit_reached_this_run": bool(api_limit_reached),
            "status_counts": status_counts,
            "query_mapping": {
                "calendar_year_companies": (
                    "AMZN, GOOGL, META: quarter derived from "
                    "fiscal_date_ending"
                ),
                "AAPL": (
                    "fiscal year ending in September; "
                    "December quarter maps to next fiscal year Q1"
                ),
                "MSFT": (
                    "fiscal year ending June 30; "
                    "September quarter maps to next fiscal year Q1"
                ),
                "META_alias": (
                    "META queried first; FB fallback for events "
                    "before 2022-06-09"
                ),
            },
            "tickers": sorted(events["ticker"].unique().tolist()),
            "minimum_word_warning_threshold": int(args.min_words),
            "outputs": {
                "request_plan": str(request_plan_path),
                "manifest": str(manifest_path),
                "turns": str(turns_path),
                "coverage": str(coverage_path),
                "missing": str(missing_path),
                "raw_directory": str(raw_dir),
                "text_directory": str(text_dir),
            },
            "methodological_notes": [
                (
                    "The API quarter is mapped to the company's "
                    "fiscal quarter rather than the calendar quarter "
                    "of publication."
                ),
                (
                    "Raw JSON responses are retained for "
                    "reproducibility."
                ),
                (
                    "Plain-text transcripts and turn-level content are "
                    "stored separately for later FinBERT inference."
                ),
                (
                    "Alpha Vantage sentiment fields are archived but "
                    "are not used as the study's FinBERT measure."
                ),
                (
                    "The script is resumable and reuses successfully "
                    "cached transcripts."
                ),
            ],
        }
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logging.info("Neue API-Anfragen: %d", new_requests)
        logging.info(
            "Verwendbare Transkripte: %d/%d",
            usable_count,
            len(events),
        )
        logging.info("Statusverteilung: %s", status_counts)
        logging.info("Manifest: %s", manifest_path)
        logging.info("Turn-Datensatz: %s", turns_path)

        print("\n" + "=" * 88)
        print("EARNINGS-CALL-TRANSKRIPTE: ERGEBNIS")
        print("=" * 88)
        print(f"Events insgesamt:                  {len(events)}")
        print(f"Neue API-Anfragen in diesem Lauf:  {new_requests}")
        print(f"Verwendbare Transkripte:          {usable_count}")
        print(
            f"Fehlend oder noch ausstehend:     "
            f"{len(events) - usable_count}"
        )
        print(f"API-Limit erkannt:                {api_limit_reached}")
        print(f"Ausgabeordner:                    {output_dir.resolve()}")

        if args.strict and usable_count != len(events):
            return 2

        return 0

    except TranscriptAcquisitionError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAbgebrochen.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
