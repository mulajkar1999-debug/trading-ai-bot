import os, time, logging, requests
from datetime import datetime, timezone
import pandas as pd
import numpy as np

# ================== CONFIG ==================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN','')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID','')
SCAN_INTERVAL = int(os.getenv('SCAN_INTERVAL','30'))
MIN_CONFLUENCE = int(os.getenv('MIN_CONFLUENCE','85'))
PAPER_BALANCE = float(os.getenv('PAPER_BALANCE','10000'))
RISK_PERCENT = float(os.getenv('RISK_PERCENT','1'))
MARKETS = [x.strip().upper() for x in os.getenv('MARKETS','BTCUSD,XAUUSD,ETHUSDT,GBPUSD').split(',') if x.strip()]

CONFIG={
 'BTCUSD':('yahoo','BTC-USD',2),
 'ETHUSDT':('binance','ETHUSDT',2),
 'GBPUSD':('yahoo','GBPUSD=X',5),
}
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('TradeBrain')

# ================== TELEGRAM ==================
def telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        r=requests.post(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',json={'chat_id':TELEGRAM_CHAT_ID,'text':text,'parse_mode':'HTML'},timeout=15)
        if r.status_code!=200: log.error('Telegram: %s',r.text[:300])
    except Exception as e: log.error('Telegram error: %s',e)

# ================== DATA ==================
def empty(): return pd.DataFrame(columns=['open','high','low','close','volume'])

def yahoo(ticker, interval, limit=300):
    ranges={'1m':'7d','5m':'60d','15m':'60d','1h':'730d','1d':'10y'}
    try:
        r=requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}',params={'range':ranges[interval],'interval':interval,'includePrePost':'false'},headers={'User-Agent':'Mozilla/5.0'},timeout=20); r.raise_for_status()
        z=r.json()['chart']['result'][0]; q=z['indicators']['quote'][0]
        df=pd.DataFrame(q,index=pd.to_datetime(z['timestamp'],unit='s',utc=True))
        for c in ['open','high','low','close','volume']: df[c]=pd.to_numeric(df[c],errors='coerce')
        return df.dropna(subset=['open','high','low','close']).tail(limit).reset_index(drop=True)
    except Exception as e: log.error('Yahoo %s %s: %s',ticker,interval,e); return empty()

def binance(symbol, interval, limit=300):
    try:
        r=requests.get('https://api.binance.com/api/v3/klines',params={'symbol':symbol,'interval':interval,'limit':limit},timeout=15); r.raise_for_status()
        rows=r.json(); df=pd.DataFrame(rows,columns=['t','open','high','low','close','volume','ct','qv','trades','tb','tq','x'])
        for c in ['open','high','low','close','volume']: df[c]=pd.to_numeric(df[c],errors='coerce')
        return df[['open','high','low','close','volume']].dropna().reset_index(drop=True)
    except Exception as e: log.error('Binance %s %s: %s',symbol,interval,e); return empty()

def data(market,tf):
    source,ticker,_=CONFIG[market]
    # Yahoo has no native 4h; resample 1h to 4h.
    if tf=='4h':
        d=data(market,'1h')
        if d.empty:return d
        d=d.copy(); d['dt']=pd.date_range(end=pd.Timestamp.now(tz='UTC'),periods=len(d),freq='h')
        d=d.set_index('dt')
        return d.resample('4h').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna().tail(300).reset_index(drop=True)
    if source=='binance': return binance(ticker,tf)
    return yahoo(ticker,tf)

# ================== RULEBOOK ENGINE ==================
def swings(df):
    hs=[]; ls=[]
    for i in range(2,len(df)-2):
        if df.high.iloc[i]>df.high.iloc[i-2:i].max() and df.high.iloc[i]>df.high.iloc[i+1:i+3].max(): hs.append(float(df.high.iloc[i]))
        if df.low.iloc[i]<df.low.iloc[i-2:i].min() and df.low.iloc[i]<df.low.iloc[i+1:i+3].min(): ls.append(float(df.low.iloc[i]))
    return hs,ls

