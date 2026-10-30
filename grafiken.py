"""Abbildungen der Seminararbeit, jeweils als PNG und PDF."""
import math
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy import stats
from statistik import modell_predictors

GRUEN, GOLD = '#257a69', '#b48035'
ZIELE = {'car': 'Gerichtete Kursreaktion', 'abs_car': 'Größe der Kursreaktion'}
LABELS = {'sue': 'Gewinnüberraschung', 'vol': 'Handelsvolumen',
          'prevol': 'Volumen im Vorlauf', 'tone': 'Tonalität',
          'search': 'Suchinteresse', 'abs_sue': 'Betrag der Gewinnüberraschung'}


def label(name):
    return f'{LABELS.get(name, name)} ({name.upper()})' if name in LABELS else name

def zahl(value, stellen=4):
    return '–' if pd.isna(value) else f'{value:.{stellen}f}'.replace('.', ',')

def pwert(value):
    return '< 0,0001' if pd.notna(value) and value < 0.0001 else zahl(value)

def speichern_grafik(fig, path):
    fig.tight_layout()
    fig.savefig(path.with_suffix('.png'), dpi=170, bbox_inches='tight')
    fig.savefig(path.with_suffix('.pdf'), bbox_inches='tight')
    plt.close(fig)

def hauptkoeffizienten(coefficients, outcome, predictors):
    selected = coefficients.loc[
        coefficients.model.eq('Vollmodell') & coefficients.outcome.eq(outcome)
        & coefficients.variable.isin(predictors)]
    return selected.set_index('variable').loc[predictors]


def partielle_zusammenhaenge(out, data, coefficients, models, cfg, outcome, title):
    predictors = modell_predictors(cfg, outcome)
    ordinary, robust, x = models[outcome]
    selected = hauptkoeffizienten(coefficients, outcome, predictors)
    cols = min(2, len(predictors))
    rows = math.ceil(len(predictors) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(11, rows * 3.6 + 0.7), squeeze=False)
    for ax, variable in zip(axes.flat, predictors):
        other = x.drop(columns=variable).to_numpy()
        raw = x[variable].to_numpy()
        residual_x = raw - other @ np.linalg.lstsq(other, raw, rcond=None)[0]
        y = data[outcome].to_numpy()
        residual_y = y - other @ np.linalg.lstsq(other, y, rcond=None)[0]
        for ticker in sorted(data.ticker.unique()):
            mask = data.ticker.eq(ticker).to_numpy()
            ax.scatter(residual_x[mask], residual_y[mask] * 100, s=23, alpha=0.65, label=ticker, edgecolors='none')
        line = np.linspace(residual_x.min(), residual_x.max(), 100)
        c = selected.loc[variable]
        low, high = (c.ci_lower * line * 100, c.ci_upper * line * 100)
        ax.plot(line, c.coefficient * line * 100, color=GRUEN, lw=2)
        ax.fill_between(line, np.minimum(low, high), np.maximum(low, high), color=GRUEN, alpha=0.13)
        ax.axhline(0, color='#999999', lw=0.7)
        ax.set(xlabel=label(variable) + ' (bereinigt)',
               ylabel=outcome.upper() + ' (bereinigt, Prozentpunkte)',
               title=f"β = {zahl(c.coefficient)} · p {('' if c.p < 0.0001 else '= ')}{pwert(c.p)}")
    for ax in list(axes.flat)[len(predictors):]:
        ax.set_visible(False)
    axes.flat[0].legend(fontsize=8, ncol=3, frameon=False)
    fig.suptitle(f"{title}\nn = {len(data)} · R² = {zahl(ordinary.rsquared, 3)} · "
                 f"korr. R² = {zahl(ordinary.rsquared_adj, 3)} · cluster", fontsize=13)
    speichern_grafik(fig, out / (outcome + '_partiell'))

