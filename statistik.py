"""Regressionen, Diagnostik, Mittelwerttests und Robustheitsprüfungen.

Die Vollmodelle enthalten Unternehmens-, Jahres- und Quartalseffekte.
Standardfehler: CR1 nach Ereignishandelstag; t-Tests mit G-1 Freiheitsgraden.
"""
import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools.tools import add_constant
from statsmodels.stats.diagnostic import het_breuschpagan
from statsmodels.stats.stattools import jarque_bera
from statsmodels.stats.outliers_influence import variance_inflation_factor


def modell_predictors(cfg, outcome, predictors=None):
    selected = cfg['predictors'] if predictors is None else predictors
    return ['abs_sue' if name == 'sue' and outcome == 'abs_car' else name
            for name in selected]


def design(data, predictors, fixed_effects):
    x = data[predictors].astype(float).copy()
    if fixed_effects:
        for name in ['ticker', 'year', 'quarter']:
            x = pd.concat([x, pd.get_dummies(data[name].astype(str), prefix=name, drop_first=True, dtype=float)], axis=1)
    return add_constant(x, has_constant='add')

def fit(data, outcome, predictors, cfg):
    predictors = modell_predictors(cfg, outcome, predictors)
    data = data.copy()
    data['abs_sue'] = data.sue.abs()
    x = design(data, predictors, cfg['fixed_effects'])
    y = data[outcome].astype(float)
    if not np.isfinite(x.to_numpy()).all() or not np.isfinite(y.to_numpy()).all():
        raise ValueError('Regressionsstichprobe enthält fehlende/ungültige Werte.')
    if len(data) <= x.shape[1] + 2 or np.linalg.matrix_rank(x) < x.shape[1]:
        raise ValueError('Zu wenige Beobachtungen oder linear abhängige Einflussgrößen/Feste Effekte.')
    ordinary = OLS(y, x).fit()
    if data.event_trading_day.nunique() < 2:
        raise ValueError('Mindestens zwei Ereignishandelstage für Datumscluster erforderlich.')
    robust = ordinary.get_robustcov_results(
        cov_type='cluster', groups=data.event_trading_day,
        use_correction=True, df_correction=True, use_t=True)

    return (ordinary, robust, x)

def koeffizienten(robust, x, model, outcome, data, cfg):
    interval = robust.conf_int(alpha=0.05)
    return [{
        'model': model, 'outcome': outcome, 'variable': col,
        'coefficient': robust.params[i], 'standard_error': robust.bse[i],
        't': robust.tvalues[i], 'p': robust.pvalues[i],
        'ci_lower': interval[i, 0], 'ci_upper': interval[i, 1],
        'n': len(data), 'clusters': data.event_trading_day.nunique(),
        'covariance': cfg['covariance'],
    } for i, col in enumerate(x.columns)]

def diagnose(ordinary, x, data, model, outcome):
    bp = het_breuschpagan(ordinary.resid, x)
    jb = jarque_bera(ordinary.resid)
    influence = ordinary.get_influence()
    residuals = pd.DataFrame({
        'event_id': data.event_id, 'ticker': data.ticker, 'outcome': outcome,
        'actual': data[outcome], 'fitted': ordinary.fittedvalues,
        'residual': ordinary.resid,
        'studentized_residual': influence.resid_studentized_internal,
        'leverage': influence.hat_matrix_diag,
        'cooks_distance': influence.cooks_distance[0],
    })
    vif = [{'model': model, 'variable': col,
            'vif': variance_inflation_factor(x.to_numpy(), i)}
           for i, col in enumerate(x.columns) if col != 'const']
    info = {
        'model': model, 'outcome': outcome, 'n': len(data),
        'parameters': x.shape[1], 'r_squared': ordinary.rsquared,
        'adjusted_r_squared': ordinary.rsquared_adj,
        'rmse': np.sqrt(np.mean(ordinary.resid ** 2)),
        'mae': np.mean(abs(ordinary.resid)), 'bp_lm_p': bp[1], 'bp_f_p': bp[3],
        'jb_p': jb[1], 'skewness': jb[2], 'kurtosis': jb[3],
        'cook_threshold': 4 / len(data),
        'cook_flagged': int((influence.cooks_distance[0] > 4 / len(data)).sum()),
    }
    return (info, residuals, vif)

def mittelwert_test(values, groups, label):
    """AAR/CAAR gegen Null; CR1 nach Datum mit G-1 Freiheitsgraden."""
    if len(values) < 3 or pd.Series(groups).nunique() < 2:
        return {'statistic': label, 'n': len(values), 'mean': np.mean(values), 'p': np.nan, 'note': 'Zu wenige Beobachtungen/Cluster'}
    ordinary = OLS(np.asarray(values, dtype=float), np.ones((len(values), 1))).fit()
    result = ordinary.get_robustcov_results(
        cov_type='cluster', groups=np.asarray(groups),
        use_correction=True, df_correction=True, use_t=True)
    low, high = result.conf_int()[0]
    return {
        'statistic': label, 'n': len(values), 'clusters': pd.Series(groups).nunique(),
        'mean': result.params[0], 'standard_error': result.bse[0],
        't': result.tvalues[0], 'p': result.pvalues[0],
        'ci_lower': low, 'ci_upper': high, 'note': 'CR1 nach Ereignishandelstag',
    }

