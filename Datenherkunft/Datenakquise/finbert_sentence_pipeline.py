"""Sentence-level FinBERT TONE, matching the seminar's stated definition.

All speakers are included. Each sentence receives one argmax class; TONE is
(positive sentences - negative sentences) / all sentences. No token weighting.
The original chunk-based pipeline is available only through --legacy-method.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd

from run_finbert_sentiment import (
    FinBERTError, add_roles_and_sections, load_turns, load_manifest,
    load_model_and_tokenizer, resolve_device, sha256_file, utc_now,
)

VERSION = '2.0.0'
SEGMENTATION_VERSION = 'english_rules_v1'
MODEL = 'yiyanghkust/finbert-tone'
LABELS = ('neutral', 'positive', 'negative')
ROOT = Path(__file__).resolve().parent
ABBREVIATIONS = {
    'mr.', 'mrs.', 'ms.', 'dr.', 'prof.', 'sr.', 'jr.', 'st.',
    'vs.', 'approx.', 'fig.', 'no.', 'nos.', 'jan.', 'feb.', 'mar.',
    'apr.', 'jun.', 'jul.', 'aug.', 'sep.', 'sept.', 'oct.', 'nov.', 'dec.',
}
CONTINUING_ABBREVIATIONS = {'inc.', 'corp.', 'co.', 'ltd.', 'etc.', 'e.g.', 'i.e.'}
SENTENCE_STARTERS = {'we', 'our', 'i', 'it', 'this', 'that', 'the', 'these', 'they', 'he', 'she', 'you', 'next', 'however', 'thank', 'thanks'}


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """Deterministic English boundary rules; preserve every non-whitespace char.

    Numbers such as 3.5 and abbreviations/initialisms are protected. Boundaries
    require punctuation followed by whitespace or end of turn. Unpunctuated
    end-of-turn text is retained and flagged for review, never silently dropped.
    This is a reproducible heuristic, not a claim of perfect linguistic parsing.
    """
    rows = []
    start = 0
    for match in re.finditer(r'''[.!?]+["'”’\)\]]*(?=\s|$)''', text):
        end = match.end()
        before = text[:match.start() + 1]
        token_match = re.search(r'([A-Za-z.]+)$', before)
        token = token_match.group(1) if token_match else ''
        following = re.search(r'[A-Za-z]+', text[end:])
        next_word = following.group() if following else ''
        if match.group().startswith('.') and next_word:
            if token.lower() in ABBREVIATIONS:
                continue
            if token.lower() in CONTINUING_ABBREVIATIONS and next_word.lower() not in SENTENCE_STARTERS:
                continue
            if re.fullmatch(r'[A-Z]\.', token) and not re.search(r'&[A-Z]\.$', before):
                continue
            if re.fullmatch(r'(?:[A-Za-z]\.){2,}', token) and next_word.lower() not in SENTENCE_STARTERS:
                continue
        left = start
        while left < end and text[left].isspace():
            left += 1
        if left < end:
            rows.append((left, end, text[left:end]))
        start = end
    left = start
    while left < len(text) and text[left].isspace():
        left += 1
    end = len(text.rstrip())
    if left < end:
        rows.append((left, end, text[left:end]))
    if ''.join(re.sub(r'\s', '', s) for _, _, s in rows) != re.sub(r'\s', '', text):
        raise FinBERTError('Satztrennung hat Text verloren.')
    return rows


def sentence_plan(turns: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for turn in turns.to_dict('records'):
        text = turn['content']
        for index, (start, end, sentence) in enumerate(split_sentences(text)):
            key = json.dumps([SEGMENTATION_VERSION, turn['event_id'], int(turn['turn_index']), index, sentence], ensure_ascii=False)
            rows.append({k: turn[k] for k in ['event_id', 'ticker', 'reported_date', 'turn_index', 'speaker', 'speaker_title', 'speaker_role', 'call_section']} | {
                'sentence_id': hashlib.sha256(key.encode()).hexdigest(),
                'sentence_index': index, 'start_char': start, 'end_char': end,
                'sentence_text': sentence,
                'terminal_punctuation': bool(re.search(r'''[.!?]["'”’\)\]]*$''', sentence)),
            })
    result = pd.DataFrame(rows)
    if result.empty or result.sentence_id.duplicated().any():
        raise FinBERTError('Keine oder doppelte Satz-IDs.')
    return result


def class_from_probabilities(values: dict[str, float]) -> str:
    if set(values) != set(LABELS) or not all(np.isfinite(list(values.values()))):
        raise FinBERTError('Ungültige Modellwahrscheinlichkeiten.')
    if not np.isclose(sum(values.values()), 1.0, atol=1e-5) or min(values.values()) < 0:
        raise FinBERTError('Wahrscheinlichkeiten müssen nichtnegativ sein und sich zu 1 summieren.')
    # A deterministic tie rule: neutral before positive before negative.
    return max(LABELS, key=values.get)


def aggregate_sentences(plan: pd.DataFrame, predictions: pd.DataFrame) -> pd.DataFrame:
    if predictions.sentence_id.duplicated().any() or set(predictions.sentence_id) != set(plan.sentence_id):
        raise FinBERTError('Jeder Satz benötigt genau eine Vorhersage.')
    if not predictions.predicted_label.isin(LABELS).all():
        raise FinBERTError('Unbekannte Klassenbezeichnung.')
    merged = plan.merge(predictions[['sentence_id', 'predicted_label']], on='sentence_id', validate='one_to_one')
    rows = []
    for event_id, group in merged.groupby('event_id', sort=False):
        counts = group.predicted_label.value_counts()
        pos, neg, neutral = (int(counts.get(k, 0)) for k in ['positive', 'negative', 'neutral'])
        total = pos + neg + neutral
        row = {'event_id': event_id, 'ticker': group.ticker.iloc[0],
               'n_positive': pos, 'n_negative': neg, 'n_neutral': neutral,
               'n_sentences': total, 'tone': (pos - neg) / total,
               'sentiment_finbert_full_call': (pos - neg) / total}
        for role in ['management', 'analyst', 'operator', 'investor_relations']:
            row['n_sentences_' + role] = int(group.speaker_role.eq(role).sum())
        rows.append(row)
    return pd.DataFrame(rows)


def merge_tone_dataset(dataset: pd.DataFrame, event_scores: pd.DataFrame) -> pd.DataFrame:
    if dataset.event_id.duplicated().any() or set(dataset.event_id) != set(event_scores.event_id):
        raise FinBERTError('Analysetabelle und TONE-Ergebnisse haben unterschiedliche Ereignisse.')
    legacy = {c: 'legacy_' + c for c in dataset if c.startswith('sentiment_finbert')}
    renamed = dataset.rename(columns=legacy)
    fresh = event_scores.drop(columns='ticker')
    collisions = (set(renamed) & set(fresh)) - {'event_id'}
    if collisions:
        raise FinBERTError('Analysetabelle enthält bereits TONE-Ergebnisse: ' + ', '.join(sorted(collisions)))
    result = renamed.merge(fresh, on='event_id', validate='one_to_one')
    result['sentiment_recalculation_required'] = False
    result['sentiment_recalculation_reason'] = ''
    result['regression_recalculation_required'] = True
    result['sentiment_method'] = 'finbert_tone_full_call_equal_sentence_argmax'
    return result


def validate_cache(path: Path, identity: dict, plan: pd.DataFrame, overwrite: bool) -> pd.DataFrame:
    signature_path = path.with_suffix('.identity.json')
    if overwrite or not path.exists():
        # Remove old predictions BEFORE changing their identity, including when
        # a run is interrupted before its first new checkpoint.
        if overwrite:
            path.unlink(missing_ok=True)
        signature_path.write_text(json.dumps(identity, indent=2, sort_keys=True), encoding='utf-8')
        return pd.DataFrame()
    if not signature_path.exists() or json.loads(signature_path.read_text(encoding='utf-8')) != identity:
        raise FinBERTError('Vorhersage-Cache gehört zu einem anderen Modell, Text oder Verfahren. Neuen Ausgabeordner oder --overwrite verwenden.')
    cached = pd.read_csv(path)
    required = {'sentence_id', 'predicted_label', 'prob_neutral', 'prob_positive', 'prob_negative'}
    if not required.issubset(cached) or cached.sentence_id.duplicated().any():
        raise FinBERTError('Ungültiger Satz-Cache.')
    if not set(cached.sentence_id).issubset(set(plan.sentence_id)):
        raise FinBERTError('Cache enthält fremde Satz-IDs.')
    for row in cached.itertuples():
        label = class_from_probabilities({k: getattr(row, 'prob_' + k) for k in LABELS})
        if label != row.predicted_label:
            raise FinBERTError('Cache-Klasse passt nicht zu den Wahrscheinlichkeiten.')
    return cached


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    frame.to_csv(tmp, index=False, encoding='utf-8-sig')
    tmp.replace(path)


def check_sentence_lengths(plan, tokenizer, model, output_dir):
    limits = [int(model.config.max_position_embeddings)]
    if int(tokenizer.model_max_length) < 100_000:
        limits.append(int(tokenizer.model_max_length))
    limit = min(limits)
    lengths = [len(tokenizer.encode(s, add_special_tokens=True, truncation=False)) for s in plan.sentence_text]
    too_long = plan.loc[np.array(lengths) > limit].copy()
    if not too_long.empty:
        too_long['token_count'] = np.array(lengths)[np.array(lengths) > limit]
        write_csv(too_long, output_dir/'sentences_exceeding_model_limit.csv')
        raise FinBERTError(f'{len(too_long)} Sätze überschreiten {limit} Tokens. Im Prüfbericht kontrollieren; es wird kein Satz gekürzt oder in mehrere Stimmen zerlegt.')
    (output_dir/'sentences_exceeding_model_limit.csv').unlink(missing_ok=True)


def infer_sentences(plan, tokenizer, model, id2label, device, batch_size, cache_path, identity, overwrite):
    import torch
    if set(id2label.values()) != set(LABELS):
        raise FinBERTError('Das Modell muss Neutral, Positive und Negative liefern.')
    check_sentence_lengths(plan, tokenizer, model, cache_path.parent)
    existing = validate_cache(cache_path, identity, plan, overwrite)
    rows = existing.to_dict('records') if not existing.empty else []
    done = {r['sentence_id'] for r in rows}
    pending = plan.loc[~plan.sentence_id.isin(done)]
    for start in range(0, len(pending), batch_size):
        batch = pending.iloc[start:start + batch_size]
        encoded = tokenizer(batch.sentence_text.tolist(), padding=True, truncation=False, return_tensors='pt')
        encoded = {k: v.to(device) for k, v in encoded.items()}
        with torch.inference_mode():
            probs = torch.softmax(model(**encoded).logits, dim=-1).detach().cpu().numpy()
        for sentence_id, values in zip(batch.sentence_id, probs):
            mapping = {id2label[i]: float(v) for i, v in enumerate(values)}
            rows.append({'sentence_id': sentence_id, 'predicted_label': class_from_probabilities(mapping),
                         **{'prob_' + k: mapping[k] for k in LABELS}})
        if (start // batch_size) % 20 == 0 or start + batch_size >= len(pending):
            write_csv(pd.DataFrame(rows), cache_path)
            logging.info('Sätze berechnet: %s/%s', len(rows), len(plan))
    return pd.DataFrame(rows)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description='FinBERT TONE: ganzer Call, ein Satz = eine Stimme.')
    p.add_argument('--turns-file', type=Path, default=ROOT/'data_completion_results/transcript_turns_completed.csv')
    p.add_argument('--manifest-file', type=Path, default=ROOT/'data_completion_results/transcript_manifest_completed.csv')
    p.add_argument('--output-dir', type=Path, default=ROOT/'finbert_sentence_results')
    p.add_argument('--analysis-file', type=Path, default=ROOT/'data_completion_results/analysis_dataset_supplemented.csv')
    p.add_argument('--model-name', default=MODEL)
    p.add_argument('--model-revision', default='main')
    p.add_argument('--batch-size', type=int, default=16)
    p.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto')
    p.add_argument('--audit-only', action='store_true', help='Satzplan und Eingabediagnostik ohne Modell erzeugen.')
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--strict', action='store_true', help='Vollständigkeit wird bereits immer streng geprüft.')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        if args.model_name != MODEL:
            raise FinBERTError('Die Satzpipeline ist auf yiyanghkust/finbert-tone festgelegt; für das alte Verfahren --legacy-method verwenden.')
        if args.batch_size < 1:
            raise FinBERTError('Die Batchgröße muss positiv sein.')
        # Do not overwrite the historical chunk outputs or a mismatched method.
        if (args.output_dir/'finbert_chunk_predictions.csv').exists():
            raise FinBERTError('Dieser Ordner enthält alte Chunk-Ergebnisse. Einen neuen Satz-Ausgabeordner verwenden.')
        turns = add_roles_and_sections(load_turns(args.turns_file))
        manifest = load_manifest(args.manifest_file)
        dataset = pd.read_csv(args.analysis_file, low_memory=False)
        if dataset.event_id.duplicated().any() or set(dataset.event_id) != set(manifest.event_id):
            raise FinBERTError('Ereignisse in Analysetabelle und Manifest stimmen nicht überein.')
        if set(turns.event_id) != set(manifest.event_id):
            raise FinBERTError('Ereignisse in Texten und Manifest stimmen nicht überein.')
        actual_counts = turns.groupby('event_id').size()
        expected_counts = manifest.set_index('event_id').turn_count
        if not actual_counts.eq(expected_counts.reindex(actual_counts.index)).all():
            raise FinBERTError('Gesprächszahlen stimmen nicht zum Manifest; das ergänzte Manifest verwenden.')
        plan = sentence_plan(turns)
        write_csv(plan, args.output_dir/'finbert_sentence_plan.csv')
        diagnostics = plan.groupby('event_id', as_index=False).agg(
            n_sentences=('sentence_id', 'size'),
            unpunctuated_end_fragments=('terminal_punctuation', lambda v: int((~v).sum())),
            maximum_sentence_characters=('sentence_text', lambda v: int(v.str.len().max())),
        )
        write_csv(diagnostics, args.output_dir/'finbert_input_diagnostics.csv')
        audit = {'status': 'audit_only', 'script_version': VERSION, 'model_name': MODEL,
                 'events': int(plan.event_id.nunique()), 'turns': len(turns), 'sentences': len(plan),
                 'segmentation': SEGMENTATION_VERSION, 'all_speaker_roles_included': True,
                 'created_at_utc': utc_now(), 'turns_sha256': sha256_file(args.turns_file),
                 'manifest_sha256': sha256_file(args.manifest_file), 'scores_computed': False}
        audit['analysis_sha256'] = sha256_file(args.analysis_file)
        (args.output_dir/'finbert_audit_metadata.json').write_text(json.dumps(audit, indent=2), encoding='utf-8')
        if args.audit_only:
            print(json.dumps(audit, indent=2)); return 0
        (args.output_dir/'finbert_run_status.json').write_text(json.dumps(
            audit | {'status': 'running', 'scores_computed': False}, indent=2), encoding='utf-8')
        device = resolve_device(args.device)
        tokenizer, model, labels, versions = load_model_and_tokenizer(MODEL, args.model_revision, device)
        commit = versions.get('model_commit_hash')
        if not commit:
            if re.fullmatch('[a-fA-F0-9]{40}', args.model_revision):
                commit = args.model_revision
            else:
                raise FinBERTError('Modellrevision konnte nicht eindeutig aufgelöst werden. Einen Commit-Hash angeben.')
        identity = {'model': MODEL, 'commit': commit, 'script_version': VERSION,
                    'segmentation': SEGMENTATION_VERSION, 'turns_sha256': audit['turns_sha256'],
                    'manifest_sha256': audit['manifest_sha256'], 'labels': {str(k): v for k, v in labels.items()},
                    'score': '(N_positive-N_negative)/N_sentences_full_call'}
        predictions = infer_sentences(plan, tokenizer, model, labels, device, args.batch_size,
                                      args.output_dir/'finbert_sentence_predictions.csv', identity, args.overwrite)
        event = aggregate_sentences(plan, predictions)
        if set(event.event_id) != set(manifest.event_id) or not np.isfinite(event.tone).all():
            raise FinBERTError('Unvollständige TONE-Ergebnisse.')
        write_csv(event, args.output_dir/'finbert_event_sentiment.csv')
        write_csv(merge_tone_dataset(dataset, event), args.output_dir/'analysis_dataset_with_tone.csv')
        metadata = audit | {'status': 'complete', 'scores_computed': True, 'model_commit_hash': commit,
                            'score_definition': identity['score'], 'primary_variable': 'tone',
                            'sentence_weight': 1, 'truncation': False, **versions}
        (args.output_dir/'finbert_metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        (args.output_dir/'finbert_run_status.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        print(f'TONE für {len(event)} Ereignisse aus {len(predictions)} gleich gewichteten Sätzen berechnet.')
        return 0
    except (FinBERTError, FileNotFoundError) as exc:
        if not args.audit_only:
            (args.output_dir/'finbert_run_status.json').write_text(json.dumps({
                'status': 'failed', 'scores_computed': False, 'error': str(exc),
                'created_at_utc': utc_now()}, indent=2), encoding='utf-8')
        print('FEHLER: ' + str(exc)); return 1


if __name__ == '__main__':
    raise SystemExit(main())
