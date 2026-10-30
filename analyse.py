"""Event Study der Seminararbeit: Daten -> Kennzahlen -> Statistik -> Grafiken.

Start: REPRODUZIEREN.bat oder python analyse.py (Python 3.12).
Alle Berechnungen arbeiten offline auf den sechs Tabellen unter Daten/.
"""
from pathlib import Path
from datetime import date, timedelta
import hashlib
import json
import sys
import numpy as np
import pandas as pd
from scipy import stats

ORDNER = Path(__file__).resolve().parent
DATEN = ORDNER / 'Daten'
ERGEBNISSE = ORDNER / 'Ergebnisse'
EINFLUSSGROESSEN = ['sue', 'vol', 'prevol', 'search', 'tone']
SUCHBEGRIFFE = {'AAPL': 'Apple stock', 'AMZN': 'Amazon stock',
               'GOOGL': 'Alphabet stock', 'META': 'Meta stock', 'MSFT': 'Microsoft stock'}
TONQUELLE = 'TONE aus gespeicherten FinBERT-Satzbewertungen neu aggregiert'


def suchbegriff(ticker, berichtstag):
    if ticker == 'META' and berichtstag < '2021-10-28':
        return 'Facebook stock'
    return SUCHBEGRIFFE[ticker]


def marktmodell(stock, market, estimation, event):
    """Formeln 2–4: OLS-Marktmodell, erwartete Rendite, AR und CAR."""
    x = np.column_stack([np.ones(len(estimation)), market.iloc[estimation].to_numpy()])
    y = stock.iloc[estimation].to_numpy()
    future = np.column_stack([np.ones(len(event)), market.iloc[event].to_numpy()])
    actual = stock.iloc[event].to_numpy()
    if not all(np.isfinite(values).all() for values in [x, y, future, actual]):
        raise ValueError('Lücken oder ungültige Kurse im Schätz-/Ereignisfenster')
    if np.linalg.matrix_rank(x) < 2:
        raise ValueError('Marktrenditen ohne ausreichende Variation')
    alpha, beta = np.linalg.lstsq(x, y, rcond=None)[0]
    expected = future @ np.array([alpha, beta])
    abnormal = actual - expected
    car = float(abnormal.sum())
    residuals = y - x @ np.array([alpha, beta])
    sigma2 = float(residuals @ residuals / (len(y) - 2))
    v = future.sum(axis=0)
    car_se = np.sqrt(sigma2 * (len(event) + v @ np.linalg.inv(x.T @ x) @ v))
    statistic = car / car_se if car_se > 0 else np.nan
    pvalue = float(2 * stats.t.sf(abs(statistic), len(y) - 2))
    metrics = {
        'alpha': alpha, 'beta': beta, 'car': car, 'abs_car': abs(car),
        'car_se_classic': car_se, 'car_t_classic': statistic,
        'car_p_classic': pvalue, 'estimation_n': len(y),
        'sigma_estimation': np.sqrt(sigma2),
    }
    return metrics, expected, abnormal

def gewinnueberraschungen(history):
    """Formeln 5–7: UE und SUE mit genau den acht vorherigen Berichten."""
    history = history.sort_values(['ticker', 'reported_date']).copy()
    history['ue'] = history.reported_eps - history.estimated_eps
    grouped = history.groupby('ticker')['ue']
    history['sue_history_std'] = grouped.transform(
        lambda s: s.shift(1).rolling(8, min_periods=8).std(ddof=1))
    history['sue_history_n'] = grouped.transform(
        lambda s: s.shift(1).rolling(8, min_periods=1).count())
    history['sue'] = history.ue / history.sue_history_std.where(history.sue_history_std > 0)
    return history

def handelsvolumen(volume, index):
    """Formeln 8–11: SAV[-60,-10], VOL[0,+1], PREVOL[-3,-1]."""
    if index - 60 < 0 or index + 1 >= len(volume):
        raise ValueError('Unvollständiges Zeitfenster für das Handelsvolumen.')
    logvolume = np.log(volume.where(volume > 0))
    baseline = logvolume.iloc[index - 60:index - 9]
    event, pre = (logvolume.iloc[index:index + 2], logvolume.iloc[index - 3:index])
    mu = float(baseline.mean()) if baseline.notna().sum() == 51 else np.nan
    sigma = float(baseline.std(ddof=1)) if baseline.notna().sum() == 51 else np.nan
    valid = np.isfinite(sigma) and sigma > 0
    return {
        'volume_log_mean': mu,
        'volume_log_std': sigma,
        'vol': float(((event - mu) / sigma).mean())
               if valid and event.notna().all() else np.nan,
        'prevol': float(((pre - mu) / sigma).mean())
                  if valid and pre.notna().all() else np.nan,
    }

