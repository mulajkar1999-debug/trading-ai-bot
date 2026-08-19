import os, time, logging, requests
from datetime import datetime
import pandas as pd
import numpy as np

# CONFIG
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN','')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID','')
SCAN_INTERVAL = 60
MIN_CONFLUENCE = 80
PAPER_BALANCE = 10000
RISK_PERCENT = 1
MARKETS = ['GBPUSD','BTCUSD','XAUUSD']

CONFIG = {
    'BTCUSD':('yahoo','BTC-USD',2),
    'ETHUSDT':('binance','ETHUSDT',2),
    'GBPUSD':('yahoo','GBPUSD=X',5),
    'XAUUSD':('yahoo','GC=F',2),
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('TradeBrain')

# TELEGRAM
def telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: 
        log.info('Telegram: %s', text[:100])
        return
    try:
        requests.post(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
                     json={'chat_id':TELEGRAM_CHAT_ID,'text':text,'parse_mode':'HTML'},timeout=10)
    except Exception as e:
        log.error('Telegram error: %s', e)

# DATA
def get_data(market, tf):
    source, ticker, _ = CONFIG.get(market, (None, None, None))
    if not source: return pd.DataFrame()
    
    try:
        if source == 'binance':
            r = requests.get('https://api.binance.com/api/v3/klines',
                           params={'symbol':ticker,'interval':tf,'limit':300},timeout=10)
            rows = r.json()
            df = pd.DataFrame(rows, columns=['t','o','h','l','c','v','ct','qv','t2','tb','tq','x'])
            df = df[['o','h','l','c','v']].astype(float)
        else:
            ranges = {'1m':'7d','5m':'60d','15m':'60d','1h':'730d','4h':'730d','1d':'5y'}
            r = requests.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}',
                           params={'range':ranges.get(tf,'60d'),'interval':tf},timeout=10)
            z = r.json()['chart']['result'][0]
            q = z['indicators']['quote'][0]
            df = pd.DataFrame(q)
            df = df[['open','high','low','close','volume']].astype(float)
        
        df.columns = ['o','h','l','c','v']
        return df.dropna().tail(300).reset_index(drop=True)
    except Exception as e:
        log.error('Data fetch error %s %s: %s', market, tf, e)
        return pd.DataFrame()

# STRATEGY - PROPER CHOCH + TGL
def find_swings(df, lb=5):
    hs, ls = [], []
    if len(df) < lb*2: return hs, ls
    
    for i in range(lb, len(df)-lb):
        if df['h'].iloc[i] > df['h'].iloc[max(0,i-lb):i].max() and \
           df['h'].iloc[i] > df['h'].iloc[i+1:min(len(df),i+lb+1)].max():
            hs.append(float(df['h'].iloc[i]))
        if df['l'].iloc[i] < df['l'].iloc[max(0,i-lb):i].min() and \
           df['l'].iloc[i] < df['l'].iloc[i+1:min(len(df),i+lb+1)].min():
            ls.append(float(df['l'].iloc[i]))
    return hs, ls

def structure(df):
    hs, ls = find_swings(df)
    labels = []
    if len(hs) >= 2: labels.append('HH' if hs[-1] > hs[-2] else 'LH')
    if len(ls) >= 2: labels.append('HL' if ls[-1] > ls[-2] else 'LL')
    trend = 'BULLISH' if 'HH' in labels and 'HL' in labels else 'BEARISH' if 'LH' in labels and 'LL' in labels else 'RANGE'
    return trend, labels, (hs[-1] if hs else None), (ls[-1] if ls else None), hs, ls

def choch(df, trend):
    if len(df) < 2: return False
    c1, c2 = float(df['c'].iloc[-2]), float(df['c'].iloc[-1])
    _, _, h, l, _, _ = structure(df)
    if trend == 'BEARISH' and h and c1 > h and c2 > h: return True
    if trend == 'BULLISH' and l and c1 < l and c2 < l: return True
    return False

def two_candle(df, trend):
    if len(df) < 2: return False
    c1, c2 = df.iloc[-2], df.iloc[-1]
    if trend == 'BULLISH':
        return c1['c'] < c1['o'] and c2['c'] < c2['o'] and c2['c'] < c1['l']
    return c1['c'] > c1['o'] and c2['c'] > c2['o'] and c2['c'] > c1['h']

