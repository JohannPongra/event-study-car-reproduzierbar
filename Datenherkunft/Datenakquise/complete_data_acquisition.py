"""Reproducible local completion using downloaded sources; never invent missing observations.

Run from any directory with Python, pandas, numpy and lxml. Original analysis outputs
are retained. Updated inputs are written to data_completion_results.
"""
from pathlib import Path
from urllib.parse import urljoin, quote
import json, re, hashlib
import numpy as np
import pandas as pd
from lxml import html
from review_event_timing import collect_timing_evidence

A=Path(__file__).resolve().parent
S=A/'verification_20260929/sources'
O=A/'data_completion_results'
O.mkdir(exist_ok=True)

def read(p):return pd.read_csv(A/p,low_memory=False)
def content(p):
 d=html.fromstring(p.read_bytes())
 for n in d.xpath('//script|//style|//nav|//header|//footer'):n.drop_tree()
 return ' '.join(d.text_content().split())
def source(p):return json.loads(p.with_suffix(p.suffix+'.source.json').read_text(encoding='utf-8'))['final_url']
def dump(df,name):df.to_csv(O/name,index=False,encoding='utf-8-sig')
def dates(text):
 text=re.sub(r'(\d)(?:st|nd|rd|th)\b',r'\1',text)
 out=[]
 for m in re.finditer(r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)\s+\d{1,2},?\s+20\d{2}',text,re.I):
  try:out.append(pd.Timestamp(m.group()).strftime('%Y-%m-%d'))
  except ValueError:pass
 return out

events=read('alpha_vantage_earnings/events_master.csv')
master=read('final_regression_results/analysis_dataset.csv')
assert len(events)==170 and events.event_id.is_unique and master.event_id.is_unique
assert set(events.event_id)==set(master.event_id)

# Independently calculate PREVOL directly from daily raw volumes, then compare
# against the saved daily standardized-volume values.
vol=read('market_data/volume_wide.csv').set_index('date')
daily=read('abnormal_volume_results/abnormal_volume_daily.csv')
pre=[]
for r in events.itertuples():
 i=vol.index.get_loc(r.event_trading_day)
 baseline=np.log(vol[r.ticker].iloc[i-60:i-9].astype(float))
 before=np.log(vol[r.ticker].iloc[i-3:i].astype(float))
 assert len(baseline)==51 and len(before)==3 and np.isfinite(baseline).all()
 val=float(((before-baseline.mean())/baseline.std(ddof=1)).mean())
 saved=daily.loc[(daily.event_id==r.event_id)&daily.relative_day.isin([-3,-2,-1]),'standardized_abnormal_volume']
 assert len(saved)==3 and np.isclose(val,saved.mean(),rtol=0,atol=1e-10)
 pre.append({'event_id':r.event_id,'prevol':val,'prevol_days':3,'prevol_baseline_n':51})
pre=pd.DataFrame(pre)
master=master.merge(pre,on='event_id',validate='one_to_one')
dump(pre,'prevol.csv')