def suchinteresse(row, weeks):
    """SEARCH: genau neun Wochen desselben Exports, niemals Skalen zusammenfügen."""
    result = {'search': np.nan, 'search_source': 'nicht verfügbar',
              'search_export': '', 'search_event_svi': np.nan,
              'search_baseline_median': np.nan}
    if pd.isna(row.event_trading_day):
        return result
    event_day = date.fromisoformat(str(row.event_trading_day)[:10])
    sunday = event_day - timedelta(days=(event_day.weekday() + 1) % 7)
    required = [(sunday - timedelta(weeks=8 - i)).isoformat() for i in range(9)]
    query = suchbegriff(row.ticker, row.reported_date)
    candidates = weeks.loc[weeks.ticker.eq(row.ticker) & weeks['query'].eq(query)]
    exports = [(bool(part.supplemental.iloc[0]), part.week.min(), name, part)
               for name, part in candidates.groupby('export_id')]
    for _, _, name, part in sorted(exports, key=lambda item: item[:3]):
        values = part.set_index('week').svi.reindex(required)
        if not np.isfinite(values).all() or not values.gt(0).all():
            continue
        median, event_svi = (float(values.iloc[:8].median()), float(values.iloc[8]))
        result.update({
            'search': float(np.log(event_svi / median)),
            'search_source': 'ein Rohdatenexport',
            'search_export': name,
            'search_event_week': sunday.strftime('%Y-%m-%d'),
            'search_query': query,
            'search_event_svi': event_svi,
            'search_baseline_median': median,
            'search_baseline_values': ';'.join(str(v) for v in values.iloc[:8]),
            'search_export_sha256': part.source_sha256.iloc[0],
        })
        break
    return result


def daten_laden():
    """Die eingefrorenen Eingaben prüfen und lesen; keine Downloads nötig."""
    herkunft = json.loads((DATEN / 'Herkunft.json').read_text(encoding='utf-8'))
    for name, info in herkunft.items():
        if name.endswith('.csv'):
            actual = hashlib.sha256((DATEN / name).read_bytes()).hexdigest()
            if actual != info['sha256']:
                raise ValueError('Eingabedatei verändert: ' + name)
    tabellen = {name: pd.read_csv(DATEN / (name + '.csv'), encoding='utf-8-sig')
                for name in ['prices', 'volume', 'events', 'earnings_history',
                             'trends_wochen', 'finbert_saetze']}
    events = tabellen['events']
    if len(events) != 170 or events.event_id.duplicated().any():
        raise ValueError('Erwartet werden genau 170 eindeutige Quartalsberichte.')
    if not events.release_timing.eq('AMC').all():
        raise ValueError('Die Kernversion erwartet Veröffentlichungen nach Börsenschluss.')
    if events.event_trading_day.nunique() != 113:
        raise ValueError('Die Zahl der Ereignistage weicht von der Referenz ab.')
    return tabellen


def tonalitaet(saetze):
    """Jeder Satz zählt einmal: TONE = (positiv - negativ) / alle Sätze.

    Die FinBERT-Klassifikation ist gespeichert. Das KI-Modell wird hier
    nicht erneut ausgeführt; auch die neutralen Sätze zählen im Nenner.
    """
    if saetze.sentence_id.duplicated().any():
        raise ValueError('Doppelte Satzbewertungen.')
    if not saetze.label.isin(['positive', 'negative', 'neutral']).all():
        raise ValueError('Unbekannte Satzklasse.')
    counts = pd.crosstab(saetze.event_id, saetze.label)
    return (counts.positive - counts.negative) / counts.sum(axis=1)