def atr(df, n=14):
    if len(df) < n: return None
    tr = pd.concat([df['h']-df['l'], (df['h']-df['c'].shift()).abs(), (df['l']-df['c'].shift()).abs()], axis=1).max(axis=1)
    val = tr.rolling(n).mean().iloc[-1]
    return float(val) if val and not np.isnan(val) else None

def analyze(market):
    tf = {x: get_data(market, x) for x in ['1d','4h','1h','5m','1m']}
    if any(len(tf.get(x, pd.DataFrame())) < 60 for x in tf): 
        return None
    
    s = {x: structure(tf[x]) for x in ['1d','4h','1h','5m','1m']}
    d, h4, h1 = s['1d'], s['4h'], s['1h']
    
    # MTF Alignment - CONDITION 1, 2, 3
    priority = None
    if d[0] == h4[0] == h1[0] and d[0] in ('BULLISH','BEARISH'): 
        priority = ('1h', d[0])
    elif d[0] == h4[0] and d[0] in ('BULLISH','BEARISH'): 
        priority = ('4h', d[0])
    elif h4[0] == h1[0] and h4[0] in ('BULLISH','BEARISH'): 
        priority = ('1d', d[0])
    
    if not priority: 
        return {'market':market, 'signal':'WAIT', 'reason':'No MTF alignment', 'conf':0}
    
    ptf, direction = priority
    price = float(tf['1m']['c'].iloc[-1])
    
    # Get zone from priority TF
    _, _, h_zone, l_zone, _, _ = s[ptf]
    zone = h_zone if direction == 'BEARISH' else l_zone
    a = atr(tf['1m']) or price*0.002
    zone_tap = zone is not None and abs(price - zone) <= a*0.5
    
    # Lower TF confirmation
    ltf = '1m' if ptf == '1h' else '5m' if ptf == '4h' else '1h'
    choch_ok = choch(tf[ltf], s[ltf][0])
    expected = (direction == 'BULLISH' and choch_ok and s[ltf][0] == 'BEARISH') or \
               (direction == 'BEARISH' and choch_ok and s[ltf][0] == 'BULLISH')
    
    # All checks
    checks = {}
    checks['STRUCTURE'] = ('HH' in s[ptf][1] and 'HL' in s[ptf][1]) if direction == 'BULLISH' else ('LH' in s[ptf][1] and 'LL' in s[ptf][1])
    checks['ZONE_TAP'] = zone_tap
    checks['CHOCH'] = expected
    checks['2CANDLE'] = two_candle(tf['1m'], direction)
    
    c = tf['1m'].iloc[-1]
    checks['CANDLE'] = (c['c'] > c['o'] and direction == 'BULLISH') or (c['c'] < c['o'] and direction == 'BEARISH')
    
    candle_range = c['h'] - c['l']
    checks['BODY'] = (abs(c['c']-c['o'])/candle_range >= 0.3) if candle_range > 0 else False
    
    # Confluence Score
    weights = {'STRUCTURE':25, 'ZONE_TAP':20, 'CHOCH':25, '2CANDLE':15, 'CANDLE':10, 'BODY':5}
    score = sum(weights[k] for k in checks if checks[k])
    mandatory = all(checks[k] for k in ['STRUCTURE','ZONE_TAP','CHOCH','2CANDLE'])
    
    signal = 'BUY' if direction == 'BULLISH' and mandatory and score >= MIN_CONFLUENCE else \
             'SELL' if direction == 'BEARISH' and mandatory and score >= MIN_CONFLUENCE else 'WAIT'
    
    entry = price
    sl = entry - a*1.5 if signal == 'BUY' else entry + a*1.5
    tp1 = entry + (entry-sl)*1.5 if signal == 'BUY' else entry - (sl-entry)*1.5
    tp2 = entry + (entry-sl)*2.5 if signal == 'BUY' else entry - (sl-entry)*2.5
    
    return {
        'market':market, 
        'signal':signal, 
        'price':price, 
        'conf':score, 
        'direction':direction,
        'ptf':ptf, 
        'entry':entry, 
        'sl':sl, 
        'tp1':tp1, 
        'tp2':tp2, 
        'checks':checks
    }