def koeffizienten(out, data, coefficients, cfg, outcome, title):
    predictors = modell_predictors(cfg, outcome)
    selected = hauptkoeffizienten(coefficients, outcome, predictors)
    scale = data[predictors].std(ddof=1) * 100
    b, lo, hi = (selected.coefficient * scale, selected.ci_lower * scale, selected.ci_upper * scale)
    fig, ax = plt.subplots(figsize=(10, 3 + len(predictors) * 0.38))
    pos = np.arange(len(selected))
    ax.errorbar(b, pos, xerr=np.vstack([b - lo, hi - b]), fmt='o', capsize=4, color=GRUEN)
    ax.axvline(0, color=GOLD)
    ax.set(yticks=pos, yticklabels=[label(p) for p in predictors],
           xlabel='Zusammenhang je zusätzlicher Standardabweichung (Prozentpunkte)',
           title=title + '\nPunktschätzung und 95%-Konfidenzintervall')
    ax.invert_yaxis()
    speichern_grafik(fig, out / (outcome + '_koeffizienten'))

def diagnostik(out, data, models, outcome, title):
    ordinary, robust, x = models[outcome]
    res, fitted = (np.asarray(ordinary.resid) * 100, np.asarray(ordinary.fittedvalues) * 100)
    cooks = ordinary.get_influence().cooks_distance[0]
    order = data.reset_index(drop=True).sort_values(['event_trading_day', 'event_id']).index.to_numpy()
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5))
    axes[0, 0].scatter(fitted, res, s=19, alpha=0.65, color=GRUEN)
    axes[0, 0].axhline(0, color=GOLD)
    axes[0, 0].set(xlabel='Geschätzte ' + outcome.upper() + ' (Prozentpunkte)',
                   ylabel='Residuum (Prozentpunkte)', title='Residuen und Modellwerte')
    (theory, ordered), (slope, intercept, _) = stats.probplot(res, dist='norm')
    axes[0, 1].scatter(theory, ordered, s=17, color=GRUEN)
    axes[0, 1].plot(theory, slope * theory + intercept, color=GOLD)
    axes[0, 1].set(xlabel='Theoretische Normalquantile',
                   ylabel='Geordnete Residuen (Prozentpunkte)', title='Normal-Quantil-Diagramm')
    marker, stems, _ = axes[1, 0].stem(np.arange(1, len(res) + 1), cooks[order], basefmt=' ')
    plt.setp(marker, color=GRUEN, markersize=3)
    plt.setp(stems, color=GRUEN)
    axes[1, 0].axhline(4 / len(res), color=GOLD, ls='--', label='4/n')
    axes[1, 0].set(xlabel='Ereignis (nach Datum sortiert)', ylabel='Cook-Distanz', title='Einfluss einzelner Ereignisse')
    axes[1, 0].legend(frameon=False)
    axes[1, 1].hist(res, bins='auto', color=GRUEN, alpha=0.85)
    axes[1, 1].set(xlabel='Residuum (Prozentpunkte)', ylabel='Anzahl', title='Verteilung der Residuen')
    fig.suptitle(title + ' · Modelldiagnostik', fontsize=13)
    speichern_grafik(fig, out / (outcome + '_diagnostik'))

def robustheit(out, cfg, variants, outcome):
    predictors = modell_predictors(cfg, outcome)
    cols = min(2, len(predictors))
    rows = math.ceil(len(predictors) / cols)
    frame = variants.loc[variants.model.str.startswith('Unternehmen: ')
                         & variants.outcome.eq(outcome)]
    fig, axes = plt.subplots(rows, cols, figsize=(12, rows * 3.3 + 0.5), squeeze=False)
    for ax, variable in zip(axes.flat, predictors):
        part = frame.loc[frame.variable.eq(variable)]
        ax.errorbar(part.coefficient, np.arange(len(part)),
                    xerr=[part.coefficient - part.ci_lower, part.ci_upper - part.coefficient],
                    fmt='o', capsize=3, color=GRUEN)
        ax.axvline(0, color=GOLD)
        names = part.model.str.removeprefix('Unternehmen: ').replace({'alle': 'Alle Unternehmen'})
        ax.set(yticks=np.arange(len(part)), yticklabels=names,
               title=label(variable), xlabel='Koeffizient mit 95%-Intervall')
        ax.invert_yaxis()
    for ax in list(axes.flat)[len(predictors):]:
        ax.set_visible(False)
    fig.suptitle(outcome.upper() + ' · Empfindlichkeit gegenüber der Unternehmensauswahl · cluster', fontsize=13)
    speichern_grafik(fig, out / f'{outcome}_unternehmen_cluster')