def kennzahlen(tabellen, fenster=(0, 1)):
    """Alle 170 Ereignisse aus Kursen, EPS, Volumen, Trends und Sätzen berechnen.

    Handelstag 0 ist der erste Handelstag nach der AMC-Veröffentlichung.
    Marktmodell: [-250,-30]; CAR: Summe der AR im gewählten Fenster.
    VOL und PREVOL behalten auch bei alternativen CAR-Fenstern ihre Definition.
    """
    prices = tabellen['prices']
    dates = prices.loc[prices.SPY.gt(0), 'date'].to_numpy()
    prices = prices.set_index('date').reindex(dates)
    volume = tabellen['volume'].set_index('date').reindex(dates)

    # Einfache Renditen für Aktien UND SPY, anschließend Marktmodell neu schätzen.
    returns = prices / prices.shift(1) - 1
    history = gewinnueberraschungen(tabellen['earnings_history']).set_index('event_id')
    tones = tonalitaet(tabellen['finbert_saetze'])
    records, days = [], []

    for row in tabellen['events'].itertuples(index=False):
        record = row._asdict()
        for name in ['ue', 'sue_history_std', 'sue_history_n', 'sue']:
            record[name] = history.loc[row.event_id, name]
        record['sue_source'] = 'aus EPS-Historie berechnet'
        record['tone'] = tones.loc[row.event_id]
        record['tone_source'] = TONQUELLE
        record.update(suchinteresse(row, tabellen['trends_wochen']))
        record['car'], record['abs_car'] = np.nan, np.nan
        record['vol'], record['prevol'] = np.nan, np.nan

        index = int(np.flatnonzero(dates == row.event_trading_day)[0])
        first, last = fenster
        if index - 250 < 1 or index + last >= len(dates):
            raise ValueError('Unvollständiges Kursfenster: ' + row.event_id)
        metrics, expected, abnormal = marktmodell(
            returns[row.ticker], returns.SPY,
            np.arange(index - 250, index - 29),
            np.arange(index + first, index + last + 1))
        record.update(metrics)
        record.update(handelsvolumen(volume[row.ticker], index))
        for day, exp, ar in zip(range(first, last + 1), expected, abnormal):
            days.append({
                'event_id': row.event_id, 'ticker': row.ticker,
                'event_trading_day': row.event_trading_day, 'relative_day': day,
                'date': dates[index + day],
                'actual_return': returns[row.ticker].iloc[index + day],
                'market_return': returns.SPY.iloc[index + day],
                'expected_return': exp, 'ar': ar})
        if not np.isfinite([record[name] for name in ['car', *EINFLUSSGROESSEN]]).all():
            raise ValueError('Unvollständige Kennzahlen: ' + row.event_id)
        record.update(event_study_ready=True, usable=True, exclusion_reason='')
        records.append(record)

    events = pd.DataFrame(records)
    for name in ['sue', 'search', 'vol', 'prevol']:
        events[name + '_raw'] = events[name]
    data = events.copy()
    data['abs_sue'] = data.sue.abs()
    daily = pd.DataFrame(days)
    np.testing.assert_allclose(
        daily.groupby('event_id').ar.sum().reindex(data.event_id), data.car,
        rtol=1e-12, atol=1e-14, err_msg='CAR muss der Summe der AR entsprechen.')
    return data, events, daily


def referenz_pruefen(tabellen):
    """Zwölf Ergebnistabellen gegen den geprüften CAR-Lauf vom 28.10.2026 prüfen.

    Nur die Herkunftsbezeichnung von TONE wird bewusst präzisiert. Sämtliche
    Zahlen, Ereigniszuordnungen und sonstigen Texte müssen übereinstimmen.
    """
    nachweise = []
    for datei in sorted((DATEN / 'Referenz').glob('*.csv')):
        old = pd.read_csv(datei, encoding='utf-8-sig')
        new = tabellen[datei.stem].reset_index(drop=True)
        if list(old.columns) != list(new.columns) or old.shape != new.shape:
            raise ValueError('Abweichender Tabellenaufbau: ' + datei.name)
        maximum = 0.0
        for col in old:
            if col == 'tone_source':
                if not new[col].eq(TONQUELLE).all():
                    raise ValueError('Unzutreffende Herkunft der Tonalität.')
            elif pd.api.types.is_numeric_dtype(old[col]) and pd.api.types.is_numeric_dtype(new[col]):
                a, b = old[col].to_numpy(dtype=float), new[col].to_numpy(dtype=float)
                np.testing.assert_allclose(a, b, rtol=1e-9, atol=1e-11, equal_nan=True,
                                           err_msg=datei.stem + '/' + col)
                finite = np.isfinite(a) & np.isfinite(b)
                if finite.any():
                    maximum = max(maximum, float(np.max(np.abs(a[finite] - b[finite]))))
            else:
                a = old[col].fillna('').astype(str)
                b = new[col].fillna('').astype(str)
                if not a.equals(b):
                    raise ValueError('Abweichende Zuordnung: ' + datei.stem + '/' + col)
        nachweise.append({'tabelle': datei.stem, 'zeilen': len(new),
                          'groesste_absolutabweichung': maximum})
    if len(nachweise) != 12:
        raise ValueError('Es müssen genau zwölf Referenztabellen geprüft werden.')
    return nachweise