# PAPER TRADER
class Trader:
    def __init__(self): 
        self.bal = PAPER_BALANCE
        self.open = {}
        self.total = 0
        self.wins = 0
        self.losses = 0
    
    def open_trade(self, s):
        if s['signal'] not in ('BUY','SELL') or s['market'] in self.open: 
            return False
        self.open[s['market']] = s
        self.total += 1
        return True
    
    def check(self, market, price):
        t = self.open.get(market)
        if not t: return None
        
        win = (price >= t['tp2'] if t['signal'] == 'BUY' else price <= t['tp2'])
        loss = (price <= t['sl'] if t['signal'] == 'BUY' else price >= t['sl'])
        
        if not (win or loss): return None
        
        exitp = t['tp2'] if win else t['sl']
        pnl = (exitp - t['entry']) if t['signal'] == 'BUY' else (t['entry'] - exitp)
        self.bal += pnl
        
        if win: self.wins += 1
        else: self.losses += 1
        
        del self.open[market]
        return (t, exitp, 'WIN' if win else 'LOSS', pnl)
    
    def wr(self): 
        return (self.wins/self.total*100) if self.total else 0

# MAIN
def main():
    log.info('='*70)
    log.info('TradeBrain AI Started')
    log.info('Markets: %s', ', '.join(MARKETS))
    log.info('Mode: PAPER TRADING')
    log.info('Win Rate Target: 55-65%%')
    log.info('='*70)
    
    telegram('🚀 <b>TradeBrain AI Started</b>\n' +
             'Markets: '+', '.join(MARKETS)+'\n' +
             'Mode: PAPER TRADING\n' +
             'Win Rate Target: 55-65%')
    
    trader = Trader()
    last = {}
    
    try:
        while True:
            for m in MARKETS:
                try:
                    r = analyze(m)
                    if not r: continue
                    
                    log.info('%s | Signal=%s | Conf=%d%% | Price=%.2f | Direction=%s', 
                            m, r['signal'], r['conf'], r['price'], r.get('direction','?'))
                    
                    # Check existing trades
                    done = trader.check(m, r['price'])
                    if done:
                        t, ex, res, pnl = done
                        wr = trader.wr()
                        msg = f'{"✅" if res=="WIN" else "❌"} <b>{m} {res}</b>\n'
                        msg += f'Entry: {t["entry"]:.2f}\n'
                        msg += f'Exit: {ex:.2f}\n'
                        msg += f'P/L: {pnl:.4f}\n'
                        msg += f'Win Rate: {wr:.1f}%\n'
                        msg += f'Total Trades: {trader.total}'
                        telegram(msg)
                    
                    # New signal
                    if r['signal'] in ('BUY','SELL'):
                        key = (r['signal'], round(r['entry'], 6), r['ptf'])
                        if last.get(m) != key and trader.open_trade(r):
                            last[m] = key
                            wr = trader.wr()
                            msg = f'{"🟢" if r["signal"]=="BUY" else "🔴"} <b>{m} {r["signal"]}</b>\n'
                            msg += f'Confidence: {r["conf"]}%\n'
                            msg += f'Priority TF: {r["ptf"]}\n'
                            msg += f'Price: {r["price"]:.2f}\n'
                            msg += f'Entry: {r["entry"]:.2f}\n'
                            msg += f'SL: {r["sl"]:.2f}\n'
                            msg += f'TP1: {r["tp1"]:.2f}\n'
                            msg += f'TP2: {r["tp2"]:.2f}\n'
                            msg += f'---\n'
                            msg += f'Win Rate: {wr:.1f}%\n'
                            msg += f'Open Trades: {len(trader.open)}\n'
                            msg += f'Total Trades: {trader.total}'
                            telegram(msg)
                
                except Exception as e:
                    log.exception('%s error: %s', m, e)
            
            time.sleep(SCAN_INTERVAL)
    
    except KeyboardInterrupt:
        log.info('Shutdown requested')
        telegram(f'🛑 TradeBrain Stopped\nFinal Win Rate: {trader.wr():.1f}%\nTotal Trades: {trader.total}')
    
    except Exception as e:
        log.exception('Fatal error: %s', e)
        telegram(f'💥 TradeBrain Error: {str(e)[:100]}')

if __name__ == '__main__':
    main()