def ereignisverlauf(out, data, daily, cfg):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.7))
    if not daily.empty:
        curve = daily.groupby('relative_day')[['ar', 'cumulative_ar']].mean() * 100
        axes[0].plot(curve.index, curve.ar, marker='o', color=GRUEN, label='Mittlere AR')
        axes[0].plot(curve.index, curve.cumulative_ar, marker='o', color=GOLD, label='Kumulierte mittlere AR')
        axes[0].axhline(0, color='#999999', lw=0.7)
        axes[0].set_xticks(curve.index)
        axes[0].legend(frameon=False, fontsize=8)
    axes[0].set(xlabel='Handelstage relativ zum Ereignis',
               ylabel='Prozentpunkte', title='Mittlere Kursreaktion (deskriptiv)')
    corr = data[list(dict.fromkeys([*cfg['predictors'], *modell_predictors(cfg, 'abs_car'), 'car', 'abs_car']))].corr()
    img = axes[1].imshow(corr, vmin=-1, vmax=1, cmap='BrBG')
    axes[1].set(xticks=range(len(corr)), yticks=range(len(corr)), xticklabels=corr.columns.str.upper(), yticklabels=corr.columns.str.upper(), title='Korrelationen')
    axes[1].tick_params(axis='x', labelsize=8)
    axes[1].grid(False)
    for i in range(len(corr)):
        for j in range(len(corr)):
            axes[1].text(j, i, zahl(corr.iloc[i, j], 2), ha='center', va='center', fontsize=8, color='white' if abs(corr.iloc[i, j]) > 0.75 else '#17312f')
    fig.colorbar(img, ax=axes[1], shrink=0.75)
    speichern_grafik(fig, out / 'ueberblick')


def unternehmensverteilung(out, data):
    """Verteilung von |CAR| innerhalb der fünf Unternehmen."""
    tickers = sorted(data.ticker.unique())
    fig, ax = plt.subplots(figsize=(9, 5))
    values = [100 * data.loc[data.ticker.eq(ticker), 'abs_car'] for ticker in tickers]
    boxes = ax.boxplot(values, tick_labels=tickers, patch_artist=True)
    for patch in boxes['boxes']:
        patch.set_facecolor(GRUEN)
        patch.set_alpha(0.6)
    ax.set(ylabel='|CAR| (Prozentpunkte)', title='Stärke der Kursreaktion nach Unternehmen')
    speichern_grafik(fig, out / 'abs_car_verteilung')

def grafiken(out, data, coefficients, models, daily, cfg, variants):
    plt.rcParams.update({
        'font.family': 'DejaVu Sans', 'font.size': 10,
        'axes.spines.top': False, 'axes.spines.right': False,
        'axes.labelcolor': '#17312f', 'text.color': '#17312f',
        'axes.titleweight': 'bold', 'figure.facecolor': 'white',
        'savefig.facecolor': 'white', 'axes.grid': True,
        'grid.alpha': 0.15, 'pdf.fonttype': 42,
    })
    for outcome, title in ZIELE.items():
        partielle_zusammenhaenge(out, data, coefficients, models, cfg, outcome, title)
        koeffizienten(out, data, coefficients, cfg, outcome, title)
        diagnostik(out, data, models, outcome, title)
        robustheit(out, cfg, variants, outcome)
    ereignisverlauf(out, data, daily, cfg)
    unternehmensverteilung(out, data)