def main():
    from importlib.metadata import version
    from statistik import auswerten
    from grafiken import grafiken

    ERGEBNISSE.mkdir(exist_ok=True)
    (ERGEBNISSE / 'Pruefung.json').unlink(missing_ok=True)
    print('1/4 Eingaben prüfen; CAR und Einflussgrößen berechnen ...')
    inputs = daten_laden()
    data, events, daily = kennzahlen(inputs)
    # In den Robustheitsmodellen ändern sich nur CAR und |CAR|.
    alternatives = {str(list(window)): kennzahlen(inputs, window)[0]
                    for window in [(-1, 1), (0, 2)]}
    print('2/4 Regressionen, Diagnostik und Robustheit berechnen ...')
    tables, models, daily, cfg = auswerten(data, events, daily, alternatives)
    print('3/4 Zwölf Tabellen mit der bisherigen Auswertung vergleichen ...')
    checks = referenz_pruefen(tables)
    for name, frame in tables.items():
        frame.to_csv(ERGEBNISSE / (name + '.csv'), index=False, encoding='utf-8-sig')
    daily.to_csv(ERGEBNISSE / 'taegliche_renditen.csv', index=False, encoding='utf-8-sig')
    print('4/4 Abbildungen und kurze Ergebnisübersicht erstellen ...')
    figures = ERGEBNISSE / 'Abbildungen'
    figures.mkdir(exist_ok=True)
    grafiken(figures, data, tables['koeffizienten'], models, daily, cfg, tables['robustheit'])
    for outcome, (_, robust, _) in models.items():
        (ERGEBNISSE / (outcome + '_modell.txt')).write_text(str(robust.summary()), encoding='utf-8')
    uebersicht(tables)
    report = {'bestanden': True, 'ereignisse': len(data),
              'datumscluster': data.event_trading_day.nunique(), 'tabellen': checks,
              'renditen': 'P_t / P_(t-1) - 1; CAR = Summe der AR',
              'tonalitaet': TONQUELLE, 'python': sys.version,
              'pakete': {name: version(name) for name in
                         ['numpy', 'pandas', 'scipy', 'statsmodels', 'matplotlib']}}
    (ERGEBNISSE / 'Pruefung.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('BESTANDEN: 170 Ereignisse, 113 Datumscluster; zwölf Tabellen stimmen überein.')
    print('Ergebnisse: ' + str(ERGEBNISSE / 'Ergebnisse.md'))


def uebersicht(tables):
    """Kurze, ohne Zusatzprogramm lesbare Zusammenfassung der Hauptmodelle."""
    lines = ['# Ergebnisse der Event Study', '',
             '170 Quartalsberichte, 113 Ereignistage; einfache Renditen, CAR [0,+1].', '',
             'Die Koeffizienten beschreiben bedingte Zusammenhänge, keine nachgewiesenen Ursachen.', '']
    for outcome, title in [('car', 'CAR: Richtung'), ('abs_car', '|CAR|: Stärke')]:
        diag = tables['diagnostik'].set_index('outcome').loc[outcome]
        lines += [f'## {title}', '', f'R² = {diag.r_squared:.4f}; korrigiertes R² = {diag.adjusted_r_squared:.4f}.', '',
                  '| Variable | Koeffizient (Prozentpunkte je Einheit) | p-Wert |',
                  '|---|---:|---:|']
        coefs = tables['koeffizienten']
        selected = coefs.loc[coefs.model.eq('Vollmodell') & coefs.outcome.eq(outcome)
                             & coefs.variable.isin([*EINFLUSSGROESSEN, 'abs_sue'])]
        for row in selected.itertuples():
            lines.append(f'| {row.variable.upper()} | {100 * row.coefficient:.4f} | {row.p:.6f} |')
        lines += ['']
    lines += ['Die CSV-Tabellen speichern Renditen als Dezimalzahlen (0,01 = ein Prozentpunkt).',
              'Abbildungen liegen als PNG und PDF im Unterordner Abbildungen.',
              'Pruefung.json dokumentiert den Vergleich mit der bisherigen Auswertung.']
    (ERGEBNISSE / 'Ergebnisse.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