def event_study(data, daily):
    daily = daily.loc[daily.event_id.isin(data.event_id)].copy()
    daily.sort_values(['event_id', 'relative_day'], inplace=True)
    daily['cumulative_ar'] = daily.groupby('event_id').ar.cumsum()
    rows = [mittelwert_test(data.car, data.event_trading_day, 'CAAR gesamtes Ereignisfenster')]
    for day, part in daily.groupby('relative_day'):
        rows.append(mittelwert_test(part.ar, part.event_trading_day, f'AAR Tag {day:+d}'))
        rows.append(mittelwert_test(part.cumulative_ar, part.event_trading_day, f'CAAR bis Tag {day:+d}'))
    return (pd.DataFrame(rows), daily)


def robustheitsmodelle(data, cfg, alternatives):
    """Winsorisierung, fehlende feste Effekte, Firmenauswahl und CAR-Fenster."""
    rows = []

    def schaetzen(label, sample, outcome, feste_effekte=True):
        settings = {**cfg, 'fixed_effects': feste_effekte}
        _, robust, x = fit(sample, outcome, cfg['predictors'], settings)
        rows.extend(koeffizienten(robust, x, label, outcome, sample, settings))

    # Nur SUE, VOL und PREVOL auf das 1./99. Perzentil begrenzen.
    winsor = data.copy()
    for col in ['sue', 'vol', 'prevol']:
        low, high = data[col].quantile([0.01, 0.99])
        winsor[col] = data[col].clip(low, high)
    winsor['abs_sue'] = winsor.sue.abs()
    for outcome in ['car', 'abs_car']:
        schaetzen('ohne feste Effekte', data, outcome, False)
        schaetzen('Winsorisierung SUE VOL PREVOL 1/99', winsor, outcome)
        schaetzen('Unternehmen: alle', data, outcome)
        for ticker in sorted(data.ticker.unique()):
            schaetzen('Unternehmen: ohne ' + ticker, data.loc[data.ticker.ne(ticker)], outcome)
    for window, calculated in alternatives.items():
        by_id = calculated.set_index('event_id')
        alternative = data.copy()
        alternative['car'] = alternative.event_id.map(by_id.car)
        alternative['abs_car'] = alternative.car.abs()
        for outcome in ['car', 'abs_car']:
            schaetzen('Ereignisfenster ' + window, alternative, outcome)
    return pd.DataFrame(rows)


def auswerten(data, events, daily, alternatives):
    """Zwei Vollmodelle, zehn Einzelmodelle und die Prüfungen der Seminararbeit."""
    cfg = {'predictors': ['sue', 'vol', 'prevol', 'search', 'tone'],
           'fixed_effects': True, 'covariance': 'cluster'}
    models, coefficients, diagnostics, residuals, vifs = {}, [], [], [], []
    for outcome in ['car', 'abs_car']:
        predictors = modell_predictors(cfg, outcome)
        ordinary, robust, x = fit(data, outcome, predictors, cfg)
        models[outcome] = ordinary, robust, x
        coefficients.extend(koeffizienten(robust, x, 'Vollmodell', outcome, data, cfg))
        info, resid, vif = diagnose(ordinary, x, data, 'Vollmodell', outcome)
        info.update(robust_f=float(robust.fvalue), robust_f_p=float(robust.f_pvalue),
                    covariance='cluster')
        diagnostics.append(info)
        residuals.append(resid)
        vifs.extend(vif)
        for variable in predictors:
            _, single, single_x = fit(data, outcome, [variable], cfg)
            coefficients.extend(koeffizienten(single, single_x,
                                'Einzelmodell ' + variable, outcome, data, cfg))

    study, daily = event_study(events, daily)
    variants = robustheitsmodelle(data, cfg, alternatives)
    columns = ['car', 'abs_car', *cfg['predictors'], 'abs_sue']
    tables = {
        'analysedaten': data,
        'alle_ereignisse': events,
        'ausschluesse': pd.DataFrame(columns=['event_id', 'ticker', 'reason']),
        'koeffizienten': pd.DataFrame(coefficients),
        'diagnostik': pd.DataFrame(diagnostics),
        'residuen': pd.concat(residuals, ignore_index=True),
        'vif': pd.DataFrame(vifs),
        'event_study': study,
        'robustheit': variants,
        'unternehmensauswahl': variants.loc[variants.model.str.startswith('Unternehmen: ')],
        'beschreibung': data[columns].describe().T.rename_axis('variable').reset_index(),
        'korrelationen': data[columns].corr().rename_axis('variable').reset_index(),
    }
    return tables, models, daily, cfg