# All 9 values (event week + 8 baseline weeks) for an incomplete event are
# taken from one supplemental export; values from differently normalized exports
# are NEVER mixed within the event ratio. Other 165 events remain unchanged.
terms={'AAPL':'Apple stock','AMZN':'Amazon stock','GOOGL':'Google stock','META':'Facebook stock','MSFT':'Microsoft stock'}
gt=read('google_trends_results/google_trends_event_attention.csv').set_index('event_id')
old_gt=gt.copy(deep=True)
changes=[];used=[];overlaps=[];raw_manifest=[]
for ticker,term in terms.items():
 p=S/f'{ticker}_Trends_2017_2018.csv'
 raw=pd.read_csv(p,skiprows=2)
 assert raw.columns[1].startswith(term+':') and 'Vereinigte Staaten' in raw.columns[1]
 values=pd.to_numeric(raw.iloc[:,1].replace({'<1':0.5}),errors='raise')
 series=pd.Series(values.to_numpy(),index=pd.to_datetime(raw.iloc[:,0]))
 assert series.index.is_unique and series.index.to_series().diff().dropna().eq(pd.Timedelta(days=7)).all()
 raw_manifest.append({'ticker':ticker,'term':term,'region':'US','category':'all','search_type':'web','retrieved_date':'2026-09-29','source_url':'https://trends.google.com/trends/explore?date=2017-01-01%202018-12-31&geo=US&q='+quote(term)+'&hl=en-US','file':str(p.relative_to(A)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'weeks':len(series)})
 mask=(gt.ticker==ticker)&gt.google_trends_attention_exact8.isna()
 for eid,row in gt.loc[mask].iterrows():
  ev=events.set_index('event_id').loc[eid]
  t=pd.Timestamp(ev.event_trading_day);w=t-pd.Timedelta(days=(t.dayofweek+1)%7)
  assert str(w.date())==row.google_trends_event_week_start
  prior=pd.DatetimeIndex([w-pd.Timedelta(weeks=k) for k in range(1,9)])
  vals=series.reindex(prior);v=float(series.loc[w])
  assert vals.notna().all() and (vals>0).all() and v>0
  med=float(vals.median());mean=float(vals.mean());sd=float(vals.std(ddof=1));ratio=v/med;score=float(np.log(ratio))
  updates={'google_trends_event_svi':v,'google_trends_baseline_weeks_available':8,'google_trends_baseline_median':med,'google_trends_baseline_mean':mean,'google_trends_attention':score,'google_trends_attention_ratio':ratio,'google_trends_attention_zscore':(v-mean)/sd,'google_trends_attention_exact8':score,'google_trends_attention_ratio_exact8':ratio,'google_trends_attention_zscore_exact8':(v-mean)/sd,'google_trends_uses_fallback_baseline':False,'google_trends_full_sample_usable':True,'google_trends_exact8_usable':True,'google_trends_note':'Eight-week baseline and event week from supplemental 2017-2018 same-query US export; no cross-export mixing.'}
  # Separate pre-event attention measure: previous week relative to its own 8 weeks.
  pw=w-pd.Timedelta(weeks=1);pv=series.reindex([pw-pd.Timedelta(weeks=k) for k in range(1,9)])
  ps=float(np.log(series.loc[pw]/pv.median()))
  updates.update(google_trends_pre_event_attention=ps,google_trends_pre_event_attention_exact8=ps)
  for col,val in updates.items():gt.loc[eid,col]=val
  changes.append({'event_id':eid,'old_search_4_week_fallback':row.google_trends_attention,'new_search_8_weeks':score,'new_event_svi':v,'new_baseline_median':med,'source_file':str(p.relative_to(A))})
  for day in sorted([w,*prior]):used.append({'event_id':eid,'week':day.date().isoformat(),'svi':float(series.loc[day]),'role':'event' if day==w else 'baseline','query':term})
 # Compare overlap with original first export (diagnostic only, no stitching).
 oldpath=next((A/'google_trends_raw').glob(f'{ticker}_*'+('_FB_' if ticker=='META' else '_1_')+'rep*.csv'))
 old=pd.read_csv(oldpath,skiprows=2);oldseries=pd.Series(pd.to_numeric(old.iloc[:,1].replace({'<1':0.5})).to_numpy(),index=pd.to_datetime(old.iloc[:,0]))
 overlap=pd.concat([oldseries.rename('old'),series.rename('new')],axis=1,sort=True).dropna()
 factor=(overlap.old/overlap.new).median()
 overlaps.append({'ticker':ticker,'overlap_weeks':len(overlap),'median_old_new_ratio_diagnostic_only':factor,'correlation':overlap.old.corr(overlap.new),'scaled_median_absolute_error':float((overlap.old-factor*overlap.new).abs().median())})
assert len(changes)==5 and gt.google_trends_attention_exact8.notna().all()
q=gt.google_trends_attention.quantile([.01,.99]);gt['google_trends_attention_winsor_1_99']=gt.google_trends_attention.clip(q.iloc[0],q.iloc[1])
for col in gt.columns:
 if col not in ['ticker','reported_date']:master[col]=master.event_id.map(gt[col])
dump(gt.reset_index(),'google_trends_event_attention_completed.csv')
dump(pd.DataFrame(changes),'google_trends_changes.csv');dump(pd.DataFrame(used),'google_trends_baseline_evidence.csv')
dump(pd.DataFrame(overlaps),'google_trends_overlap_diagnostics.csv');dump(pd.DataFrame(raw_manifest),'google_trends_source_manifest.csv')

# Official date evidence. Feed timestamps without documented time zone are NOT
# accepted as after-close proof, nor are conference-call times alone.
official={};timing={}
for name,ticker,host in [('alphabet','GOOGL','https://abc.xyz'),('meta','META','https://investor.atmeta.com'),('amazon','AMZN','https://ir.aboutamazon.com')]:
 p=S/(name+'_news_body.json');p=p if p.exists() else S/(name+'_news.json')
 for r in json.loads(p.read_text(encoding='utf-8'))['GetPressReleaseListResult']:
  headline=r['Headline'];date=pd.Timestamp(r['PressReleaseDate']).strftime('%Y-%m-%d');url=urljoin(host,r['LinkToDetailPage'])
  if re.search(r'(?:Reports|Announces).*Results',headline,re.I) and not re.search(r'Announces Date|to Announce',headline,re.I):official[(ticker,date)]=(url,headline)
  text=' '.join(html.fromstring(r.get('Body') or '<p/>').text_content().split())
  m=re.search(r'after (?:the )?(?:close of (?:the )?market|market close)[^.]{0,100}',text,re.I)
  if m:
   for d in dates(m.group()):timing[(ticker,d)]=(url,m.group(),'scheduled_after_close_official_notice')
# FinancialReport.ReportDate can be the fiscal period end; never use it as the
# announcement date. For older Amazon events read the linked release dateline.
for p in (S/'events').glob('*.html'):
 ticker,expected=p.stem.split('_',1);text=content(p);url=source(p)
 if ticker=='AAPL':
  m=re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})',p.read_text(encoding='utf-8'))
  if m:official[(ticker,m.group(1))]=(url,'Apple Newsroom datePublished')
 if ticker=='AMZN':
  tree=html.fromstring(p.read_bytes())
  fields=tree.xpath('//*[contains(@class,"module_date-text")]/text()')
  for field in fields:
   for d in dates(field):official[(ticker,d)]=(url,'Amazon release date shown on official page')
 if ticker=='MSFT':
  m=re.search(r'REDMOND,?\s+Wash\.?\s*[—–-]\s*(.{0,30}20\d{2})',text,re.I)
  if m:
   for d in dates(m.group(1)):official[(ticker,d)]=(url,'Microsoft earnings release dateline')