def structure(df):
    hs,ls=swings(df); labels=[]
    if len(hs)>=2: labels.append('HH' if hs[-1]>hs[-2] else 'LH')
    if len(ls)>=2: labels.append('HL' if ls[-1]>ls[-2] else 'LL')
    trend='BULLISH' if 'HH' in labels and 'HL' in labels else 'BEARISH' if 'LH' in labels and 'LL' in labels else 'RANGE'
    return trend,labels,(hs[-1] if hs else None),(ls[-1] if ls else None)

def choch(df):
    trend,labels,h,l=structure(df)
    if h is None or l is None:return 'NONE'
    a,b=float(df.close.iloc[-2]),float(df.close.iloc[-1])
    if trend=='BEARISH' and a>h and b>h:return 'BULLISH_CHOCH'
    if trend=='BULLISH' and a<l and b<l:return 'BEARISH_CHOCH'
    return 'NONE'

def atr(df,n=14):
    pc=df.close.shift(1); tr=pd.concat([df.high-df.low,(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1); return float(tr.rolling(n).mean().iloc[-1]) if len(df)>n else 0

def analyze(market):
    tf={x:data(market,x) for x in ['1d','4h','1h','15m','5m','1m']}
    if any(len(x)<60 for x in tf.values()): return None
    s={x:structure(tf[x]) for x in ['1d','4h','1h','5m','1m']}
    d,h4,h1=s['1d'],s['4h'],s['1h']; priority=None
    if d[0]==h4[0]==h1[0] and d[0] in ('BULLISH','BEARISH'): priority=('1h',d[0])
    elif d[0]==h4[0] and d[0] in ('BULLISH','BEARISH'): priority=('4h',d[0])
    elif h4[0]==h1[0] and h4[0] in ('BULLISH','BEARISH'): priority=('1d',d[0])
    price=float(tf['1m'].close.iloc[-1]); checks={}
    if not priority:return {'market':market,'signal':'WAIT','price':price,'confidence':0,'reason':'HTF priority not aligned'}
    ptdir=priority[1]; p_tf=priority[0]; ps=s[p_tf]; zone=ps[2] if ptdir=='BEARISH' else ps[3]
    a=atr(tf['1m']) or price*.002
    zone_tap=zone is not None and abs(price-zone)<=a*.5
    lower_tf='1m' if p_tf=='1h' else '5m' if p_tf=='4h' else '1h'
    expected='BULLISH_CHOCH' if ptdir=='BULLISH' else 'BEARISH_CHOCH'
    checks['HTF_DIRECTION']=True; checks['STRUCTURE']=('HH' in ps[1] and 'HL' in ps[1]) if ptdir=='BULLISH' else ('LH' in ps[1] and 'LL' in ps[1]); checks['PRICE_TAP']=zone_tap; checks['LOWER_TF_CHOCH']=choch(tf[lower_tf])==expected
    c1,c2=tf['1m'].iloc[-2],tf['1m'].iloc[-1]
    checks['TWO_CANDLE_RETRACEMENT']=(c1.close<c1.open and c2.close>c2.open) if ptdir=='BULLISH' else (c1.close>c1.open and c2.close<c2.open)
    checks['CONFIRMATION']=('BULLISH' if c2.close>c2.open else 'BEARISH')==ptdir
    rng=float(c2.high-c2.low); checks['NO_FAKEOUT']=rng>0 and abs(float(c2.close-c2.open))/rng>=.3
    checks['LEVEL_NOT_PLAYED']=True
    weights={'HTF_DIRECTION':20,'STRUCTURE':15,'PRICE_TAP':15,'LOWER_TF_CHOCH':20,'TWO_CANDLE_RETRACEMENT':10,'CONFIRMATION':10,'NO_FAKEOUT':5,'LEVEL_NOT_PLAYED':5}
    score=sum(weights[k] for k,v in checks.items() if v); mandatory=all(checks[k] for k in ['HTF_DIRECTION','STRUCTURE','PRICE_TAP','LOWER_TF_CHOCH','TWO_CANDLE_RETRACEMENT','CONFIRMATION','NO_FAKEOUT','LEVEL_NOT_PLAYED'])
    signal='BUY' if ptdir=='BULLISH' and mandatory and score>=MIN_CONFLUENCE else 'SELL' if ptdir=='BEARISH' and mandatory and score>=MIN_CONFLUENCE else 'WAIT'
    entry=price; sl=entry-a*1.5 if signal=='BUY' else entry+a*1.5; tp1=entry+(entry-sl)*1.5 if signal=='BUY' else entry-(sl-entry)*1.5; tp2=entry+(entry-sl)*2.5 if signal=='BUY' else entry-(sl-entry)*2.5
    return {'market':market,'signal':signal,'price':price,'confidence':score,'direction':ptdir,'priority_tf':p_tf,'checks':checks,'entry':entry,'sl':sl,'tp1':tp1,'tp2':tp2}

# ================== PAPER TRADER ==================
class PaperTrader:
    def __init__(self): self.balance=PAPER_BALANCE; self.open={}; self.total=0; self.wins=0; self.losses=0
    def open_trade(self,s):
        if s['signal'] not in ('BUY','SELL') or s['market'] in self.open:return False
        self.open[s['market']]=s; self.total+=1; return True
    def check(self,market,price):
        t=self.open.get(market)
        if not t:return None
        win=(price>=t['tp2'] if t['signal']=='BUY' else price<=t['tp2']); loss=(price<=t['sl'] if t['signal']=='BUY' else price>=t['sl'])
        if not(win or loss):return None
        exitp=t['tp2'] if win else t['sl']; pnl=(exitp-t['entry']) if t['signal']=='BUY' else (t['entry']-exitp); self.balance+=pnl
        self.wins+=win; self.losses+=loss; del self.open[market]; return (t,exitp,'WIN' if win else 'LOSS',pnl)
    def wr(self): return self.wins/self.total*100 if self.total else 0

# ================== MAIN ==================
def fmt(m,v): return f'{v:.{CONFIG[m][2]}f}'
def main():
    log.info('START | Markets=%s | Scan=%ss | MinConfluence=%s | Paper=%s | Risk=%s%%',','.join(MARKETS),SCAN_INTERVAL,MIN_CONFLUENCE,PAPER_BALANCE,RISK_PERCENT)
    telegram(f'🚀 <b>TradeBrain AI Started</b>\nMarkets: {", ".join(MARKETS)}\nMode: PAPER TRADING\nScan: {SCAN_INTERVAL}s')
    trader=PaperTrader(); last={}
    while True:
        for m in MARKETS:
            if m not in CONFIG: continue
            try:
                s=analyze(m)
                if not s: continue
                log.info('%s | %s | Price %s | Confidence %s%% | Bias %s',m,s['signal'],fmt(m,s['price']),s['confidence'],s.get('direction','NONE'))
                done=trader.check(m,s['price'])
                if done:
                    t,ex,res,pnl=done; telegram(f'{"✅" if res=="WIN" else "❌"} <b>{m} PAPER {res}</b>\nSide: {t["signal"]}\nEntry: {fmt(m,t["entry"])}\nExit: {fmt(m,ex)}\nP/L: {pnl:.6f}\nWin Rate: {trader.wr():.2f}%')
                if s['signal'] in ('BUY','SELL'):
                    key=(s['signal'],round(s['entry'],6),s['priority_tf'])
                    if last.get(m)!=key and trader.open_trade(s):
                        last[m]=key
                        telegram(f'{"🟢" if s["signal"]=="BUY" else "🔴"} <b>{m} {s["signal"]}</b>\nPrice: {fmt(m,s["price"])}\nConfidence: {s["confidence"]}%\nPriority: {s["priority_tf"]}\nEntry: {fmt(m,s["entry"])}\nSL: {fmt(m,s["sl"])}\nTP1: {fmt(m,s["tp1"])}\nTP2: {fmt(m,s["tp2"])}\n<b>PAPER TRADE</b>')
            except Exception as e: log.exception('%s error: %s',m,e)
        time.sleep(SCAN_INTERVAL)
if __name__=='__main__': main()