for p in (S/'msft_notices').glob('*.html'):
 text=content(p)
 m=re.search(r'after the close of the market.{0,110}',text,re.I)
 if m:
  for d in dates(m.group()):timing[('MSFT',d)]=(source(p),m.group(),'scheduled_after_close_official_notice')
extra_evidence,timing_review=collect_timing_evidence(A,events)
dump(extra_evidence,'additional_timing_evidence.csv')
dump(timing_review,'timing_review_original_113.csv')
priority={'scheduled_after_close_official_notice':0,'syndicated_release_timestamp':1,
          'reported_after_close_news':2,'historical_calendar_after_close':3,'scheduled_after_close_news':4}
extra_evidence['_priority']=extra_evidence.evidence_type.map(priority)
for row in extra_evidence.sort_values('_priority',kind='stable').drop_duplicates('event_id').itertuples():
 ev=events.set_index('event_id').loc[row.event_id]
 timing.setdefault((ev.ticker,ev.reported_date),(row.source_url,row.evidence,row.evidence_type))
prices=read('market_data/adjusted_close_wide.csv').set_index('date')
audit=[]
for r in events.itertuples():
 key=(r.ticker,r.reported_date);proof=official.get(key);tm=timing.get(key)
 nextday=next(x for x in prices.index if x>r.reported_date)
 audit.append({'event_id':r.event_id,'ticker':r.ticker,'reported_date':r.reported_date,'official_date_confirmed':bool(proof),'date_source_url':proof[0] if proof else '', 'date_evidence':proof[1] if proof else '', 'after_close_supported_by_notice':bool(tm and tm[2]=='scheduled_after_close_official_notice'),'after_close_independently_supported':bool(tm),'timing_evidence_type':tm[2] if tm else 'not_independently_confirmed','timing_source_url':tm[0] if tm else '', 'timing_evidence':tm[1] if tm else '', 'prior_manual_verified_flag':r.report_time_verified,'event_trading_day':r.event_trading_day,'next_session_from_price_calendar':nextday,'trading_day_rule_matches':nextday==r.event_trading_day})
audit=pd.DataFrame(audit);dump(audit,'event_verification.csv')
master=master.merge(audit[['event_id','official_date_confirmed','after_close_supported_by_notice','after_close_independently_supported','timing_evidence_type','trading_day_rule_matches']],on='event_id',validate='one_to_one')
dump(audit.loc[~audit.official_date_confirmed|~audit.after_close_independently_supported],'event_sources_still_needed.csv')

# Alternative complete transcript, kept separately with provenance. Original raw
# data and previously computed FinBERT scores are not silently overwritten.
doc=html.fromstring((S/'GOOGL_2021_Q1_alphastreet.html').read_bytes())
body=doc.xpath('//*[@id="transcript-body"]');assert len(body)==1
turns=[];speaker=None;parts=[]
for p in body[0].xpath('./p'):
 text=' '.join(p.text_content().split())
 if 'speaker-label' in p.get('class','') or (len(text)<140 and re.fullmatch(r'[^—]{2,50} — .*(?:Analyst|Officer|Relations)',text)):
  if speaker and parts:turns.append({'speaker':speaker,'content':'\n\n'.join(parts)})
  speaker=text;parts=[]
 elif speaker and text:parts.append(text)
if speaker and parts:turns.append({'speaker':speaker,'content':'\n\n'.join(parts)})
assert len(turns)>20 and any('Analyst' in t['speaker'] for t in turns)
transcript='\n\n'.join(t['speaker']+'\n'+t['content'] for t in turns)
assert len(transcript.split())>7000
(O/'GOOGL_2021-04-27_complete_transcript.txt').write_text(transcript,encoding='utf-8')
dump(pd.DataFrame(turns),'GOOGL_2021-04-27_complete_transcript_turns.csv')
old_turns=read('earnings_transcripts/transcript_turns.csv')
template=old_turns.loc[old_turns.event_id=='GOOGL_2021-04-27'].iloc[0].to_dict()
replacements=[]
transcript_url=source(S/'GOOGL_2021_Q1_alphastreet.html')
for i,t in enumerate(turns):
 name,separator,title=t['speaker'].partition(' — ')
 row=template|{'turn_index':i,'speaker':name,'speaker_title':title if separator else 'Operator','content':t['content'],'api_sentiment':np.nan,'api_sentiment_score':np.nan,'text_file':str((O/'GOOGL_2021-04-27_complete_transcript.txt').relative_to(A)),'supplement_source_url':transcript_url}
 replacements.append(row)
combined=pd.concat([old_turns.loc[old_turns.event_id!='GOOGL_2021-04-27'],pd.DataFrame(replacements)],ignore_index=True).sort_values(['reported_date','ticker','turn_index'])
assert combined.event_id.nunique()==170 and not combined.duplicated(['event_id','turn_index']).any()
dump(combined,'transcript_turns_completed.csv')
manifest=read('earnings_transcripts/transcript_manifest.csv')
mi=manifest.index[manifest.event_id=='GOOGL_2021-04-27'][0]
for key,value in {'turn_count':len(replacements),'word_count':sum(len(t['content'].split()) for t in turns),'character_count':sum(len(t['content']) for t in turns),'text_file':str((O/'GOOGL_2021-04-27_complete_transcript.txt').relative_to(A)),'raw_file':str((S/'GOOGL_2021_Q1_alphastreet.html').relative_to(A)),'source':'AlphaStreet supplemental transcript','source_endpoint':transcript_url,'processed_at_utc':'','supplement_version':'alphastreet_q1_2021_v1'}.items():
 manifest.loc[mi,key]=value
dump(manifest,'transcript_manifest_completed.csv')
dump(pd.DataFrame([{'event_id':'GOOGL_2021-04-27','source_type':'third_party_published_transcript','source_url':transcript_url,'original_source_retained':True,'old_turns':int((old_turns.event_id=='GOOGL_2021-04-27').sum()),'new_turns':len(turns),'analyst_turns':sum('Analyst' in t['speaker'] for t in turns),'new_words':len(transcript.split()),'sentiment_recalculation_required':True}]),'transcript_replacement_provenance.csv')
master['sentiment_recalculation_required']=True
master['sentiment_recalculation_reason']='New thesis-aligned model, full-call sentence classification and equal sentence weighting for all events.'
master['regression_recalculation_required']=True
dump(master,'analysis_dataset_supplemented.csv')
summary={'events':len(events),'prevol_completed':int(master.prevol.notna().sum()),'strict_8_week_search_completed':int(master.google_trends_attention_exact8.notna().sum()),'google_trends_events_completed':len(changes),'official_dates_confirmed':int(audit.official_date_confirmed.sum()),'after_close_supported_by_official_notice':int(audit.after_close_supported_by_notice.sum()),'after_close_independently_supported':int(audit.after_close_independently_supported.sum()),'timing_evidence_types':audit.timing_evidence_type.value_counts().to_dict(),'original_113_timing_gaps_closed':int(timing_review.additional_source_found.sum()),'event_day_rule_matches':int(audit.trading_day_rule_matches.sum()),'supplemental_transcript_turns':len(turns),'supplemental_transcript_words':len(transcript.split()),'transcript_source':source(S/'GOOGL_2021_Q1_alphastreet.html'),'regressions_rerun':False,'sentiment_rerun':False,'original_data_preserved':True}
(O/'completion_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
print(json.dumps(summary,indent=2))
