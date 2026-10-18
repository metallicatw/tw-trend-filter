# -*- coding: utf-8 -*-

from __future__ import annotations

import warnings
warnings.filterwarnings('ignore')

import os, sys, json, datetime, platform, subprocess as _sp
import time as _time
from dataclasses import dataclass, fields
from html import escape
from io import StringIO

import requests
import yfinance as yf
from curl_cffi import requests as _cffi_requests
import pandas as pd
import numpy as np

_YF_SESSION = _cffi_requests.Session(impersonate='chrome')

PLOTLY_CDN = "https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.35.3/plotly.min.js"

PLOTLY_CDN_FALLBACK = "https://cdn.plot.ly/plotly-2.35.2.min.js"

DEFAULT_WORKERS = 4

RETRY_ROUNDS = ((45, 2), (90, 1), (180, 1))

_RATE_LIMIT_MARKS = ('ratelimit', 'too many requests', '429')


NO_DATA = '兩次下載都回空的'


def _is_rate_limited(why: str) -> bool:
    w = (why or '').lower()
    return any(m in w for m in _RATE_LIMIT_MARKS)


def _worth_retry(why: str) -> bool:
    return bool(why) and why != NO_DATA


MASS_EMPTY_RATIO = 0.01


def _mass_empty(n_empty: int, universe: int) -> bool:
    return universe > 0 and n_empty > universe * MASS_EMPTY_RATIO


RATE_LIMIT_STREAK = 5

RATE_LIMIT_COOLDOWN = 60.0

RATE_LIMIT_TOTAL_BUDGET = 900.0

RATE_LIMIT_MAX_COOLDOWN = 240.0


class RateLimitBreaker:

    def __init__(self, streak: int = RATE_LIMIT_STREAK,
                 cooldown: float = RATE_LIMIT_COOLDOWN,
                 max_cooldown: float = RATE_LIMIT_MAX_COOLDOWN,
                 max_total_sleep: float = RATE_LIMIT_TOTAL_BUDGET,
                 sleep=None, clock=None):
        import threading as _threading
        self.streak_limit = streak
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self.max_total_sleep = max_total_sleep
        self._sleep = sleep or _time.sleep
        self._clock = clock or _time.monotonic
        self._lock = _threading.Lock()
        self._streak = 0
        self._level = 0
        self._cool_until = None
        self.trips = 0
        self.slept = 0.0

    def note_ok(self) -> None:
        with self._lock:
            self._streak = 0
            self._level = 0

    def note_failure(self, why: str) -> None:
        if not _is_rate_limited(why):
            return
        with self._lock:
            if self.slept >= self.max_total_sleep:
                return
            self._streak += 1
            if self._streak < self.streak_limit:
                return
            self._streak = 0
            wait = min(self.cooldown * (2 ** self._level), self.max_cooldown)
            self._level += 1
            self.trips += 1
            now = self._clock()
            until = now + wait
            if self._cool_until is None or until > self._cool_until:
                self._cool_until = until
            print(f'🧯 連續 {self.streak_limit} 檔被 Yahoo 限流，'
                  f'整批先停 {wait:.0f} 秒讓額度回來'
                  f'（第 {self.trips} 次）…')

    def wait_if_cooling(self) -> float:
        with self._lock:
            if self._cool_until is None:
                return 0.0
            if self.slept >= self.max_total_sleep:
                self._cool_until = None
                return 0.0
            remain = min(self._cool_until - self._clock(),
                         self.max_total_sleep - self.slept)
            if remain <= 0:
                self._cool_until = None
                return 0.0
        self._sleep(remain)
        with self._lock:
            self.slept += remain
            if self._cool_until is not None and self._cool_until <= self._clock():
                self._cool_until = None
        return remain



STUB_VOLUME_RATIO = 0.5

SESSION_PROBES = ('2330.TW', '2317.TW', '2412.TW', '2882.TW', '1301.TW')


def _looks_like_stub(df) -> bool:
    if df is None or len(df) < 22:
        return False
    try:
        cur, prev = df.iloc[-1], df.iloc[-2]
        close, open_ = float(cur['Close']), float(cur['Open'])
        vol = float(cur['Volume'])
        pc, po = float(prev['Close']), float(prev['Open'])
        vol20 = float(df['Volume'].iloc[-21:-1].mean())
    except Exception:                                   # noqa: BLE001
        return False
    if pc <= 0 or close != pc or open_ != po:
        return False
    return vol20 > 0 and vol < vol20 * STUB_VOLUME_RATIO


STUB_VOTE_RATIO = 0.5


SESSION_SETTLED_HOUR = 14


def _taipei_now():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz)


def session_unfinished(frames, now=None) -> bool:
    now = now or _taipei_now()
    if now.hour >= SESSION_SETTLED_HOUR:
        return False
    today = now.date()
    for df in frames:
        if df is None or not len(df):
            continue
        last = pd.to_datetime(df.index[-1])
        if last.date() == today:
            return True
    return False


def stub_vote(frames) -> bool:
    votes = [_looks_like_stub(df) for df in frames if df is not None and len(df)]
    if not votes:
        return False
    return sum(votes) > len(votes) * STUB_VOTE_RATIO


VERSION = 'V3.1'


class UniverseIncomplete(RuntimeError):
    pass


@dataclass(frozen=True)
class Rules:

    min_price: float = 10.0
    min_vol20: float = 1000.0
    min_amount: float = 50_000_000.0
    lookback: int = 10
    squeeze: float = 0.12
    vol_ratio: float = 1.2
    atr_stop: float = 3.0
    min_six: float = 0.0
    min_rr: float = 0.0

    def describe(self) -> tuple[tuple[str, str], ...]:
        return (
            ('① 基礎流動性防禦',
             f'股價 > {self.min_price:g} 元 ｜ 20日均量 > {self.min_vol20:,.0f} 股 ｜ '
             f'日均成交金額 > {self.min_amount / 1e4:,.0f} 萬元'),
            ('② 趨勢多頭確認',
             '收盤站穩季線(60MA)之上，且月線(20MA) > 季線(60MA)'),
            ('③ 關鍵發動時機',
             f'過去 {self.lookback} 日內：月季線黃金交叉 或 '
             f'布林頻寬壓縮 ≤ {self.squeeze * 100:g}%'),
            ('④ 強勢突破＋爆量',
             f'收盤突破布林上軌 或 創 20 日新高，且當日量 ≥ 20 日均量 × '
             f'{self.vol_ratio:g}'),
        )

    def changed(self) -> dict[str, tuple[float, float]]:
        base = Rules()
        return {
            f.name: (getattr(base, f.name), getattr(self, f.name))
            for f in fields(self)
            if getattr(base, f.name) != getattr(self, f.name)
        }


DEFAULT_RULES = Rules()

SNAPSHOT_DAYS = 20

SNAPSHOT_COLUMNS: tuple[str, ...] = (
    'code', 'name', 'industry',
    'close', 'vol20', 'amt20',
    'ma20', 'ma60',
    'cross_ago', 'bw',
    'boll_up', 'donchian', 'vol_ratio',
    'trend_ok', 'brk_boll', 'brk_don',
    'atr14',
    'chg', 'chg_pct',
    'six',
    'rr',
    'rr_free',
)

SNAPSHOT_PRECISION: dict[str, int] = {
    'close': 6,
    'vol20': 2,
    'amt20': 0,
    'ma20': 2,
    'ma60': 2,
    'boll_up': 2,
    'donchian': 2,
    'vol_ratio': 6,
    'atr14': 2,
    'six': 4,
    'rr': 6,
}


SNAPSHOT_DIRECTION: dict[str, str] = {
    'close': 'up', 'vol20': 'up', 'amt20': 'up', 'six': 'up', 'rr': 'up',
    'vol_ratio': 'down', 'bw': 'up',
}


def _toward(value: float, digits: int, direction: str) -> float:
    g = round(value, digits) if digits else float(round(value))
    step = 10.0 ** -digits
    if direction == 'up' and g < value:
        g = round(g + step, digits) if digits else g + 1
    elif direction == 'down' and g > value:
        g = round(g - step, digits) if digits else g - 1
    return int(g) if digits == 0 else g


def _snap_round(key: str, value: float) -> float:
    digits = SNAPSHOT_PRECISION[key]
    direction = SNAPSHOT_DIRECTION.get(key)
    if direction:
        return _toward(value, digits, direction)
    return round(value) if digits == 0 else round(value, digits)


def snapshot_row(snap: dict) -> list:
    return [snap[k] for k in SNAPSHOT_COLUMNS]


def passes(snap: dict, rules: Rules) -> tuple[bool, list[str]]:
    if snap['close'] <= rules.min_price:
        return False, []
    if snap['vol20'] <= rules.min_vol20 or snap['amt20'] <= rules.min_amount:
        return False, []
    if not snap['trend_ok']:
        return False, []

    look = min(max(int(rules.lookback), 0), SNAPSHOT_DAYS)
    ago = snap['cross_ago']
    golden = 0 < ago <= look
    window = snap['bw'][-look:] if look else []
    squeeze = any(v <= rules.squeeze for v in window)
    if not (golden or squeeze):
        return False, []

    brk_boll, brk_don = snap['brk_boll'], snap['brk_don']
    if not (brk_boll or brk_don):
        return False, []
    if snap['vol_ratio'] < rules.vol_ratio:
        return False, []

    triggers = []
    if golden:
        triggers.append('黃金交叉')
    if squeeze:
        triggers.append('布林壓縮')
    if brk_boll:
        triggers.append('突破布林上軌')
    if brk_don:
        triggers.append('突破20日高點')
    return True, triggers




def reward_risk(target, downside, close):
    if target is None or downside is None or not close or close <= 0:
        return None, False
    if close <= downside:
        return None, True
    ret = target / close - 1
    if ret <= 0:
        return 0.0, False
    risk = downside / close - 1
    if not risk:
        return None, False
    return abs(ret / risk), False


def cross_passes(snap: dict, min_six: float, min_rr: float) -> bool:
    if min_six > 0:
        six = snap.get('six')
        if six is None or six <= min_six:
            return False
    if min_rr > 0:
        if snap.get('rr_free'):
            return True
        rr = snap.get('rr')
        if rr is None or rr <= min_rr:
            return False
    return True


def load_tw_stock_universe():
    all_rows    = []
    got         = set()
    isin_industry = {}

    for url, suffix in [
        ('https://isin.twse.com.tw/isin/C_public.jsp?strMode=2', '.TW'),
        ('https://isin.twse.com.tw/isin/C_public.jsp?strMode=4', '.TWO'),
    ]:
        try:
            resp = requests.get(url, timeout=20, headers={'User-Agent':'Mozilla/5.0'})
            resp.encoding = 'cp950'
            tables = pd.read_html(StringIO(resp.text), header=0)
            df = tables[0].dropna(how='all').astype(str)

            extracted = df.iloc[:, 0].str.extract(r'^(\d{4})\s+(.+)$')
            extracted.columns = ['code', 'name']
            mask = extracted['code'].notna()
            extracted = extracted[mask].copy()
            extracted['ticker'] = extracted['code'] + suffix
            floor = UNIVERSE_FLOOR.get(suffix, 0)
            if len(extracted) < floor:
                print(f'   ⚠️ {suffix} 只解出 {len(extracted)} 檔（下界 {floor}）'
                      '——當成沒拿到，改走備援。')
            elif len(extracted):
                all_rows.append(extracted)
                got.add(suffix)

            if df.shape[1] > 4:
                ind_col = df.iloc[:, 4]
                for idx in extracted.index:
                    code = extracted.loc[idx, 'code']
                    raw  = str(ind_col.loc[idx]).strip() if idx in ind_col.index else ''
                    if raw and raw not in ('nan', 'NaN', 'None', '', '-', '產業別'):
                        isin_industry[code] = raw

            print(f'✅ ISIN {suffix}：{len(extracted)} 檔  (取得產業別 {sum(1 for k in extracted["code"] if k in isin_industry)} 筆)')
        except Exception as e:
            print(f'⚠️ ISIN {suffix} 失敗：{e}')

    if got == {'.TW', '.TWO'}:
        all_df = pd.concat(all_rows, ignore_index=True).drop_duplicates('ticker')
        return all_df['ticker'].tolist(), all_df.set_index('code')['name'].to_dict(), isin_industry

    missing = {'.TW', '.TWO'} - got
    print(f'🔄 ISIN 缺 {sorted(missing)}，改用 OpenAPI 備援...')
    rows = []
    for url, sfx in [
        ('https://openapi.twse.com.tw/v1/opendata/t187ap03_L', '.TW'),
        ('https://openapi.twse.com.tw/v1/opendata/t187ap03_O', '.TWO'),
    ]:
        if sfx not in missing:
            continue
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            n = 0
            for r in resp.json():
                code = str(r.get('公司代號','')).strip()
                name = str(r.get('公司簡稱','')).strip()
                ind  = str(r.get('產業別', '')).strip()
                if code.isdigit() and len(code) == 4:
                    rows.append({'code': code, 'name': name, 'ticker': code + sfx})
                    n += 1
                    if ind and ind not in ('nan', 'NaN', 'None', ''):
                        isin_industry[code] = ind
            if n:
                got.add(sfx)
            print(f'✅ OpenAPI {sfx}：{n} 檔')
        except Exception as e:
            print(f'⚠️ OpenAPI {sfx} 失敗：{e}')

    if rows:
        all_rows.append(pd.DataFrame(rows))
    if got != {'.TW', '.TWO'}:
        raise UniverseIncomplete(
            f'股票母體不完整：{sorted({".TW", ".TWO"} - got)} 兩個來源都沒拿到。'
            '寧可這一趟不發布，也不要用半個市場覆蓋掉昨天的報告。'
        )
    all_df = pd.concat(all_rows, ignore_index=True).drop_duplicates('ticker')
    print(f'✅ 母體合計：{len(all_df)} 檔（上市＋上櫃都到齊）')
    return all_df['ticker'].tolist(), all_df.set_index('code')['name'].to_dict(), isin_industry



INDUSTRY_CODE_TO_NAME = {
    '01':'水泥工業',   '02':'食品工業',   '03':'塑膠工業',
    '04':'紡織纖維',   '05':'電機機械',   '06':'電器電纜',
    '07':'化學工業',   '08':'生技醫療業', '09':'玻璃陶瓷',
    '10':'造紙工業',   '11':'鋼鐵工業',   '12':'橡膠工業',
    '13':'汽車工業',   '14':'半導體業',   '15':'電腦及週邊設備業',
    '16':'光電業',     '17':'通訊網路業', '18':'電子零組件業',
    '19':'電子通路業', '20':'資訊服務業', '21':'其他電子業',
    '22':'建材營造業', '23':'航運業',     '24':'觀光餐旅業',
    '25':'金融保險業', '26':'貿易百貨業', '27':'油電燃氣業',
    '28':'電信服務業', '29':'綜合',       '30':'其他',
    '31':'存託憑證',   '32':'文化創意業', '33':'農業科技業',
    '34':'電子商務',   '35':'綠能環保',   '36':'數位雲端',
    '37':'運動休閒',   '38':'居家生活',
}

INDUSTRY_MAP = {
    '1101':'水泥工業','1102':'水泥工業','1103':'水泥工業','1104':'水泥工業',
    '1108':'水泥工業','1109':'水泥工業','1110':'水泥工業',
    '1201':'食品工業','1203':'食品工業','1210':'食品工業','1213':'食品工業',
    '1214':'食品工業','1215':'食品工業','1216':'食品工業','1217':'食品工業',
    '1218':'食品工業','1219':'食品工業','1220':'食品工業','1225':'食品工業',
    '1227':'食品工業','1229':'食品工業','1231':'食品工業','1232':'食品工業',
    '1233':'食品工業','1234':'食品工業','1235':'食品工業','1236':'食品工業',
    '1256':'食品工業',
    '1301':'塑膠工業','1303':'塑膠工業','1304':'塑膠工業','1305':'塑膠工業',
    '1307':'塑膠工業','1308':'塑膠工業','1309':'塑膠工業','1310':'塑膠工業',
    '1312':'塑膠工業','1313':'塑膠工業','1314':'塑膠工業','1315':'塑膠工業',
    '1316':'塑膠工業','1317':'塑膠工業','1318':'塑膠工業','1319':'塑膠工業',
    '1321':'塑膠工業','1323':'塑膠工業','1324':'塑膠工業','1325':'塑膠工業',
    '1326':'塑膠工業',
    '1402':'紡織纖維','1403':'紡織纖維','1404':'紡織纖維','1405':'紡織纖維',
    '1406':'紡織纖維','1414':'紡織纖維','1416':'紡織纖維','1417':'紡織纖維',
    '1418':'紡織纖維','1419':'紡織纖維','1423':'紡織纖維','1424':'紡織纖維',
    '1425':'紡織纖維','1426':'紡織纖維','1427':'紡織纖維','1429':'紡織纖維',
    '1431':'紡織纖維','1432':'紡織纖維','1433':'紡織纖維','1434':'紡織纖維',
    '1435':'紡織纖維','1436':'紡織纖維','1437':'紡織纖維','1438':'紡織纖維',
    '1439':'紡織纖維','1440':'紡織纖維','1441':'紡織纖維','1442':'紡織纖維',
    '1443':'紡織纖維','1444':'紡織纖維','1445':'紡織纖維','1446':'紡織纖維',
    '1447':'紡織纖維','1448':'紡織纖維','1449':'紡織纖維','1451':'紡織纖維',
    '1452':'紡織纖維','1453':'紡織纖維','1454':'紡織纖維','1455':'紡織纖維',
    '1456':'紡織纖維','1457':'紡織纖維','1459':'紡織纖維','1460':'紡織纖維',
    '1461':'紡織纖維','1462':'紡織纖維','1463':'紡織纖維','1464':'紡織纖維',
    '1465':'紡織纖維','1466':'紡織纖維','1467':'紡織纖維','1468':'紡織纖維',
    '1469':'紡織纖維','1470':'紡織纖維','1471':'紡織纖維','1472':'紡織纖維',
    '1473':'紡織纖維','1474':'紡織纖維','1475':'紡織纖維','1476':'紡織纖維',
    '1477':'紡織纖維','1478':'紡織纖維',
    '1503':'電機機械','1504':'電機機械','1506':'電機機械','1507':'電機機械',
    '1512':'電機機械','1513':'電機機械','1514':'電機機械','1515':'電機機械',
    '1516':'電機機械','1517':'電機機械','1519':'電機機械','1521':'電機機械',
    '1522':'電機機械','1523':'電機機械','1524':'電機機械','1525':'電機機械',
    '1526':'電機機械','1527':'電機機械','1528':'電機機械','1529':'電機機械',
    '1530':'電機機械','1531':'電機機械','1532':'電機機械','1533':'電機機械',
    '1535':'電機機械','1536':'電機機械','1537':'電機機械','1538':'電機機械',
    '1539':'電機機械','1540':'電機機械','1541':'電機機械','1543':'電機機械',
    '1544':'電機機械','1545':'電機機械','1546':'電機機械','1547':'電機機械',
    '1548':'電機機械','1549':'電機機械','1550':'電機機械','1551':'電機機械',
    '1552':'電機機械','1553':'電機機械','1554':'電機機械','1555':'電機機械',
    '1556':'電機機械','1558':'電機機械','1560':'電機機械','1561':'電機機械',
    '1562':'電機機械','1563':'電機機械','1564':'電機機械','1565':'電機機械',
    '1566':'電機機械','1568':'電機機械','1569':'電機機械','1570':'電機機械',
    '1571':'電機機械','1572':'電機機械','1573':'電機機械','1574':'電機機械',
    '1575':'電機機械','1576':'電機機械','1577':'電機機械','1579':'電機機械',
    '1580':'電機機械','1582':'電機機械','1583':'電機機械','1584':'電機機械',
    '1585':'電機機械','1586':'電機機械','1587':'電機機械','1589':'電機機械',
    '1590':'電機機械','1591':'電機機械','1592':'電機機械','1593':'電機機械',
    '1597':'電機機械','1598':'電機機械','1599':'電機機械',
    '1603':'電器電纜','1604':'電器電纜','1605':'電器電纜','1608':'電器電纜',
    '1609':'電器電纜','1611':'電器電纜','1612':'電器電纜','1613':'電器電纜',
    '1614':'電器電纜','1615':'電器電纜','1616':'電器電纜','1617':'電器電纜',
    '1618':'電器電纜','1619':'電器電纜','1620':'電器電纜',
    '1701':'化學工業','1702':'化學工業','1704':'化學工業','1705':'化學工業',
    '1706':'化學工業','1707':'化學工業','1708':'化學工業','1709':'化學工業',
    '1710':'化學工業','1711':'化學工業','1712':'化學工業','1713':'化學工業',
    '1714':'化學工業','1715':'化學工業','1717':'化學工業','1718':'化學工業',
    '1719':'化學工業','1720':'化學工業','1721':'化學工業','1722':'化學工業',
    '1723':'化學工業','1724':'化學工業','1725':'化學工業','1726':'化學工業',
    '1727':'化學工業','1730':'化學工業','1731':'化學工業','1732':'化學工業',
    '1733':'化學工業','1734':'化學工業','1735':'化學工業','1736':'化學工業',
    '1737':'化學工業','1738':'化學工業','1741':'化學工業','1742':'化學工業',
    '1752':'化學工業','1753':'化學工業','1754':'化學工業','1755':'化學工業',
    '1760':'化學工業','1762':'化學工業','1773':'化學工業',
    '1786':'生技醫療業','1788':'生技醫療業','1789':'生技醫療業',
    '1795':'生技醫療業','1796':'生技醫療業','1802':'玻璃陶瓷',
    '1805':'生技醫療業','1806':'玻璃陶瓷','1807':'玻璃陶瓷',
    '1808':'玻璃陶瓷','1809':'玻璃陶瓷',
    '1901':'造紙工業','1903':'造紙工業','1904':'造紙工業','1905':'造紙工業',
    '1906':'造紙工業','1907':'造紙工業',
    '2002':'鋼鐵工業','2006':'鋼鐵工業','2007':'鋼鐵工業','2008':'鋼鐵工業',
    '2009':'鋼鐵工業','2010':'鋼鐵工業','2011':'鋼鐵工業','2012':'鋼鐵工業',
    '2013':'鋼鐵工業','2014':'鋼鐵工業','2015':'鋼鐵工業','2016':'鋼鐵工業',
    '2017':'鋼鐵工業','2018':'鋼鐵工業','2019':'鋼鐵工業','2020':'鋼鐵工業',
    '2022':'鋼鐵工業','2023':'鋼鐵工業','2024':'鋼鐵工業','2025':'鋼鐵工業',
    '2026':'鋼鐵工業','2027':'鋼鐵工業','2028':'鋼鐵工業','2029':'鋼鐵工業',
    '2030':'鋼鐵工業','2031':'鋼鐵工業','2032':'鋼鐵工業','2033':'鋼鐵工業',
    '2034':'鋼鐵工業','2035':'鋼鐵工業','2036':'鋼鐵工業','2038':'鋼鐵工業',
    '2039':'鋼鐵工業','2040':'鋼鐵工業','2041':'鋼鐵工業','2042':'鋼鐵工業',
    '2043':'鋼鐵工業','2044':'鋼鐵工業','2045':'鋼鐵工業','2046':'鋼鐵工業',
    '2047':'鋼鐵工業','2048':'鋼鐵工業','2049':'鋼鐵工業',
    '2062':'鋼鐵工業','2064':'鋼鐵工業','2069':'鋼鐵工業',
    '2072':'鋼鐵工業','2073':'鋼鐵工業','2076':'鋼鐵工業',
    '2083':'鋼鐵工業','2084':'鋼鐵工業','2092':'鋼鐵工業',
    '2093':'鋼鐵工業','2095':'鋼鐵工業','2096':'鋼鐵工業',
    '2304':'光電業','2305':'電腦及週邊設備業','2306':'電腦及週邊設備業',
    '2307':'電腦及週邊設備業','2309':'鋼鐵工業',
    '2059':'其他電子業',
    '2101':'橡膠工業','2102':'橡膠工業','2103':'橡膠工業','2104':'橡膠工業',
    '2105':'橡膠工業','2106':'橡膠工業','2107':'橡膠工業','2108':'橡膠工業',
    '2109':'橡膠工業','2110':'橡膠工業',
    '2201':'汽車工業','2202':'汽車工業','2203':'汽車工業','2204':'汽車工業',
    '2205':'汽車工業','2206':'汽車工業','2207':'汽車工業','2208':'汽車工業',
    '2209':'汽車工業','2210':'汽車工業','2211':'汽車工業','2212':'汽車工業',
    '2213':'汽車工業','2214':'汽車工業','2215':'汽車工業','2216':'汽車工業',
    '2217':'汽車工業','2219':'汽車工業','2220':'汽車工業','2221':'汽車工業',
    '2222':'汽車工業','2223':'汽車工業','2224':'汽車工業','2225':'汽車工業',
    '2226':'汽車工業','2227':'汽車工業','2228':'汽車工業','2229':'汽車工業',
    '2230':'汽車工業','2231':'汽車工業','2232':'汽車工業','2233':'汽車工業',
    '2234':'汽車工業','2235':'汽車工業','2236':'汽車工業','2237':'汽車工業',
    '2239':'汽車工業','2240':'汽車工業','2241':'汽車工業','2243':'汽車工業',
    '2244':'汽車工業','2245':'汽車工業',
    '2301':'電腦及週邊設備業','2303':'半導體業',
    '2308':'光電業',
    '2317':'電腦及週邊設備業',
    '2327':'電子零組件業','2330':'半導體業','2337':'半導體業','2344':'半導體業',
    '2345':'通訊網路業','2347':'半導體業','2348':'電子通路業','2351':'半導體業',
    '2353':'電腦及週邊設備業','2356':'電子通路業','2357':'電腦及週邊設備業',
    '2360':'電子零組件業','2362':'資訊服務業','2365':'電子通路業',
    '2376':'電腦及週邊設備業','2377':'電腦及週邊設備業',
    '2379':'通訊網路業','2382':'電腦及週邊設備業','2383':'電子零組件業',
    '2385':'電腦及週邊設備業','2388':'半導體業','2392':'電腦及週邊設備業',
    '2395':'電腦及週邊設備業',
    '2408':'半導體業','2409':'光電業','2412':'電信服務業','2439':'電子零組件業',
    '2449':'半導體業','2454':'半導體業','2474':'光電業',
    '2501':'建材營造業','2502':'建材營造業','2503':'建材營造業','2504':'建材營造業',
    '2505':'建材營造業','2506':'建材營造業','2507':'建材營造業','2508':'建材營造業',
    '2509':'建材營造業','2511':'建材營造業','2514':'建材營造業','2515':'建材營造業',
    '2516':'建材營造業','2517':'建材營造業','2518':'建材營造業','2520':'建材營造業',
    '2521':'建材營造業','2524':'建材營造業','2527':'建材營造業','2528':'建材營造業',
    '2530':'建材營造業','2534':'建材營造業','2535':'建材營造業','2536':'建材營造業',
    '2537':'建材營造業','2538':'建材營造業','2539':'建材營造業','2540':'建材營造業',
    '2542':'建材營造業','2543':'建材營造業','2545':'建材營造業','2546':'建材營造業',
    '2547':'建材營造業','2548':'建材營造業',
    '2601':'航運業','2602':'航運業','2603':'航運業','2605':'航運業',
    '2606':'航運業','2607':'航運業','2608':'航運業','2609':'航運業',
    '2610':'航運業','2611':'航運業','2612':'航運業','2613':'航運業',
    '2614':'航運業','2615':'航運業','2616':'航運業','2617':'航運業',
    '2618':'航運業','2619':'航運業','2620':'航運業',
    '2701':'觀光餐旅業','2702':'觀光餐旅業','2703':'觀光餐旅業','2704':'觀光餐旅業',
    '2705':'觀光餐旅業','2706':'觀光餐旅業','2707':'觀光餐旅業','2708':'觀光餐旅業',
    '2709':'觀光餐旅業','2711':'觀光餐旅業','2712':'觀光餐旅業','2713':'觀光餐旅業',
    '2714':'觀光餐旅業','2715':'觀光餐旅業','2716':'觀光餐旅業','2717':'觀光餐旅業',
    '2718':'觀光餐旅業','2719':'觀光餐旅業','2720':'觀光餐旅業','2721':'觀光餐旅業',
    '2722':'觀光餐旅業','2723':'觀光餐旅業','2724':'觀光餐旅業','2726':'觀光餐旅業',
    '2727':'觀光餐旅業','2728':'觀光餐旅業',
    '2801':'金融保險業','2809':'金融保險業','2812':'金融保險業','2820':'金融保險業',
    '2822':'金融保險業','2823':'金融保險業','2824':'金融保險業','2825':'金融保險業',
    '2826':'金融保險業','2832':'金融保險業','2834':'金融保險業','2836':'金融保險業',
    '2838':'金融保險業','2841':'金融保險業','2845':'金融保險業','2847':'金融保險業',
    '2849':'金融保險業','2850':'金融保險業','2851':'金融保險業','2852':'金融保險業',
    '2855':'金融保險業','2856':'金融保險業','2857':'金融保險業','2860':'金融保險業',
    '2861':'金融保險業','2867':'金融保險業','2868':'金融保險業','2869':'金融保險業',
    '2870':'金融保險業','2871':'金融保險業','2872':'金融保險業','2873':'金融保險業',
    '2874':'金融保險業','2876':'金融保險業','2877':'金融保險業','2878':'金融保險業',
    '2879':'金融保險業','2880':'金融保險業','2881':'金融保險業','2882':'金融保險業',
    '2883':'金融保險業','2884':'金融保險業','2885':'金融保險業','2886':'金融保險業',
    '2887':'金融保險業','2888':'金融保險業','2889':'金融保險業','2890':'金融保險業',
    '2891':'金融保險業','2892':'金融保險業','2893':'金融保險業','2897':'金融保險業',
    '2898':'金融保險業',
    '2901':'貿易百貨業','2903':'貿易百貨業','2904':'貿易百貨業','2906':'貿易百貨業',
    '2908':'貿易百貨業','2910':'貿易百貨業','2911':'貿易百貨業','2912':'貿易百貨業',
    '2913':'貿易百貨業','2915':'貿易百貨業','2916':'貿易百貨業','2917':'貿易百貨業',
    '2918':'貿易百貨業','2919':'貿易百貨業','2920':'貿易百貨業',
    '3006':'半導體業','3034':'半導體業','3037':'半導體業',
    '3045':'電信服務業','3094':'半導體業','3130':'資訊服務業',
    '3231':'電腦及週邊設備業','3260':'電子零組件業','3293':'資訊服務業',
    '3443':'半導體業','3481':'光電業','3529':'半導體業',
    '3552':'電子零組件業','3661':'半導體業','3702':'電子通路業','3711':'電子零組件業',
    '4105':'生技醫療業','4106':'生技醫療業','4107':'生技醫療業','4108':'生技醫療業',
    '4109':'生技醫療業','4110':'生技醫療業','4111':'生技醫療業','4112':'生技醫療業',
    '4113':'生技醫療業','4114':'生技醫療業','4115':'生技醫療業','4116':'生技醫療業',
    '4117':'生技醫療業','4119':'生技醫療業','4121':'生技醫療業','4122':'生技醫療業',
    '4123':'生技醫療業','4124':'生技醫療業','4125':'生技醫療業','4126':'生技醫療業',
    '4127':'生技醫療業','4128':'生技醫療業','4129':'生技醫療業','4130':'生技醫療業',
    '4131':'生技醫療業','4132':'生技醫療業','4133':'生技醫療業','4134':'生技醫療業',
    '4135':'生技醫療業','4136':'生技醫療業','4137':'生技醫療業','4138':'生技醫療業',
    '4139':'生技醫療業','4140':'生技醫療業','4141':'生技醫療業','4142':'生技醫療業',
    '4144':'生技醫療業','4148':'生技醫療業','4152':'生技醫療業','4154':'生技醫療業',
    '4155':'生技醫療業','4157':'生技醫療業','4158':'生技醫療業','4159':'生技醫療業',
    '4160':'生技醫療業','4161':'生技醫療業','4162':'生技醫療業','4163':'生技醫療業',
    '4164':'生技醫療業','4165':'生技醫療業','4166':'生技醫療業','4167':'生技醫療業',
    '4168':'生技醫療業','4169':'生技醫療業','4170':'生技醫療業','4171':'生技醫療業',
    '4172':'生技醫療業','4173':'生技醫療業','4174':'生技醫療業','4175':'生技醫療業',
    '4176':'生技醫療業','4177':'生技醫療業','4179':'生技醫療業','4180':'生技醫療業',
    '4183':'生技醫療業','4188':'生技醫療業','4192':'生技醫療業','4194':'生技醫療業',
    '4198':'生技醫療業','4200':'生技醫療業','4205':'生技醫療業','4206':'生技醫療業',
    '4207':'生技醫療業','4209':'生技醫療業','4210':'生技醫療業','4211':'生技醫療業',
    '4215':'生技醫療業','4217':'生技醫療業','4218':'生技醫療業','4219':'生技醫療業',
    '4220':'生技醫療業','4221':'生技醫療業','4222':'生技醫療業','4223':'生技醫療業',
    '4224':'生技醫療業','4225':'生技醫療業','4226':'生技醫療業','4227':'生技醫療業',
    '4228':'生技醫療業','4229':'生技醫療業','4230':'生技醫療業','4232':'生技醫療業',
    '4904':'電信服務業','4919':'半導體業','4938':'電腦及週邊設備業',
    '4958':'通訊網路業','4966':'半導體業',
    '5274':'半導體業','5425':'半導體業','5483':'半導體業',
    '5522':'建材營造業','5534':'建材營造業',
    '5820':'金融保險業','5821':'金融保險業','5823':'金融保險業','5824':'金融保險業',
    '5825':'金融保險業','5826':'金融保險業','5827':'金融保險業','5830':'金融保險業',
    '5832':'金融保險業','5834':'金融保險業','5835':'金融保險業','5836':'金融保險業',
    '5838':'金融保險業','5843':'金融保險業','5844':'金融保險業','5845':'金融保險業',
    '5846':'金融保險業','5847':'金融保險業','5850':'金融保險業','5853':'金融保險業',
    '5854':'金融保險業','5855':'金融保險業','5856':'金融保險業','5857':'金融保險業',
    '5859':'金融保險業','5860':'金融保險業','5861':'金融保險業','5863':'金融保險業',
    '5864':'金融保險業','5865':'金融保險業','5866':'金融保險業','5867':'金融保險業',
    '5868':'金融保險業','5869':'金融保險業','5871':'金融保險業',
    '6176':'光電業','6197':'電子零組件業','6274':'電子零組件業',
    '6278':'電子零組件業','6414':'通訊網路業','6415':'半導體業',
    '6488':'半導體業','6505':'油電燃氣業','6669':'電腦及週邊設備業',
    '6770':'半導體業',
    '8046':'半導體業','8103':'資訊服務業',
    '9910':'橡膠工業','9945':'建材營造業','9951':'油電燃氣業',
    '9955':'油電燃氣業','9957':'油電燃氣業','9958':'油電燃氣業',
}

def get_industry_from_api():
    VALID_NAMES = set(INDUSTRY_CODE_TO_NAME.values())

    industry = {}
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Referer': 'https://www.twse.com.tw/',
    }

    for label, url in [
        ('上市', 'https://openapi.twse.com.tw/v1/opendata/t187ap03_L'),
        ('上櫃', 'https://openapi.twse.com.tw/v1/opendata/t187ap03_O'),
    ]:
        try:
            resp = requests.get(url, timeout=15, headers=hdrs)
            if resp.status_code != 200:
                print(f'  [{label}] API HTTP {resp.status_code}，跳過')
                continue

            data = resp.json()
            if not data:
                continue

            sample = data[0]
            print(f'  [{label}] API 欄位: {list(sample.keys())}')
            print(f'  [{label}] 第一筆範例: {sample}')

            code_col = next(
                (f for f in ['公司代號', '股票代號', 'Code', 'StockCode']
                 if f in sample), None
            )
            ind_col = next(
                (f for f in ['產業別', '產業類別', '產業別名稱',
                             '產業類別代號', 'IndustryName', 'industry']
                 if f in sample), None
            )

            if not code_col or not ind_col:
                print(f'  [{label}] 找不到必要欄位 (code={code_col}, ind={ind_col})，跳過')
                continue

            ok = 0
            for item in data:
                code    = str(item.get(code_col, '')).strip()
                raw_ind = str(item.get(ind_col, '')).strip()

                if not (code.isdigit() and len(code) == 4):
                    continue
                if not raw_ind or raw_ind in ('nan', 'None', '', '-'):
                    continue

                if raw_ind.isdigit():
                    mapped = INDUSTRY_CODE_TO_NAME.get(raw_ind.zfill(2), '')
                    if not mapped:
                        continue
                    raw_ind = mapped

                if raw_ind not in VALID_NAMES:
                    continue

                if code not in INDUSTRY_MAP:
                    industry[code] = raw_ind
                    ok += 1

            print(f'  [{label}] 補充了 {ok} 筆靜態表未涵蓋的股票產業別')

        except Exception as e:
            print(f'  [{label}] API 例外：{e}')

    total = len(industry)
    if total:
        print(f'✅ API 額外補充：{total} 筆（靜態表以外的股票）')
    else:
        print('ℹ️  API 未補充新筆數（靜態表已全涵蓋，或 API 不可用）')
    return industry


def lookup_industry(code, isin_industry, static_map=None):
    if code in isin_industry:
        val = isin_industry[code]
        if val and val not in ('nan', 'NaN', 'None', '', '-'):
            return val
    sm = static_map if static_map is not None else INDUSTRY_MAP
    if code in sm:
        return sm[code]
    return '其他'



def compute_atr(df, period=14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([(h-l),(h-c.shift()).abs(),(l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_bollinger(close, period=20, k=2):
    ma  = close.rolling(period).mean()
    std = close.rolling(period).std()
    up  = ma + k*std
    dn  = ma - k*std
    bw  = (up - dn) / ma
    return ma, up, dn, bw


UNIVERSE_FLOOR = {'.TW': 800, '.TWO': 600}

CROSS_MAX_AGE_DAYS = 5

CROSS_URL = 'https://metallicatw.github.io/tw-six-metrics/cross.json'


def indicator_snapshot(df):
    close, volume = df['Close'], df['Volume']
    price = float(close.iloc[-1])
    vol20 = float(volume.rolling(20).mean().iloc[-1])
    amt20 = float((close*volume).rolling(20).mean().iloc[-1])
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    boll_ma, boll_up, boll_dn, bw = compute_bollinger(close)
    donchian = close.rolling(20).max().shift(1)
    atr_val = float(compute_atr(df).iloc[-1])
    vol_ratio = float(volume.iloc[-1]) / vol20 if vol20 else 0.0

    cross_ago = -1
    for i in range(1, min(SNAPSHOT_DAYS, len(ma20) - 1) + 1):
        if (float(ma20.iloc[-i]) > float(ma60.iloc[-i])
                and float(ma20.iloc[-i - 1]) <= float(ma60.iloc[-i - 1])):
            cross_ago = i
            break

    don_last = (None if pd.isna(donchian.iloc[-1])
                else round(float(donchian.iloc[-1]), 2))
    prev = float(close.iloc[-2]) if len(close) > 1 else price
    chg = round(price - prev, 2)
    chg_pct = round(chg / prev * 100, 2) if prev else 0.0
    ma20_last = float(ma20.iloc[-1])
    ma60_last = float(ma60.iloc[-1])
    boll_up_last = float(boll_up.iloc[-1])
    don_raw = None if pd.isna(donchian.iloc[-1]) else float(donchian.iloc[-1])
    trend_ok = price > ma60_last and ma20_last > ma60_last
    brk_boll = price > boll_up_last
    brk_don = don_raw is not None and price > don_raw


    numbers = {
        'close': _snap_round('close', price),
        'vol20': _snap_round('vol20', vol20),
        'amt20': _snap_round('amt20', amt20),
        'ma20': _snap_round('ma20', ma20_last),
        'ma60': _snap_round('ma60', ma60_last),
        'cross_ago': cross_ago,
        'bw': [_toward(float(v), 6, SNAPSHOT_DIRECTION['bw'])
               if not pd.isna(v) else 9.0
               for v in bw.iloc[-SNAPSHOT_DAYS:]],
        'boll_up': _snap_round('boll_up', boll_up_last),
        'donchian': don_last,
        'vol_ratio': _snap_round('vol_ratio', vol_ratio),
        'trend_ok': trend_ok,
        'brk_boll': brk_boll,
        'brk_don': brk_don,
        'atr14': _snap_round('atr14', atr_val),
        'chg': chg,
        'chg_pct': chg_pct,
    }
    ind = {
        'price': price, 'vol20': vol20, 'amt20': amt20, 'ma20': ma20, 'ma60': ma60,
        'boll_ma': boll_ma, 'boll_up': boll_up, 'boll_dn': boll_dn, 'bw': bw,
        'atr_val': atr_val, 'vol_ratio': vol_ratio,
    }
    return numbers, ind


def load_cross_feed(url: str = '') -> dict:
    import json
    import urllib.request

    if url == '-':
        return {}
    target = url or CROSS_URL
    try:
        with urllib.request.urlopen(target, timeout=30) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except Exception as exc:                      # noqa: BLE001
        print(f'⚠️ 六大與估值抓不到（{exc}）——〔趨勢∩六大∩報酬〕今天沒有資料可比，'
              f'四部曲不受影響')
        return {}
    rows = payload.get('rows') or {}
    as_of = str(payload.get('as_of') or '')
    print(f'✅ 六大與估值：{len(rows)} 檔　財報 {payload.get("quarter", "?")}'
          f'　估值基準 {as_of or "?"}')

    if as_of:
        try:
            age = (_taipei_now().date()
                   - datetime.date.fromisoformat(as_of[:10])).days
        except ValueError:
            age = None
        if age is not None and age > CROSS_MAX_AGE_DAYS:
            print(f'::warning::六大與估值那一份是 {as_of} 的，已經 {age} 天沒更新'
                  f'（上限 {CROSS_MAX_AGE_DAYS} 天）——這一趟不拿它算報酬風險比。')
            return {}
    return payload


OFFICIAL_DAILY_URL = ('https://raw.githubusercontent.com/metallicatw/tw-six-metrics/'
                      'main/data/market/daily/prices/{date}.csv.gz')


def market_gaps(frames) -> list:
    days = set()
    for d in frames:
        days.update(pd.to_datetime(d.index).tz_localize(None).normalize())
    if len(days) < 2:
        return []
    lo, hi = min(days), max(days)
    return [t for t in pd.bdate_range(lo, hi) if t not in days]


def load_official_day(day, url: str = '') -> dict:
    import csv
    import gzip
    import io
    import urllib.request

    target = (url or OFFICIAL_DAILY_URL).format(date=f'{day:%Y-%m-%d}')
    try:
        with urllib.request.urlopen(target, timeout=20) as resp:
            raw = resp.read()
    except Exception:                               # noqa: BLE001 - 404 就是假日
        return {}
    try:
        text = gzip.decompress(raw).decode('utf-8')
    except OSError:
        text = raw.decode('utf-8', 'replace')
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        try:
            o, h, lo, c = (float(r[k]) for k in ('open', 'high', 'low', 'close'))
            v = float(r['volume'])
            chg = float(r['change'] or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if c > 0 and o > 0:
            out[str(r['code']).strip()] = (o, h, lo, c, v, c - chg)
    return out


def fill_gap_rows(df, code: str, gaps: dict):
    if not gaps or df is None or not len(df):
        return df
    add = []
    for day, rows in gaps.items():
        row = rows.get(code)
        if row is None or day in df.index or day < df.index[0] or day > df.index[-1]:
            continue
        o, h, lo, c, v, prev_raw = row
        before = df.loc[df.index < day, 'Close']
        k = 1.0
        if len(before) and prev_raw > 0:
            k = float(before.iloc[-1]) / prev_raw
            if not (0.2 < k < 5):
                k = 1.0
        add.append(pd.DataFrame({'Open': [o * k], 'High': [h * k], 'Low': [lo * k],
                                 'Close': [c * k], 'Volume': [v]}, index=[day]))
    if not add:
        return df
    out = pd.concat([df] + add)
    return out[~out.index.duplicated(keep='first')].sort_index()


def run(
    output_dir: str,
    *,
    limit: int = 0,
    workers: int = DEFAULT_WORKERS,
    retry_rounds: tuple = RETRY_ROUNDS,
    period: str = '2y',
    link_base: str = '',
    plotly_cdn: bool = True,
    chart_years: float = 2.0,
    index_copy: str = '',
    open_when_done: bool = False,
    rules: Rules = DEFAULT_RULES,
    data_dir: str = '',
    data_base: str = '',
    cross_url: str = '',
    official_url: str = '',
    breaker: 'RateLimitBreaker | None' = None,
    session_probes: tuple | None = None,
) -> dict:
    print('=' * 60)
    print(f'  台股順勢交易篩選系統 {VERSION}')
    print('=' * 60)
    print('[1/3] 載入上市＋上櫃股票清單（含產業別）...')
    TICKERS, NAME_MAP, ISIN_INDUSTRY = load_tw_stock_universe()
    if limit:
        TICKERS = TICKERS[:limit]
        print(f'   （--limit {limit}：只掃前 {len(TICKERS)} 檔）')
    print(f'✅ 股票池：{len(TICKERS)} 檔  |  ISIN 產業別：{len(ISIN_INDUSTRY)} 筆')
    CROSS = load_cross_feed(cross_url)
    print()

    import collections as _collections
    import threading as _threading
    SCREEN_ERRORS = _collections.Counter()
    SCREEN_ERROR_SAMPLES: list[str] = []
    _err_lock = _threading.Lock()
    _scanned = [0]
    _too_short = [0]
    _scan_lock = _threading.Lock()
    _failed: dict[str, str] = {}
    _breaker = breaker if breaker is not None else RateLimitBreaker()
    _asof = [None]
    _gaps: dict = {}
    SNAPSHOTS: list[dict] = []
    _snap_lock = _threading.Lock()
    CHARTS: dict[str, dict] = {}
    _chart_lock = _threading.Lock()

    def _download(ticker):
        last = ''
        for kw in ({'session': _YF_SESSION}, {}):
            _breaker.wait_if_cooling()
            try:
                df = yf.download(ticker, period=period, interval='1d',
                                 auto_adjust=True, progress=False, threads=False, **kw)
            except Exception as e:                      # noqa: BLE001
                last = f'{type(e).__name__}: {e}'
                df = None
            if df is not None and len(df):
                _breaker.note_ok()
                return df, ''
        why = last or NO_DATA
        _breaker.note_failure(why)
        return df, why

    def screen_stock(ticker):
        try:
            df, why = _download(ticker)
            if df is None or len(df) == 0:
                with _err_lock:
                    _failed[ticker] = why
                return None
            if len(df) < 65:
                with _scan_lock:
                    _too_short[0] += 1
                return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            df = df.dropna(subset=['Close','Volume'])
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df = fill_gap_rows(df, ticker.split('.')[0], _gaps)
            if _asof[0] is not None:
                df = df[df.index <= _asof[0]]
                if len(df) < 65:
                    with _scan_lock:
                        _too_short[0] += 1
                    return None
            volume = df['Volume']

            code = ticker.split('.')[0]
            numbers, ind = indicator_snapshot(df)
            price, vol20, amt20 = ind['price'], ind['vol20'], ind['amt20']
            ma20, ma60 = ind['ma20'], ind['ma60']
            boll_ma, boll_up, boll_dn, bw = (ind['boll_ma'], ind['boll_up'],
                                             ind['boll_dn'], ind['bw'])
            atr_val, vol_ratio = ind['atr_val'], ind['vol_ratio']
            snap = {
                'code': code, 'name': NAME_MAP.get(code, code),
                'industry': lookup_industry(code, ISIN_INDUSTRY),
                **numbers,
            }
            six, target_price, floor_price = (CROSS.get('rows') or {}).get(
                code, (None, None, None))
            rr_value, rr_free = reward_risk(target_price, floor_price, price)
            snap['six'] = None if six is None else _snap_round('six', float(six))
            snap['rr'] = None if rr_value is None else _snap_round('rr', rr_value)
            snap['rr_free'] = rr_free
            with _snap_lock:
                SNAPSHOTS.append(snap)
            with _scan_lock:
                _scanned[0] += 1

            with _chart_lock:
                CHARTS[code] = {
                    'ticker': ticker, 'code': code,
                    'name': NAME_MAP.get(code, code),
                    'industry': lookup_industry(code, ISIN_INDUSTRY),
                    'close': round(price, 2),
                    'ma20_last': round(float(ma20.iloc[-1]), 2),
                    'ma60_last': round(float(ma60.iloc[-1]), 2),
                    'boll_up_last': round(float(boll_up.iloc[-1]), 2),
                    'boll_mid_last': round(float(boll_ma.iloc[-1]), 2),
                    'boll_dn_last': round(float(boll_dn.iloc[-1]), 2),
                    'boll_bw_pct': round(float(bw.iloc[-1]) * 100, 2),
                    'vol_today': int(volume.iloc[-1]),
                    'vol20_avg': round(vol20, 0),
                    'vol_ratio': round(vol_ratio, 2),
                    'amt_M': round(amt20 / 1e6, 1),
                    'atr14': round(atr_val, 2),
                    'stop_loss': round(price - DEFAULT_RULES.atr_stop * atr_val, 2),
                    'trigger': '',
                    '_df': df, '_ma20': ma20, '_ma60': ma60,
                    '_boll_up': boll_up, '_boll_mid': boll_ma, '_boll_dn': boll_dn,
                }

            ok, trigger_parts = passes(snap, rules)
            if not ok:
                return None

            golden_cross = '黃金交叉' in trigger_parts
            squeeze = '布林壓縮' in trigger_parts
            stop_loss = round(price - rules.atr_stop * atr_val, 2)

            return {
                'ticker': ticker, 'code': code, 'name': NAME_MAP.get(code, code),
                'industry': lookup_industry(code, ISIN_INDUSTRY),
                'close':         round(price, 2),
                'ma20_last':     round(float(ma20.iloc[-1]), 2),
                'ma60_last':     round(float(ma60.iloc[-1]), 2),
                'boll_up_last':  round(float(boll_up.iloc[-1]), 2),
                'boll_mid_last': round(float(boll_ma.iloc[-1]), 2),
                'boll_dn_last':  round(float(boll_dn.iloc[-1]), 2),
                'boll_bw_pct':   round(float(bw.iloc[-1])*100, 2),
                'vol_today':     int(volume.iloc[-1]),
                'vol20_avg':     round(vol20, 0),
                'vol_ratio':     round(vol_ratio, 2),
                'amt_M':         round(amt20/1e6, 1),
                'atr14':         round(atr_val, 2),
                'stop_loss':     stop_loss,
                'golden_cross':  golden_cross,
                'squeeze':       squeeze,
                'trigger':       ' ｜ '.join(trigger_parts),
                '_df':           df.copy(),
                '_ma20':         ma20.copy(),
                '_ma60':         ma60.copy(),
                '_boll_up':      boll_up.copy(),
                '_boll_mid':     boll_ma.copy(),
                '_boll_dn':      boll_dn.copy(),
            }
        except Exception as e:
            with _err_lock:
                SCREEN_ERRORS[type(e).__name__] += 1
                if len(SCREEN_ERROR_SAMPLES) < 5:
                    SCREEN_ERROR_SAMPLES.append(f'{ticker}: {type(e).__name__}: {e}')
            return None

    def _settle_failures():
        for tk, why in sorted(_failed.items()):
            kind = 'RateLimited' if _is_rate_limited(why) else 'DownloadFailed'
            SCREEN_ERRORS[kind] += 1
            if len(SCREEN_ERROR_SAMPLES) < 5:
                SCREEN_ERROR_SAMPLES.append(f'{tk}: {why}')

    TW_TZ     = datetime.timezone(datetime.timedelta(hours=8))
    now       = datetime.datetime.now(TW_TZ)
    today_str = now.strftime('%Y-%m-%d')

    print('='*60)
    print(f'📊 [2/3] 台股順勢交易系統 {VERSION} ── 嚴格四部曲篩選')
    print('='*60)
    print(f'掃描股票池：{len(TICKERS)} 檔  ｜  日期：{today_str}  ｜  歷史長度：{period}')
    changed = rules.changed()
    if changed:
        bits = ', '.join(f'{k} {a:g}→{b:g}' for k, (a, b) in changed.items())
        print(f'⚠️ 門檻：非預設（{bits}）')
    else:
        print('門檻：預設')
    print('-'*60)

    from concurrent.futures import ThreadPoolExecutor
    import threading

    probes = []
    for tk in (SESSION_PROBES if session_probes is None else session_probes):
        if tk not in TICKERS and TICKERS:
            continue
        d, _why = _download(tk)
        if d is not None and len(d):
            if isinstance(d.columns, pd.MultiIndex):
                d = d.copy()
                d.columns = d.columns.droplevel(1)
            probes.append(d)
    reason = ''
    if probes and stub_vote(probes):
        reason = ('權值股的最後一根是「還沒開盤」的佔位棒'
                  '（開盤收盤都和前一根一模一樣、量不到均量的一半）'
                  '——今天那一場交易還沒發生')
    elif probes and session_unfinished(probes):
        reason = ('現在還沒收盤（台股 13:30，緩衝到 14:00）'
                  '——最後一根是**半場**的量，拿去比全天的均量，第四關會全滅')
    if reason:
        prevs = []
        for d in probes:
            idx = pd.to_datetime(d.index).tz_localize(None)
            if len(idx) >= 2:
                prevs.append(idx[-2])
        if prevs:
            _asof[0] = max(prevs)
            print(f'⚠️ {reason}。')
            print(f'   整批算到 {_asof[0]:%Y-%m-%d} 為止。')
    elif probes:
        idx = pd.to_datetime(probes[0].index).tz_localize(None)
        print(f'📅 資料基準：{idx[-1]:%Y-%m-%d}（權值股的最後一根是真的收過盤的）')

    if official_url != '-' and probes:
        for day in market_gaps(probes):
            if _asof[0] is not None and day > _asof[0]:
                continue
            rows = load_official_day(day, official_url)
            if rows:
                _gaps[day] = rows
        if _gaps:
            print('🩹 Yahoo 整個市場少了這幾天，改用官方逐日行情補上：'
                  + '、'.join(f'{d:%Y-%m-%d}（{len(r)} 檔）' for d, r in sorted(_gaps.items())))

    _lock    = threading.Lock()
    RESULTS  = []
    _done = [0]

    def _screen_and_collect(args):
        i, ticker = args
        r = screen_stock(ticker)
        with _lock:
            _done[0] += 1
            if r:
                RESULTS.append(r)
                print(f"✅ {r['code']:>4s} {r['name']:<8s} "
                      f"收:{r['close']:>7.2f} 量比:{r['vol_ratio']:.2f}x │ {r['trigger']}")
            if _done[0] % 100 == 0:
                print(f'── 已跑完 {_done[0]}/{len(TICKERS)} 檔'
                      f'（真的掃到 {_scanned[0]} 檔）──')

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(_screen_and_collect, enumerate(TICKERS)))

    def _retry_one(ticker):
        with _err_lock:
            _failed.pop(ticker, None)
        r = screen_stock(ticker)
        with _lock:
            if r:
                RESULTS.append(r)
                print(f"✅ {r['code']:>4s} {r['name']:<8s} "
                      f"收:{r['close']:>7.2f} 量比:{r['vol_ratio']:.2f}x │ {r['trigger']}"
                      f"  ←補問")

    for wait, rw in (retry_rounds or ()):
        todo = sorted(tk for tk, why in _failed.items() if _worth_retry(why))
        empty = sorted(tk for tk, why in _failed.items() if not _worth_retry(why))
        mass = _mass_empty(len(empty), len(TICKERS))
        if mass:
            todo = sorted(todo + empty)
        if not todo:
            break
        limited = sum(1 for tk in todo if _is_rate_limited(_failed[tk]))
        print(f'⏳ 有 {len(todo)} 檔沒問到（其中 {limited} 檔明確是被限流'
              + (f'，{len(empty)} 檔回空表——數量異常，一併補問' if mass else '')
              + f'）。等 {wait} 秒，再用 {rw} 條連線補問一次…')
        _time.sleep(wait)
        before = len(_failed)
        with ThreadPoolExecutor(max_workers=rw) as executor:
            list(executor.map(_retry_one, todo))
        print(f'   補回 {before - len(_failed)} 檔，還差 {len(_failed)} 檔')

    _settle_failures()

    RESULTS.sort(key=lambda x: (-x['vol_ratio'], x['code']))

    errors = sum(SCREEN_ERRORS.values())
    print('='*60)
    print(f'🎯 篩選完成！今日共 {len(RESULTS)} 檔標的通過'
          f'（真的掃到 {_scanned[0]}/{len(TICKERS)} 檔）')
    if _too_short[0]:
        print(f'   另有 {_too_short[0]} 檔歷史不足 65 根（新上市），算不了 60MA。'
              '這是正常的，不計入失敗。')
    if errors:
        print(f'⚠️ 有 {errors} 檔沒有掃成（不等於沒過篩）：')
        for name, n in SCREEN_ERRORS.most_common():
            print(f'     {name:<28} {n:>5} 檔')
        for s in SCREEN_ERROR_SAMPLES:
            print(f'     例：{s}')
        if SCREEN_ERRORS.get('RateLimited'):
            print(f'     ↑ 其中 {SCREEN_ERRORS["RateLimited"]} 檔連補問都還被 Yahoo'
                  ' 限流（限流是按 IP 算的，而 runner 的出口 IP 和別人共用）。'
                  '這幾檔沒有進入今天的篩選母體。')
    if _breaker.trips:
        print(f'🧯 期間踩了 {_breaker.trips} 次煞車，總共停了 {_breaker.slept:.0f} 秒'
              '（連續被限流就整批停一下，不要繼續去撞已經用完的額度）。')
    print('='*60)

    os.makedirs(output_dir, exist_ok=True)

    print('\n🎨 [3/3] 產生互動式個股技術線圖（HTML）...')
    html_path = build_interactive_html(
        RESULTS, today_str, output_dir, now,
        link_base=link_base, plotly_cdn=plotly_cdn,
        chart_years=chart_years, rules=rules,
        snapshots=sorted(SNAPSHOTS, key=lambda x: x['code']),
        extra_charts=CHARTS, data_dir=data_dir, data_base=data_base,
        cross=CROSS,
        asof=None if _asof[0] is None else f'{_asof[0]:%Y-%m-%d}',
    )

    index_path = ''
    if html_path and index_copy:
        index_path = index_copy if os.path.isabs(index_copy) else os.path.join(
            os.getcwd(), index_copy)
        os.makedirs(os.path.dirname(os.path.abspath(index_path)) or '.', exist_ok=True)
        with open(html_path, encoding='utf-8') as src, \
             open(index_path, 'w', encoding='utf-8') as dst:
            dst.write(src.read())
        print(f'📄 已複製一份為：{index_path}')

    if open_when_done:
        for target in (html_path,):
            if not target:
                continue
            try:
                if platform.system() == 'Windows':
                    os.startfile(target)                      # noqa: S606
                elif platform.system() == 'Darwin':
                    _sp.Popen(['open', target])
                else:
                    _sp.Popen(['xdg-open', target])
                print(f'📂 已開啟：{target}')
            except Exception as e:
                print(f'⚠️ 自動開啟失敗（{target}）：{e}')

    snap_path = ''
    if SNAPSHOTS:
        import gzip as _gzip
        stamp = (f'{_asof[0]:%Y-%m-%d}' if _asof[0] is not None else today_str)
        snap_path = os.path.join(output_dir, f'snapshot_{stamp}.json.gz')
        with _gzip.open(snap_path, 'wt', encoding='utf-8') as fh:
            json.dump({
                'asof': stamp,
                'generated_at': now.isoformat(),
                'version': VERSION,
                'rules': {f.name: getattr(rules, f.name) for f in fields(rules)},
                'columns': list(SNAPSHOT_COLUMNS),
                'rows': [snapshot_row(s) for s in
                         sorted(SNAPSHOTS, key=lambda x: x['code'])],
            }, fh, ensure_ascii=False, separators=(',', ':'))
        print(f'🗄️ 全市場快照：{snap_path}'
              f'（{len(SNAPSHOTS)} 檔，{os.path.getsize(snap_path) / 1024:.0f} KB）')

    print()
    print('=' * 60)
    print(f'  篩選完成！通過 {len(RESULTS)} 檔，產出在 {output_dir}')
    print('=' * 60)
    return {
        'snapshot_file': snap_path,
        'results': RESULTS,
        'count': len(RESULTS),
        'universe': len(TICKERS),
        'scanned': _scanned[0],
        'errors': errors,
        'too_short': _too_short[0],
        'error_kinds': dict(SCREEN_ERRORS),
        'error_samples': list(SCREEN_ERROR_SAMPLES),
        'date': today_str,
        'asof': None if _asof[0] is None else f'{_asof[0]:%Y-%m-%d}',
        'html': html_path or '',
        'index': index_path,
        'rules': rules,
        'rules_changed': rules.changed(),
        'snapshots': sorted(SNAPSHOTS, key=lambda x: x['code']),
    }





LIVE_FIELDS: tuple[tuple[str, str, str, str, str], ...] = (
    ('min_price',  '股價下限',   '元',    '1',    ''),
    ('min_vol20',  '20日均量',   '股',    '100',  ''),
    ('min_amount', '20日均額',   '百萬',  '10',   ''),
    ('lookback',   '回看天數',   '日',    '1',    str(SNAPSHOT_DAYS)),
    ('squeeze',    '壓縮門檻',   '',      '0.01', ''),
    ('vol_ratio',  '量比下限',   '倍',    '0.1',  ''),
    ('atr_stop',   '停損倍數',   '×ATR',  '0.5',  ''),
    ('min_six',    '六大評分',   '分以上', '0.1',  ''),
    ('min_rr',     '報酬風險比', '倍以上', '0.1',  ''),
)

CROSS_DEFAULTS: dict[str, float] = {'min_six': 0.0, 'min_rr': 0.0}


def _live_block(rules, snapshots=None, drawn=None, link_base='', data_base='',
                data_ver='', cross=None):
    if rules is None:
        return ''
    items = ''.join(
        f'<li><b>{escape(label)}</b><span>{escape(text)}</span></li>'
        for label, text in rules.describe()
    )
    tip = (
        '<template id="tf-rules">'
        f'<ol>{items}</ol>'
        '<p class="stop">停損：進場後設在 <b>'
        f'收盤 − {rules.atr_stop:g} × ATR(14)</b>，'
        '固定不放寬；收盤跌破 20MA 考慮出場。</p></template>'
    )
    if not snapshots:
        return tip

    fields = ''.join(
        f'<label><span>{escape(label)}</span>'
        f'<input id="f_{key}" type="number" min="0" step="{step}"'
        + (f' max="{cap}"' if cap else '')
        + f' value="{_live_default(rules, key):g}">'
        f'<em>{escape(unit)}</em></label>'
        for key, label, unit, step, cap in LIVE_FIELDS
    )
    rows = json.dumps([snapshot_row(x) for x in snapshots],
                      ensure_ascii=False, separators=(',', ':'))
    drawn_js = json.dumps(dict(drawn or {}), ensure_ascii=False, sort_keys=True)
    link_js = json.dumps((link_base or '').rstrip('/'))
    data_js = json.dumps((data_base or '').rstrip('/'))
    ver_js = json.dumps(str(data_ver or ''))
    cross_js = json.dumps({'as_of': (cross or {}).get('as_of', ''),
                           'quarter': (cross or {}).get('quarter', '')},
                          ensure_ascii=False)
    cols = json.dumps({k: i for i, k in enumerate(SNAPSHOT_COLUMNS)})
    return f"""
      <div id="live">
        <div class="live-fields" id="tf-fields">{fields}</div>
        <div class="live-bar">
          <button type="button" class="tf-fold" id="tf-fold" aria-expanded="true"
                  aria-controls="tf-fields" onclick="tfFoldToggle()">調整門檻</button>
          <button type="button" class="go" onclick="tfApply()">篩選</button>
          <button type="button" onclick="tfReset()">回到預設</button>
          <span id="live-count"></span>
          {tip}
        </div>
      </div>
      <script type="application/json" id="tf-snap">{rows}</script>
      <script>
      // 這一段裡的 tfPass 是 `pipeline.passes()` 的逐行翻譯。改了一邊要改另一邊，
      // 而 tests/test_snapshot.py 會在兩邊不一致的時候紅。
      const TF_DAYS = {SNAPSHOT_DAYS};
      const TF_KEYS = {json.dumps([f[0] for f in LIVE_FIELDS])};
      const TF_DEFAULT = {json.dumps({f[0]: _live_default(rules, f[0]) for f in LIVE_FIELDS})};
      // 〔趨勢∩六大∩報酬〕那個入口的門檻。網址帶 #cross 才套用。
      const TF_CROSS_DEFAULT = {json.dumps(CROSS_DEFAULTS)};
      // 六大與估值是哪一天的——一份沒有日期的估值，和一份標錯日期的估值，
      // 後者比較糟。
      const TF_CROSS_AS_OF = {cross_js};
      // 欄位名 → 在那一列裡的位置。寫成名字而不是數字，是因為 `row[12]` 這種
      // 東西在 SNAPSHOT_COLUMNS 中間插一欄之後會安靜地變成另一個指標。
      const C = {cols};
      // 有圖的那幾檔：代號 → 側欄第幾張。放寬門檻多出來的股票不在裡面。
      const TF_DRAWN = {drawn_js};
      const TF_LINK = {link_js};
      // 沒過預設篩選的那幾檔，圖表資料放在這個路徑底下（一檔一個 <代號>.json）。
      // 空字串代表這一份報告沒有附圖表資料（例如本機自己跑一份）。
      const TF_DATA = {data_js};
      // 這一份報告的建置時間戳。它跟在圖表資料的網址後面（`?v=…`），
      // 而那一個查詢字串修掉兩個不同的毛病：
      //
      // 1. **昨天的圖配今天的報告**。`trend-d/<代號>.json` 每天整批換掉，
      //    網址卻一模一樣。瀏覽器（和 Pages 前面的 CDN）照 max-age 留著
      //    舊的那一份，於是今天的報告畫出昨天的 K 線——不報錯，只是最後
      //    一根不見了，而那正是你在看的那一根。
      // 2. **被快取起來的 404**。報告是先推 report 分支、Pages 隨後才發布
      //    的，中間有一小段時間報告已經更新、資料還沒上去。那時候點一檔
      //    沒過預設篩選的股票會拿到 404——而 404 是可以被快取的，所以就算
      //    一分鐘後檔案已經在那裡，同一個瀏覽器還是會一直重播那個 404。
      //    （這一版之前用的是 `cache: 'force-cache'`，那個設定的意思正是
      //    「有快取就用，不要問伺服器」——等於把那個 404 釘死。）
      const TF_VER = {ver_js};
      const TF_CACHE = {{}};        // 抓過就不再抓第二次
      let TF_ROWS = [];
      let TF_HITS = [];
      let TF_SHORT = [];

      function tfRules() {{
        const r = {{}};
        for (const k of TF_KEYS) {{
          const el = document.getElementById('f_' + k);
          const v = parseFloat(el && el.value);
          r[k] = isFinite(v) ? v : TF_DEFAULT[k];
        }}
        r.min_amount *= 1e6;          // 畫面上是百萬，判定用元
        return r;
      }}

      // 回傳 null 代表沒過；過了就回觸發訊號。和 passes() 同一個順序、
      // 同一組比較符號——`<=` 和 `<` 在邊界上是不同的答案。
      function tfPass(row, r) {{
        const close = row[C.close], vol20 = row[C.vol20], amt20 = row[C.amt20];
        const crossAgo = row[C.cross_ago], bw = row[C.bw];
        const volRatio = row[C.vol_ratio];
        if (close <= r.min_price) return null;
        if (vol20 <= r.min_vol20 || amt20 <= r.min_amount) return null;
        // ② 讀快照裡算好的布林值，不再自己拿 ma20/ma60 比一次——那兩個數字
        // 是四捨五入過的，差在小數第三位的時候會被捨成同一個值。ma20/ma60
        // 現在只給畫面看。（Python 那邊是 `if not snap['trend_ok']`。）
        if (!row[C.trend_ok]) return null;
        // 夾住上下界、再取整——和 passes() 那一行的 min(max(int(…),0),DAYS) 同義。
        const look = Math.min(Math.max(Math.floor(r.lookback), 0), TF_DAYS);
        const golden = crossAgo > 0 && crossAgo <= look;
        let squeeze = false;
        for (const v of bw.slice(bw.length - look)) {{ if (v <= r.squeeze) {{ squeeze = true; break; }} }}
        if (!golden && !squeeze) return null;
        // ④ 的兩個突破同理，見上面那一段。
        const brkBoll = row[C.brk_boll], brkDon = row[C.brk_don];
        if (!brkBoll && !brkDon) return null;
        if (volRatio < r.vol_ratio) return null;
        const t = [];
        if (golden) t.push('黃金交叉');
        if (squeeze) t.push('布林壓縮');
        if (brkBoll) t.push('突破布林上軌');
        if (brkDon) t.push('突破20日高點');
        return t;
      }}

      function tfEsc(s) {{
        return String(s).replace(/[&<>"]/g, function (c) {{
          return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];
        }});
      }}

      // 第五道關卡：六大評分與報酬風險比。`cross_passes()` 的逐行翻譯，
      // tests/test_snapshot.py 兩邊對過。門檻 0 等於這一關不啟用。
      //
      // 刻意**不寫在 tfPass 裡**：tfPass 是四部曲，而四部曲和這一關講的是兩件
      // 事。混在一起的話，〔台股趨勢選股〕那一頁篩出什麼就會取決於另一個 repo
      // 今天有沒有把檔案發出來。
      function tfCross(row, r) {{
        if (r.min_six > 0) {{
          const six = row[C.six];
          if (six === null || six === undefined || six <= r.min_six) return false;
        }}
        if (r.min_rr > 0) {{
          if (row[C.rr_free]) return true;   // 沒有下檔風險，比任何門檻都好
          const rr = row[C.rr];
          if (rr === null || rr === undefined || rr <= r.min_rr) return false;
        }}
        return true;
      }}

      // 「它不是被門檻刷掉的，是根本沒有數字可比」。
      //
      // 這兩種要分開，因為它們該給讀者的動作不一樣：被刷掉的是「看過了，不
      // 合格」，沒有數字的是「還不知道」——而 6770 力積電那種六大 3.0、只差
      // 一張〔年度交易資訊〕的，混在一起就會無聲消失。
      function tfCrossUnknown(row, r) {{
        const six = row[C.six], rr = row[C.rr];
        if (r.min_six > 0 && (six === null || six === undefined)) return true;
        if (r.min_rr > 0 && !row[C.rr_free] && (rr === null || rr === undefined)) return true;
        return false;
      }}

      function tfCrossWhy(row, r) {{
        if (r.min_six > 0 && (row[C.six] === null || row[C.six] === undefined)) {{
          return '沒有六大評分';
        }}
        return '沒有報酬風險比（多半是歷年本益比不足 3 個完整年度，算不出本益比區間）';
      }}

      // 「這一檔為什麼沒有報酬風險比」——只有兩種，而它們的意思正好相反。
      //
      // 三種非數字的值都寫成字，不用符號：∞ 要先知道它代表什麼才看得懂，而
      // 0.00 看起來像「算出來剛好是零」，真正的意思是往下。隔壁
      // 〔台股評等清單〕那一欄用的是同一套字，兩邊對得起來。
      function tfRrText(row) {{
        if (row[C.rr_free]) return '無風險';
        const rr = row[C.rr];
        if (rr === null || rr === undefined) return '—';
        // 目標價低於股價＝價格高估（估值判斷）。2026-10-14 前叫「空頭」，但那是走勢用語：
        // 高估值的股票走勢照樣可以是多頭，而這一頁本來就是在篩多頭走勢。
        return rr <= 0 ? '價格高估' : rr.toFixed(2);
      }}

      // 一張側欄卡片。這是**唯一**一份實作——Python 那一份拿掉了。
      // 收盤價旁邊那顆小圖示：開 Yahoo 股市那一檔的〔技術分析〕（新分頁）。
      // 網址只帶代號、不帶 .TW／.TWO——Yahoo 自己會導到對的市場（實測上市、上櫃
      // 都是 200、標題是對的那一檔），而這份快照裡本來就沒有市場別。
      // stopPropagation：整張卡片點下去是開這一頁的圖，點圖示不該連圖一起換掉。
      function tfYahoo(code) {{
        return '<a class="nb-yf" href="https://tw.stock.yahoo.com/quote/' +
          encodeURIComponent(code) + '/technical-analysis" target="_blank" rel="noopener"' +
          ' title="Yahoo股市技術分析" aria-label="' + tfEsc(code) + ' Yahoo 技術分析"' +
          ' onclick="event.stopPropagation()"></a>';
      }}

      // 台股慣例：漲紅、跌綠。
      function tfCard(row, triggers, i, shortWhy) {{
        const code = row[C.code], chg = row[C.chg], pct = row[C.chg_pct];
        const cls = chg > 0 ? 'up' : (chg < 0 ? 'dn' : 'fl');
        const sign = chg > 0 ? '▲ ' : (chg < 0 ? '▼ ' : '');
        // 「這一檔點下去有沒有圖」現在有兩個來源：數列嵌在這一頁上（TF_DRAWN
        // ——排程當天過篩的那幾檔），或者這份報告附了圖表資料（TF_DATA），那
        // 全市場每一檔都抓得到。後者才是常態，所以只看 TF_DRAWN 的話，放寬門檻
        // 多出來的股票每一張卡片都會被標成「沒有圖」——而它們其實點得開。
        const drawn = Object.prototype.hasOwnProperty.call(TF_DRAWN, code) || !!TF_DATA;
        const tags = triggers.map(function (t) {{
          return '<span class="nb-tag">' + tfEsc(t) + '</span>';
        }}).join('');
        return '<div class="nb' + (drawn ? '' : ' nochart') +
          (shortWhy ? ' nbshort' : '') + '" id="sb-' + i +
          '" data-code="' + tfEsc(code) + '" onclick="tfOpen(\\'' + tfEsc(code) + '\\')">' +
          '<div class="nb-top"><span class="nb-code">' + tfEsc(code) + '</span>' +
          '<span class="nb-px"><span class="nb-close ' + cls + '">' + row[C.close].toFixed(2) +
          '</span>' + tfYahoo(code) + '</span></div>' +
          '<div class="nb-bot"><span class="nb-name">' + tfEsc(row[C.name]) + '</span>' +
          '<span class="nb-pct ' + cls + '">' + sign + Math.abs(chg).toFixed(2) +
          ' (' + pct.toFixed(2) + '%)</span></div>' +
          '<div class="nb-ind">' + tfEsc(row[C.industry] || '') + '</div>' +
          (shortWhy
            ? '<div class="nb-why">' + tfEsc(shortWhy) + '</div>' : '') +
          '<div class="nb-tagrow">' + tags + '</div>' +
          tfCrossLinks(row) + '</div>';
      }}

      // 卡片最底下那兩顆：〔六大 2.17 ↗〕〔報酬/風險 1.77 ↗〕。
      //
      // **永遠顯示**，不只在 #cross 那個入口——一檔技術面在動的股票，下一個問題
      // 本來就是體質和價位，而那兩個數字已經在手上。
      //
      // 它們是**按鈕**，直接跳到個股頁對應的那一個分頁：六大 → #six（〔六大財務
      // 指標評等〕），報酬/風險 → #eps（〔EPS預估與估價〕，報酬風險比就是那一頁
      // 算的）。個股頁把分頁寫在網址的 hash 上，開啟時會直接停在那一頁。
      //
      // 手機與桌機是**同一個位置**（卡片最底下）。以前桌機是一行小字夾在產業和
      // 觸發訊號之間、另外再一顆〔六大↗〕；手機則因為觸發訊號收起來，那一行自然
      // 落到底。兩種排法讓同一個東西在兩個寬度上找的位置不一樣。
      //
      // 對面沒發資料的時候整列不出現，而不是印兩個破折號佔一行。沒有 TF_LINK
      // （本機版）的時候是兩個不能點的標籤，不是兩個點了 404 的連結。
      function tfCrossLinks(row) {{
        const six = row[C.six];
        const hasSix = six !== null && six !== undefined;
        const hasRr = row[C.rr] !== null && row[C.rr] !== undefined || row[C.rr_free];
        if (!hasSix && !hasRr) return '';
        const code = row[C.code];
        const rrTitle = '報酬風險比（無風險 ＝ 股價已低於下檔價；價格高估 ＝ 目標價低於現價、預期報酬為負）' +
          (TF_CROSS_AS_OF.as_of ? '。估值基準 ' + TF_CROSS_AS_OF.as_of +
            '，財報 ' + (TF_CROSS_AS_OF.quarter || '?') : '');
        function one(label, value, tab, title) {{
          const text = '<span class="k">' + label + '</span> <b>' + value + '</b>';
          if (!TF_LINK) {{
            return '<span class="nb-lnk" title="' + tfEsc(title) + '">' + text + '</span>';
          }}
          return '<a class="nb-lnk" href="' + TF_LINK + '/' + tfEsc(code) + '.html#' + tab +
            '" target="_blank" rel="noopener" title="' + tfEsc(title) + '"' +
            ' onclick="event.stopPropagation()">' + text + '<i>↗</i></a>';
        }}
        return '<div class="nb-cross">' +
          one('六大', hasSix ? six.toFixed(2) : '—', 'six',
              '六大財務指標最新綜合評分——開啟〔六大財務指標評等〕') +
          one('報酬/風險', tfRrText(row), 'eps', rrTitle + '——開啟〔EPS預估與估價〕') +
          '</div>';
      }}

      function tfApply() {{
        const r = tfRules();
        TF_HITS = [];
        TF_SHORT = [];
        for (const row of TF_ROWS) {{
          const t = tfPass(row, r);
          if (!t) continue;
          if (tfCross(row, r)) TF_HITS.push([row, t]);
          // 四部曲過了、第五關因為**沒有資料**沒過的那幾檔。門檻是 0 的時候
          // 這一籃一定是空的（tfCross 直接回 true）。
          else if (tfCrossUnknown(row, r)) TF_SHORT.push([row, t]);
        }}
        // 量比高的排前面，同量比照代號。和 Excel 那一份同一個排法。
        TF_HITS.sort(function (a, b) {{
          return b[0][C.vol_ratio] - a[0][C.vol_ratio] ||
                 (a[0][C.code] < b[0][C.code] ? -1 : 1);
        }});
        const n = document.getElementById('live-count');
        n.textContent = TF_HITS.length + ' 檔符合';
        n.className = TF_HITS.length ? 'hit' : 'miss';
        const list = document.getElementById('sb-list');
        // 只畫前 300 張：再多就不是拿來讀的了，而 DOM 會開始卡。
        let html = TF_HITS.slice(0, 300).map(function (h, i) {{
          return tfCard(h[0], h[1], i);
        }}).join('') + (TF_HITS.length > 300
          ? '<div class="sb-more">還有 ' + (TF_HITS.length - 300) +
            ' 檔沒列出來——把門檻收緊一點。</div>' : '');
        if (!TF_HITS.length) {{
          html += '<div class="sb-more">今天沒有一檔同時過這幾關。' +
                  '把門檻調鬆一點，或看下面那幾檔還缺什麼。</div>';
        }}
        // 資料不足的那幾檔列在名單**下方**，標示原因。
        //
        // 不列的話它們會無聲消失，而「消失」和「不合格」在畫面上長得一模一樣
        // ——6770 力積電六大 3.0，只差一張〔年度交易資訊〕。
        if (TF_SHORT.length) {{
          TF_SHORT.sort(function (a, b) {{
            return (b[0][C.six] || -1) - (a[0][C.six] || -1) ||
                   (a[0][C.code] < b[0][C.code] ? -1 : 1);
          }});
          html += '<div class="sb-sep">資料不足，沒有納入交集（' +
                  TF_SHORT.length + ' 檔）</div>' +
                  TF_SHORT.slice(0, 60).map(function (h, i) {{
                    return tfCard(h[0], h[1], TF_HITS.length + i, tfCrossWhy(h[0], r));
                  }}).join('');
        }}
        list.innerHTML = html;
        // 門檻改過之後，原本選中的那一檔可能已經不在名單上了。還在就留著，
        // 不在就跳到新名單的第一張——空著一張圖停在畫面上比切走更難懂。
        const still = TF_HITS.findIndex(function (h) {{ return h[0][C.code] === TF_CUR; }});
        if (still >= 0) tfMark(still);
        else if (TF_HITS.length) tfOpen(TF_HITS[0][0][C.code]);
        else tfOpen(null);
      }}

      let TF_CUR = null;

      function tfMark(i) {{
        const cards = document.querySelectorAll('#sb-list .nb');
        for (const c of cards) c.classList.remove('act');
        if (!cards[i]) return;
        cards[i].classList.add('act');
        // 手機上側欄是一條橫向滑動的列，選到的那一張可能在畫面外——換到它卻看
        // 不到哪一張亮著，等於不知道自己在看哪一檔。
        if (cards[i].scrollIntoView) {{
          cards[i].scrollIntoView({{block: 'nearest', inline: 'nearest'}});
        }}
      }}

      // 點一張卡片。
      //
      // 每一檔走的都是同一條路。今天過篩的那幾檔的數列已經嵌在這一頁上（
      // tf-series），其他每一檔各有一個自己的 <代號>.json，點下去才抓——所以
      // 放寬門檻多出來的股票也看得到一樣完整的 K 線圖，而這一頁不必背著
      // 1,900 檔的 K 棒。
      //
      // 以前這裡分兩條：嵌過的叫 showChart() 去切一格早就畫好的圖，其他的叫
      // tfFetchChart() 畫在另一格。那兩格長得不一樣——後者沒有徽章、沒有觸發
      // 條件、沒有時間範圍按鈕、滑鼠移上去也沒有十字線。同一份報告裡，同一張圖
      // 兩種待遇，而哪一種取決於「排程當天它有沒有過篩」，和讀者無關。
      function tfOpen(code) {{
        TF_CUR = code;
        const i = TF_HITS.findIndex(function (h) {{ return h[0][C.code] === code; }});
        tfMark(i);
        if (code === null) {{ tfNoChart(null); return false; }}
        tfFetchChart(code);
        return false;
      }}

      // 抓一檔的圖表資料，畫在圖表區。
      //
      // 同一檔只抓一次（TF_CACHE）。抓的時候先說「載入中」——15 KB 在手機上
      // 也要一下子，而一片空白看起來像壞掉。
      // 抓回來的是**數列**，不是一整張圖。
      //
      // 以前每一檔在 Pages 上是一份完整的 plotly 圖 JSON（約 240 KB）：日期那
      // 六百多個字串被十一條 trace 各存了一份，customdata 又把每條數列原封不動
      // 再存一次，然後同一套兩百行的樣式每一檔都帶一份。現在檔案裡只有數列，
      // 樣式由頁面上那份共用的樣板提供（tfSeriesFig）——59 KB，gzip 之後 11.5 KB。
      //
      // 對讀者是點一檔股票下載 39 KB 變 11.5 KB；對排程是每天往 Pages 推的
      // 那包從四百多 MB 變一百 MB。而且圖和頁面內嵌那幾張走的是同一條組裝路徑，
      // 不可能長得不一樣。
      // 抓一檔的數列。`mode` 是 fetch 的 cache 模式：第一趟用 'default'（照
      // HTTP 的規矩走，Pages 給的 max-age 內重複點同一檔仍然是命中快取），
      // 404 之後那一趟用 'reload'（一定去問伺服器）。
      async function tfGrab(code, mode) {{
        const url = TF_DATA + '/' + code + '.json' +
                    (TF_VER ? '?v=' + encodeURIComponent(TF_VER) : '');
        const r = await fetch(url, {{cache: mode}});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const s = await r.json();
        if (!s || !s.d || !s.d.length) throw new Error('這一檔的資料是空的');
        return s;
      }}

      async function tfFetchChart(code) {{
        // 今天過篩的那幾檔的數列本來就在頁面上——那些不必跑一趟網路。
        let s = (typeof tfSeriesOf === 'function') ? tfSeriesOf(code) : null;
        if (!s) {{
          if (!TF_DATA) {{ tfNoChart(code); return; }}
          tfShowPane(code, '載入 ' + code + ' 的走勢圖…');
          s = TF_CACHE[code];
          if (!s) {{
            try {{
              s = await tfGrab(code, 'default');
            }} catch (e) {{
              // 404 再試一次，這次繞過快取。
              //
              // 為什麼值得多跑一趟：報告是先推 report 分支、Pages 隨後才發布的，
              // 中間那一小段時間點下去就是 404。那不是「這一檔沒有圖」，是「還沒
              // 上去」——而使用者看到的訊息會是前者，然後他會去找一個不存在的
              // 問題。重試一次就把那一段時間內的誤判清掉。
              //
              // 只重試一次、而且只在 404：其他錯誤（斷線、JSON 壞掉）重試只是
              // 讓使用者多等一倍的時間看同一句話。
              if (String(e.message).indexOf('404') >= 0) {{
                try {{ s = await tfGrab(code, 'reload'); }} catch (e2) {{ e = e2; }}
              }}
              if (!s) {{
                // 換過股票了就不要把錯誤蓋在新的那一張上面。
                if (TF_CUR === code) tfNoChart(code, e.message);
                return;
              }}
            }}
            TF_CACHE[code] = s;
          }}
        }}
        if (TF_CUR !== code) return;      // 抓回來的時候使用者已經點了別檔
        if (typeof tfDraw !== 'function') {{ tfNoChart(code, '圖表程式沒有載入'); return; }}
        tfShowPane(code, '');
        tfDraw(code, s);
      }}

      // 把圖表區露出來，順便把標題換成這一檔。
      //
      // 這一格是**唯一**的圖表區（見 build_interactive_html 裡 `chart_divs`
      // 那一段）：徽章、觸發條件、時間範圍按鈕、貫穿上下圖的十字線都在它身上，
      // 所以每一檔都拿得到同一套。
      function tfShowPane(code, msg) {{
        const col = document.getElementById('chartcol');
        const pane = document.getElementById('tf-pane');
        const note = document.getElementById('tf-note');
        if (note) note.hidden = true;
        if (col) col.classList.add('noting');
        pane.hidden = false;
        document.getElementById('tf-plot-msg').textContent = msg;
        // 標題帶名字：只有代號的話，切了三檔之後分不出自己在看哪一家。
        const row = (TF_HITS.find(function (h) {{ return h[0][C.code] === code; }}) || [])[0];
        document.getElementById('tf-plot-title').textContent =
          row ? (code + '　' + row[C.name]) : code;
      }}

      function tfHideNote() {{
        const el = document.getElementById('tf-note');
        if (el) el.hidden = true;
        const pane = document.getElementById('tf-pane');
        if (pane) pane.hidden = true;
        const col = document.getElementById('chartcol');
        if (col) col.classList.remove('noting');
      }}

      // 沒有圖的時候，圖表區要說出**為什麼**，而不是留一片空白。
      function tfNoChart(code, why) {{
        const el = document.getElementById('tf-note');
        const col = document.getElementById('chartcol');
        const pane = document.getElementById('tf-pane');
        if (!el) return;
        if (pane) pane.hidden = true;
        el.hidden = false;
        if (col) col.classList.add('noting');
        if (code === null) {{
          el.innerHTML = '<b>這組門檻下沒有任何一檔通過。</b>' +
            '<p>把門檻放寬一點，或按〔回到預設〕。</p>';
          return;
        }}
        const six = TF_LINK
          ? '<p><a href="' + TF_LINK + '/' + code + '.html" target="_blank" rel="noopener">' +
            '到〔六大財務指標評等〕看 ' + code + ' ↗</a></p>'
          : '';
        // 兩種情況，訊息不一樣：這一份報告根本沒附圖表資料（例如本機自己跑
        // 一份），或是有附但這一檔抓不到（網路、檔案不在）。混在一起講，讀者
        // 會去修錯的東西。
        const head = TF_DATA
          ? '<b>' + tfEsc(code) + '：圖表資料拿不到。</b>' +
            '<p>' + tfEsc(why || '') + '</p>'
          : '<b>' + tfEsc(code) + '：這一份報告沒有附圖表資料。</b>' +
            '<p>只有今天通過<b>預設門檻</b>的那幾檔有圖。' +
            '排程產的那一份每一檔都有。</p>';
        el.innerHTML = head + six;
      }}

      // 「回到預設」回到的是**這個入口的**預設，也就是 TF_CROSS_DEFAULT。
      // 那一組目前是 0 和 0（＝第五關不啟用，見 Python 的 CROSS_DEFAULTS），
      // 所以現在兩個入口回到的是同一組；它留在這裡，是因為那一組隨時可能
      // 再被調起來，而「回到預設」該回到的一直是**這個入口的**那一組。
      function tfDefaults() {{
        const d = Object.assign({{}}, TF_DEFAULT);
        if (tfCrossMode()) Object.assign(d, TF_CROSS_DEFAULT);
        return d;
      }}

      function tfCrossMode() {{
        return (location.hash || '').toLowerCase().indexOf('cross') >= 0;
      }}

      function tfReset() {{
        const d = tfDefaults();
        for (const k of TF_KEYS) {{
          const el = document.getElementById('f_' + k);
          if (el) el.value = d[k];
        }}
        tfApply();
      }}

      // 〔調整門檻〕的開關。手機上預設收起來（見 CSS 的 .tf-fold）。
      //
      // 收合本身是 CSS 的事（`#live.folded .live-fields{{display:none}}`），這裡
      // 只負責那個 class、無障礙屬性、按鈕上的字，以及**收合之後重新量一次圖**
      // ——頁首高度變了，`--hd-h` 和 plotly 的畫布都要跟著改，否則圖會停在
      // 它上一次被量到的尺寸。
      function tfFoldToggle() {{
        const box = document.getElementById('live');
        const btn = document.getElementById('tf-fold');
        if (!box) return;
        const folded = box.classList.toggle('folded');
        if (btn) {{
          btn.setAttribute('aria-expanded', folded ? 'false' : 'true');
          btn.textContent = folded ? '調整門檻' : '收起門檻';
        }}
        if (typeof syncHdHeight === 'function') syncHdHeight();
        const a = document.getElementById('tf-plot');
        if (a && typeof fitPlotSize === 'function') {{
          requestAnimationFrame(function () {{ fitPlotSize(a); }});
        }}
      }}

      document.addEventListener('DOMContentLoaded', function () {{
        try {{ TF_ROWS = JSON.parse(document.getElementById('tf-snap').textContent); }}
        catch (e) {{ TF_ROWS = []; }}
        // 同一份報告，兩個入口。`#cross` 把第五關的兩個門檻預先填成
        // TF_CROSS_DEFAULT——**目前那一組是 0 和 0**，也就是這一關不啟用，
        // 兩個入口篩出來的東西一模一樣。那一組很嚴（實測 25 檔趨勢入選裡只有
        // 1 檔同時過另外兩關），所以預設先不啟用，要用的人把數字打上去。
        //
        // 做成兩份 HTML 的話，趨勢圖、卡片、圖表資料那一整套都要維護兩次，
        // 而它們沒有任何一處該不一樣。
        if (tfCrossMode()) {{
          for (const k in TF_CROSS_DEFAULT) {{
            const el = document.getElementById('f_' + k);
            if (el) el.value = TF_CROSS_DEFAULT[k];
          }}
          document.body.classList.add('crossmode');
        }}
        // 手機上輸入框預設收起來。收起來的是輸入框，不是功能——〔篩選〕、
        // 〔回到預設〕、〔幾檔符合〕和那顆燈泡都留在同一列上。
        //
        // 門檻本身照樣生效：收合只是 `display:none`，`f_*` 那些 input 還在
        // DOM 裡，`tfApply()` 讀得到它們的值（包括 #cross 預先填進去的那組）。
        if (window.matchMedia('(max-width:760px)').matches) {{
          const box = document.getElementById('live');
          const btn = document.getElementById('tf-fold');
          if (box) box.classList.add('folded');
          if (btn) btn.setAttribute('aria-expanded', 'false');
        }}
        // 改門檻**不會**馬上重篩——要按〔篩選〕（或在任何一格按 Enter）。
        //
        // 邊打邊篩那一版試過：打「1200」的過程中會先用 1、12、120 各篩一次，
        // 左邊那排卡片跳三次，而且每一次都可能把你正在看的那一檔換掉。
        // 門檻是一組值，不是一個值——要一起生效。
        for (const k of TF_KEYS) {{
          const el = document.getElementById('f_' + k);
          if (!el) continue;
          el.addEventListener('input', tfStale);
          el.addEventListener('keydown', function (e) {{
            if (e.key === 'Enter') {{ e.preventDefault(); tfApply(); }}
          }});
        }}
        tfApply();
      }});

      // 門檻改過、還沒按〔篩選〕：把數字淡掉並說一聲。
      // 不說的話，畫面上那個「368 檔符合」會變成一個安靜的謊。
      function tfStale() {{
        const n = document.getElementById('live-count');
        n.textContent = '門檻改了——按〔篩選〕';
        n.className = 'stale';
      }}
      </script>"""


def _live_default(rules, key):
    v = getattr(rules, key)
    return v / 1e6 if key == 'min_amount' else v


def _snap_note(snapshots):
    return f'{len(snapshots):,} 檔的當日指標'


def _script_json(el_id, payload):
    import json as _json

    body = _json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return ('<script type="application/json" id="' + el_id + '">'
            + body.replace('</', '<\\/') + '</script>')


SERIES_ARRAYS = ('o', 'h', 'l', 'c', 'm20', 'm60', 'bu', 'bm', 'bd',
                 'vl', 'vm', 'vr')


def _chart_series(res, keep_rows, sf):
    df = res['_df'].copy()
    if keep_rows and len(df) > keep_rows:
        df = df.iloc[-keep_rows:]
    ma20 = res['_ma20'].reindex(df.index).ffill().bfill()
    ma60 = res['_ma60'].reindex(df.index).ffill().bfill()
    bup = res['_boll_up'].reindex(df.index).ffill().bfill()
    bmid = res['_boll_mid'].reindex(df.index).ffill().bfill()
    bdn = res['_boll_dn'].reindex(df.index).ffill().bfill()
    vol = (df['Volume'] / 1000).round(0)
    vmean = vol.rolling(20).mean().ffill().bfill()
    vratio = (vol / vmean).round(2)

    def arr(series, d=2):
        return [sf(v, d) for v in series]

    return {
        'code': res['code'],
        'name': res.get('name', ''),
        'd': df.index.strftime('%Y-%m-%d').tolist(),
        'o': arr(df['Open']), 'h': arr(df['High']),
        'l': arr(df['Low']), 'c': arr(df['Close']),
        'm20': arr(ma20), 'm60': arr(ma60),
        'bu': arr(bup), 'bm': arr(bmid), 'bd': arr(bdn),
        'vl': arr(vol, 0), 'vm': arr(vmean, 0), 'vr': arr(vratio),
        'stop': sf(res['stop_loss']),
        'ma20_last': sf(res['ma20_last']),
        'ma60_last': sf(res['ma60_last']),
    }


def _chart_figure(s):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    BG = '#0d1117'; AXES = '#161b22'; GRID = '#30363d'; TEXT = '#c9d1d9'
    MA20C = '#f0c27f'; MA60C = '#ff7b72'; BBC = '#58a6ff'; STOPC = '#ff4500'
    VUP = '#3fb950'; VDN = '#f85149'; VMAC = '#ffa657'

    dates = s['d']
    o_l, h_l, l_l, c_l = s['o'], s['h'], s['l'], s['c']
    m20, m60 = s['m20'], s['m60']
    bu, bm, bd = s['bu'], s['bm'], s['bd']
    vl, vm, vr = s['vl'], s['vm'], s['vr']
    stop = s['stop']
    N = len(dates)

    cd = [[dates[i], o_l[i], h_l[i], l_l[i], c_l[i],
           m20[i], m60[i], bu[i], bm[i], bd[i],
           vl[i], vm[i], vr[i]] for i in range(N)]
    cd_v = [[dates[i], vl[i], vm[i], vr[i]] for i in range(N)]

    htpl = (
        '<b style="font-size:13px">%{customdata[0]}</b><br>'
        '<span style="color:#8b949e">─────────────────────</span><br>'
        '開 <b>%{customdata[1]:.2f}</b> '
        '高 <b style="color:#3fb950">%{customdata[2]:.2f}</b> '
        '低 <b style="color:#f85149">%{customdata[3]:.2f}</b> '
        '收 <b style="color:#58a6ff">%{customdata[4]:.2f}</b><br>'
        '<span style="color:#f0c27f">20MA&nbsp;%{customdata[5]:.2f}</span>&nbsp;'
        '<span style="color:#ff7b72">60MA&nbsp;%{customdata[6]:.2f}</span><br>'
        '布林上&nbsp;%{customdata[7]:.2f}&nbsp;'
        '中&nbsp;%{customdata[8]:.2f}&nbsp;'
        '下&nbsp;%{customdata[9]:.2f}<br>'
        '<span style="color:#8b949e">─────────────────────</span><br>'
        '<span style="color:#3fb950">成交量 %{customdata[10]:,.0f} 張</span>  '
        '均量 %{customdata[11]:,.0f} 張  '
        '<span style="color:#ffa657">量比&nbsp;%{customdata[12]:.2f}x</span>'
        '<extra></extra>'
    )

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.68, 0.32], vertical_spacing=0.03)

    fig.add_trace(go.Scatter(
        x=dates, y=bu, mode='lines', name='布林上軌',
        line=dict(color=BBC, width=1.2, dash='dot'),
        showlegend=True, hoverinfo='skip',
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=dates, y=bd, mode='lines', name='布林下軌',
        line=dict(color=BBC, width=1.2, dash='dot'),
        fill='tonexty', fillcolor='rgba(88,166,255,0.08)',
        showlegend=True, hoverinfo='skip',
    ), row=1, col=1)

    fig.add_trace(go.Scatter(x=dates, y=bm, mode='lines', name='布林中軌',
        line=dict(color='#8b949e', width=0.8, dash='dot'),
        showlegend=False, hoverinfo='skip'), row=1, col=1)

    fig.add_trace(go.Candlestick(
        x=dates, open=o_l, high=h_l, low=l_l, close=c_l,
        increasing_line_color=VUP, decreasing_line_color=VDN,
        increasing_fillcolor=VUP,  decreasing_fillcolor=VDN,
        name='K棒', showlegend=True,
        hoverinfo='skip', line=dict(width=1),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(x=dates, y=m20, mode='lines',
        name='20MA 月線 {:.2f}'.format(s['ma20_last'] or 0.0),
        line=dict(color=MA20C, width=2.0),
        showlegend=True, hoverinfo='skip'), row=1, col=1)

    fig.add_trace(go.Scatter(x=dates, y=m60, mode='lines',
        name='60MA 季線 {:.2f}'.format(s['ma60_last'] or 0.0),
        line=dict(color=MA60C, width=2.0, dash='dash'),
        showlegend=True, hoverinfo='skip'), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=[dates[0], dates[-1]] if N else [], y=[stop, stop] if N else [],
        mode='lines', name='建議停損 {:.2f}'.format(stop or 0.0),
        line=dict(color=STOPC, width=1.5, dash='dashdot'),
        showlegend=True, hoverinfo='skip'), row=1, col=1)

    vcols = [VUP if (c_l[i] or 0) >= (o_l[i] or 0) else VDN for i in range(N)]
    fig.add_trace(go.Bar(x=dates, y=vl, name='成交量(張)',
        marker_color=vcols, opacity=0.85, showlegend=True,
        hoverinfo='skip'), row=2, col=1)

    fig.add_trace(go.Scatter(x=dates, y=vm, mode='lines', name='20日均量',
        line=dict(color=VMAC, width=1.5),
        showlegend=True, hoverinfo='skip'), row=2, col=1)

    fig.add_trace(go.Scatter(
        x=dates, y=c_l, mode='markers',
        marker=dict(opacity=0, size=14, color='rgba(0,0,0,0)'),
        customdata=cd, hovertemplate=htpl,
        showlegend=False, name='__hover_price__',
    ), row=1, col=1)

    htpl_v = ('成交量 <b style="color:#3fb950">%{customdata[1]:,.0f}</b> 張&nbsp;&nbsp;'
              '均量 %{customdata[2]:,.0f} 張&nbsp;&nbsp;'
              '<span style="color:#ffa657">量比 %{customdata[3]:.2f}x</span>'
              '<extra></extra>')
    fig.add_trace(go.Scatter(
        x=dates, y=vl, mode='markers',
        marker=dict(opacity=0, size=14, color='rgba(0,0,0,0)'),
        customdata=cd_v, hovertemplate=htpl_v,
        showlegend=False, name='__hover_vol__',
    ), row=2, col=1)

    fig.update_xaxes(
        showspikes=False,
        gridcolor=GRID, gridwidth=0.5, tickfont=dict(color=TEXT),
        showgrid=True,
    )
    fig.update_yaxes(
        showspikes=True, spikecolor='#8b949e', spikethickness=1,
        spikedash='dot', gridcolor=GRID, gridwidth=0.5,
        tickfont=dict(color=TEXT), zeroline=False,
    )
    fig.update_xaxes(rangeslider_visible=False)
    fig.update_layout(
        paper_bgcolor=BG, plot_bgcolor=AXES,
        font=dict(family='Microsoft JhengHei, Arial', color=TEXT, size=12),
        hovermode='x unified',
        hoverlabel=dict(bgcolor='rgba(13,17,23,0.95)', bordercolor='#444',
                        font=dict(color=TEXT, size=12,
                                  family='Microsoft JhengHei, Arial'),
                        align='left', namelength=0),
        legend=dict(orientation='h', x=0.5, xanchor='center',
                    y=1.015, yanchor='bottom',
                    bgcolor='rgba(22,27,34,0.9)', bordercolor=GRID,
                    borderwidth=1, font=dict(size=11)),
        margin=dict(t=72, b=26, l=64, r=26),
        height=760, dragmode='pan',
        autosize=True,
    )
    fig.update_yaxes(title_text='價格 (元)',  row=1, col=1,
                     title_font=dict(color='#8b949e', size=11))
    fig.update_yaxes(title_text='成交量(張)', row=2, col=1,
                     title_font=dict(color='#8b949e', size=11))

    if N:
        lo, hi, vhi = _chart_window(s)
        fig.update_layout(
            xaxis2=dict(range=[dates[max(0, N - 63)], dates[-1]]),
            yaxis=dict(range=[lo, hi], autorange=False),
            yaxis2=dict(range=[0, vhi], autorange=False),
        )
    return fig


def _chart_window(s):
    N = len(s['d'])
    d_from = max(0, N - 63)
    stop = s['stop']
    win_v = [v for v in s['vl'][d_from:] if v is not None]
    win_hi = max([v for v in s['h'][d_from:] if v is not None] +
                 [v for v in s['bu'][d_from:] if v is not None] or [1.0])
    win_lo = min([v for v in s['l'][d_from:] if v is not None] +
                 [v for v in s['bd'][d_from:] if v is not None] +
                 ([stop] if stop is not None else []) or [0.0])
    pad = (win_hi - win_lo) * 0.06 or (abs(win_hi) or 1.0) * 0.05
    return win_lo - pad, win_hi + pad, (max(win_v) if win_v else 1.0) * 1.12


def chart_template():
    import json as _json
    import plotly.io as pio

    blank = {
        'code': '', 'name': '',
        'd': ['2000-01-03', '2000-01-04'],
        'stop': 0.0, 'ma20_last': 0.0, 'ma60_last': 0.0,
    }
    for k in SERIES_ARRAYS:
        blank[k] = [0.0, 0.0]
    fig = _json.loads(pio.to_json(_chart_figure(blank)))
    for tr in fig['data']:
        for key in ('x', 'y', 'open', 'high', 'low', 'close', 'customdata'):
            if key in tr:
                tr[key] = []
        if isinstance(tr.get('marker'), dict) and isinstance(
                tr['marker'].get('color'), list):
            tr['marker']['color'] = []
    for axis in ('xaxis', 'xaxis2', 'yaxis', 'yaxis2'):
        if axis in fig.get('layout', {}):
            fig['layout'][axis].pop('range', None)
            fig['layout'][axis].pop('autorange', None)
    return fig


def build_interactive_html(results, today_str, output_dir, now=None, *,
                           link_base='', plotly_cdn=True, chart_years=2.0,
                           rules=None, snapshots=None,
                           extra_charts=None, data_dir='', data_base='',
                           cross=None, asof=None):
    try:
        import plotly.io  # noqa: F401  （只是確認裝得起來）
        import json as _json, math as _math
    except ImportError:
        print('plotly 未安裝，跳過互動線圖'); return None
    if rules is None or snapshots is None:
        raise ValueError(
            'build_interactive_html 需要 rules 與 snapshots：側欄那排卡片是從'
            '快照畫出來的，少了它們會產出一份沒有側欄的報告'
        )

    def sf(v, d=2):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if _math.isnan(f) or _math.isinf(f):
            return None
        return round(f, d)

    if plotly_cdn:
        pljs = (
            f'<script src="{PLOTLY_CDN}" charset="utf-8" '
            'onerror="this.onerror=null;'
            "var s=document.createElement('script');"
            f"s.src='{PLOTLY_CDN_FALLBACK}';"
            "s.charset='utf-8';document.head.appendChild(s);\"></script>"
        )
    else:
        try:
            import plotly.offline as pyo
            plotly_js_src = pyo.get_plotlyjs()
            pljs = '<script>' + plotly_js_src + '</script>'
        except Exception as e:
            print(f'⚠️ plotly.js 內嵌失敗，改用 CDN：{e}')
            pljs = f'<script src="{PLOTLY_CDN}"></script>'

    keep_rows = int(chart_years * 252) + 130 if chart_years else 0


    stock_infos  = []

    extra_list = list((extra_charts or {}).values()) if isinstance(extra_charts, dict) \
        else list(extra_charts or [])
    embedded = {r['code'] for r in results}
    extra_list = [r for r in extra_list if r['code'] not in embedded]
    if not data_dir:
        extra_list = []
    else:
        import shutil as _shutil
        _shutil.rmtree(data_dir, ignore_errors=True)
        os.makedirs(data_dir, exist_ok=True)
    written = 0
    series_map = {}

    for idx, res in enumerate(list(results) + extra_list):
        s = _chart_series(res, keep_rows, sf)

        if idx >= len(results):
            with open(os.path.join(data_dir, res['code'] + '.json'),
                      'w', encoding='utf-8') as fh:
                fh.write(_json.dumps(s, ensure_ascii=False,
                                     separators=(',', ':')))
            written += 1
            continue

        series_map[s['code']] = s

        c_l = s['c']
        if len(c_l) >= 2 and c_l[-2]:
            chg     = round(c_l[-1] - c_l[-2], 2)
            chg_pct = round(chg / c_l[-2] * 100, 2)
        else:
            chg, chg_pct = 0.0, 0.0

        stock_infos.append({
            'code': res['code'],    'name': res['name'],
            'ind':  res.get('industry',''),
            'close':res['close'],   'ma20': res['ma20_last'],
            'ma60': res['ma60_last'],'stop': res['stop_loss'],
            'vratio':res['vol_ratio'],'bw': res['boll_bw_pct'],
            'atr':  res['atr14'],   'trigger': res['trigger'],
            'chg':  chg,            'chg_pct': chg_pct,
            'url':  (link_base.rstrip('/') + '/' + res['code'] + '.html')
                    if link_base else '',
        })


    n      = len(results)
    if now is None:
        now = datetime.datetime.now()
    ts     = now.strftime('%Y_%m_%d_%H%M')
    ts_display = today_str.replace('-', '/')
    info_js = _json.dumps(stock_infos, ensure_ascii=False)

    chart_divs = (
        '<div class="badge-row" id="tf-badges"></div>'
        '<div class="trig-line" id="tf-trig"></div>'
        '<div class="plot" id="tf-plot"><p class="ld">&#9203; 載入中...</p></div>'
        '<div id="rb-tf">'
        '<span>時間範圍：</span>'
        '<button class="rbtn"     onclick="setRange(1,0,this)">1月</button>'
        '<button class="rbtn rba" onclick="setRange(3,0,this)">3月</button>'
        '<button class="rbtn"     onclick="setRange(6,0,this)">6月</button>'
        '<button class="rbtn"     onclick="setRange(0,1,this)">1年</button>'
        '<button class="rbtn"     onclick="setRange(0,{y},this)">{y}年</button>'
        '</div>'
    ).format(y=int(chart_years) if chart_years else 2)

    css = (
        '*{box-sizing:border-box;margin:0;padding:0}'
        'body{background:#0d1117;color:#c9d1d9;'
            'font-family:"Microsoft JhengHei",Arial,sans-serif;overflow-x:hidden}'
        '#hd{background:#161b22;border-bottom:1px solid #30363d;'
            'padding:8px 16px 10px;display:flex;flex-direction:column;gap:2px}'
        '.meta{font-size:12px;color:#8b949e}'
        '.tip{font-size:12px;color:#f0c27f;margin-left:auto}'
        'html,body{height:100%;overflow:hidden}'
        ':root{--hd-h:46px}'
        '#topbar{position:sticky;top:0;z-index:30;background:#0d1117;'
            'max-height:66vh;overflow-y:auto;'
            'box-shadow:0 2px 8px rgba(0,0,0,0.35)}'
        '#main{display:flex;align-items:stretch;'
            'height:calc(100vh - var(--hd-h, 46px))}'
        '#sidebar{width:300px;flex:0 0 300px;background:#0d1117;'
            'border-right:1px solid #30363d;height:100%;overflow-y:auto}'
        '#sb-list{padding:6px}'
        '#chartcol{flex:1 1 auto;min-width:0;height:100%;'
            'display:flex;flex-direction:column;overflow-y:auto}'
        '.nb{background:#161b22;border:1px solid #30363d;'
            'color:#c9d1d9;cursor:pointer;padding:10px 12px;margin:5px 4px;'
            'border-radius:8px;font-family:inherit;'
            'transition:background .15s,border-color .15s}'
        '.nb:hover{background:#1c2530;border-color:#388bfd}'
        '.nb.act{background:#132a45;border-color:#58a6ff}'
        '.nb-top{display:flex;justify-content:space-between;align-items:baseline}'
        '.nb-code{font-size:16px;font-weight:bold;color:#e6edf3}'
        '.nb-close{font-size:16px;font-weight:bold;color:#e6edf3}'
        '.nb-px{display:inline-flex;align-items:center;gap:5px}'
        '.nb-yf{display:inline-flex;align-items:center;justify-content:center;width:18px;'
            'height:18px;border-radius:4px;border:1px solid #30363d;background:#0d1117;'
            'color:#58a6ff;text-decoration:none;flex:0 0 auto}'
        '.nb-yf:hover{border-color:#58a6ff;background:#161b22}'
        '.nb-yf::before{content:"";width:13px;height:13px;background:currentColor;'
            '-webkit-mask:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27 fill=%27none%27 stroke=%27%23000%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27M1.5 14.5h13%27 stroke-width=%271.3%27 opacity=%27.55%27/%3E%3Cpath d=%27M2 11.5l3.6-4 2.8 2.6L14 3.6M10.4 3.4H14v3.6%27 stroke-width=%271.7%27/%3E%3C/svg%3E") center/contain no-repeat;'
            'mask:url("data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 16 16%27 fill=%27none%27 stroke=%27%23000%27 stroke-linecap=%27round%27 stroke-linejoin=%27round%27%3E%3Cpath d=%27M1.5 14.5h13%27 stroke-width=%271.3%27 opacity=%27.55%27/%3E%3Cpath d=%27M2 11.5l3.6-4 2.8 2.6L14 3.6M10.4 3.4H14v3.6%27 stroke-width=%271.7%27/%3E%3C/svg%3E") center/contain no-repeat}'
        '.nb-close.up{color:#f85149}'
        '.nb-close.dn{color:#3fb950}'
        '.nb-bot{display:flex;justify-content:space-between;align-items:baseline;'
            'margin-top:2px;font-size:12px;color:#8b949e}'
        '.nb-pct{font-weight:bold}'
        '.nb-pct.up{color:#f85149}'
        '.nb-pct.dn{color:#3fb950}'
        '.nb-pct.fl{color:#8b949e}'
        '.nb-ind{font-size:11px;color:#79c0ff;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
        '.nb-cross{display:flex;gap:6px;margin-top:7px;font-variant-numeric:tabular-nums}'
        '.nb-lnk{flex:1 1 0;min-width:0;display:inline-flex;align-items:baseline;'
            'justify-content:center;gap:3px;padding:3px 8px;border-radius:6px;'
            'font-size:11px;line-height:1.35;white-space:nowrap;text-decoration:none;'
            'color:#79c0ff;background:rgba(88,166,255,.10);'
            'border:1px solid rgba(88,166,255,.34)}'
        '.nb-lnk .k{color:#8b949e}'
        '.nb-lnk b{color:#e6edf3;font-weight:600}'
        '.nb-lnk i{font-style:normal;color:#58a6ff;font-size:10px}'
        'a.nb-lnk:hover{background:rgba(88,166,255,.24);border-color:#58a6ff}'
        'span.nb-lnk{cursor:default}'
        '.nbshort{opacity:.72}'
        '.nb-why{font-size:10px;color:#d9a441;margin-top:3px;white-space:normal;line-height:1.35}'
        '.sb-sep{margin:14px 6px 6px;padding-top:10px;border-top:1px solid #30363d;'
            'color:#8b949e;font-size:11px}'
        '.nb-tagrow{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin-top:6px}'
        '.nb-tag{display:inline-block;flex:0 0 auto;padding:2px 8px;'
            'border-radius:11px;font-size:10.5px;background:rgba(240,194,127,.14);'
            'color:#f0c27f;border:1px solid rgba(240,194,127,.32);white-space:nowrap}'
        'a.badge.link{text-decoration:none;background:rgba(88,166,255,.16);'
            'color:#79c0ff;border-color:rgba(88,166,255,.42)}'
        'a.badge.link:hover{background:rgba(88,166,255,.30);border-color:#58a6ff}'
        '#live{margin-top:6px}'
        '#live .live-fields{display:flex;flex-wrap:wrap;gap:6px 10px}'
        '#live label{display:inline-flex;align-items:center;gap:4px;'
            'font-size:11px;color:#8b949e}'
        '#live input{width:74px;background:#0d1117;color:#e6edf3;'
            'border:1px solid #30363d;border-radius:5px;padding:2px 5px;'
            'font-family:inherit;font-size:11.5px}'
        '#live input:focus{outline:none;border-color:#58a6ff}'
        '#live em{font-style:normal;color:#6e7681;font-size:10.5px}'
        '#live .live-bar{display:flex;align-items:center;gap:10px;margin-top:6px;'
            'flex-wrap:wrap}'
        '#live .live-bar button{background:#21262d;color:#c9d1d9;'
            'border:1px solid #30363d;border-radius:5px;padding:3px 10px;'
            'font-family:inherit;font-size:11.5px;cursor:pointer}'
        '#live .live-bar button:hover{background:#30363d;border-color:#8b949e}'
        '.tf-fold{display:none}'
        '#live #live-count{font-size:11.5px;font-weight:600}'
        '#live #live-count.hit{color:#7ee787}'
        '#live #live-count.miss{color:#8b949e}'
        '#live #live-count.stale{color:#f0c27f}'
        '.nb.nochart{border-style:dashed;opacity:.86}'
        '.nb.nochart .nb-code{color:#8b949e}'
        '.sb-more{color:#6e7681;font-size:11px;text-align:center;padding:10px 6px}'
        '#tf-note{margin:auto;max-width:46em;padding:24px 20px;color:#8b949e;'
            'font-size:13px;line-height:1.75;text-align:center}'
        '#tf-note b{color:#e6edf3}'
        '#tf-note p{margin:8px 0 0}'
        '#tf-note a{color:#58a6ff;text-decoration:none}'
        '#tf-note a:hover{text-decoration:underline}'
        '#tf-pane{display:flex;flex-direction:column;width:100%;'
            'flex:1 0 auto;min-height:100%;padding:10px 16px 12px}'
        '#tf-pane[hidden]{display:none}'
        '.tf-pane-head{display:flex;align-items:baseline;gap:10px;'
            'font-size:13px;color:#8b949e;padding:0 0 6px}'
        '.tf-pane-head b{color:#e6edf3;font-size:15px}'
        '#tf-plot{flex:1 1 0;'
            'min-height:440px;min-height:clamp(440px,52cqw,720px)}'
        '#chartcol{container-type:inline-size}'
        '#live .live-bar button.go{background:#238636;color:#fff;'
            'border-color:#2ea043;font-weight:700}'
        '#live .live-bar button.go:hover{background:#2ea043;border-color:#3fb950}'
        '.badge-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;'
            'margin-bottom:6px;flex:0 0 auto}'
        '.badge{display:inline-flex;align-items:center;padding:5px 13px;'
            'border-radius:16px;font-size:12.5px;font-weight:600;'
            'background:rgba(88,166,255,.12);color:#58a6ff;'
            'border:1px solid rgba(88,166,255,.3);white-space:nowrap}'
        '.badge.ind{background:rgba(88,166,255,.16);color:#79c0ff}'
        '.badge.stop{background:rgba(248,81,73,.14);color:#f85149;'
            'border-color:rgba(248,81,73,.35)}'
        '.badge.gold{background:rgba(240,194,127,.14);color:#f0c27f;'
            'border-color:rgba(240,194,127,.35)}'
        '.trig-line{font-size:13px;color:#f0c27f;margin:2px 0 8px;'
            'display:flex;align-items:center;gap:6px;flex:0 0 auto}'
        '.trig-arrow{color:#ffa657}'
        '.ld{text-align:center;padding:80px;color:#8b949e;font-size:15px}'
        '.plot{width:100%;flex:1 1 auto;min-height:0;position:relative}'
        '.xh{position:absolute;top:0;bottom:0;width:1px;background:#58a6ff;'
            'pointer-events:none;display:none;z-index:5}'
        '[id^="rb-"]{display:flex;align-items:center;gap:6px;flex-wrap:wrap;'
            'justify-content:center;margin-top:8px;flex:0 0 auto}'
        '[id^="rb-"] span{font-size:11px;color:#8b949e;margin-right:4px}'
        '.rbtn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;'
            'cursor:pointer;padding:4px 13px;border-radius:16px;'
            'font-family:inherit;font-size:11px;transition:background .12s}'
        '.rbtn:hover{background:#30363d;border-color:#58a6ff}'
        '.rbtn.rba{background:#1f6feb;border-color:#388bfd;color:#fff}'
        '@media(max-width:760px){'
            'html,body{overflow:visible;height:auto}'
            ':root{--hd-h:auto}'
            '#hd{flex-wrap:wrap;gap:6px;padding:8px 10px}'
            '#hd .meta{font-size:11px}'
            '#topbar{position:static;max-height:none;overflow:visible}'
            '#main{flex-direction:column;height:auto}'
            '#sidebar{width:100%;flex:0 0 auto;height:auto;max-height:none;'
                'border-right:none;border-bottom:1px solid #30363d;'
                'overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch}'
            '#sb-list{display:flex;gap:6px;padding:6px 8px;width:max-content}'
            '.nb{flex:0 0 auto;width:132px;margin:0;padding:7px 9px}'
            '.nb-code{font-size:14px}'
            '.nb-close{font-size:13px}'
            '.nb-bot{font-size:11px;margin-top:1px}'
            '.nb-tagrow{display:none}'
            '.nb-ind{font-size:10px;margin-top:2px}'
            '.nb-cross{flex-direction:column;gap:4px;margin-top:5px}'
            '.nb-lnk{flex:0 0 auto;justify-content:flex-start;min-height:26px;'
                'align-items:center;padding:2px 7px;font-size:10.5px}'
            '.nb-lnk b{margin-left:auto}'
            '.nb-why{font-size:9.5px}'
            '.sb-sep{flex:0 0 auto;min-width:92px;max-width:92px;margin:0 2px;'
                'padding:6px 8px 6px 10px;border-top:none;border-left:1px solid #30363d;'
                'white-space:normal;line-height:1.4;display:flex;align-items:center}'
            '#chartcol{flex:1 1 auto;height:auto;min-height:0;overflow:visible}'
            '.plot{flex:0 0 auto;height:clamp(320px,118vw,520px)}'
            '#tf-plot{flex:0 0 auto;min-height:0}'
            '.tf-fold{display:inline-flex;align-items:center}'
            '#live.folded .live-fields{display:none}'
            '#live .live-bar{flex-wrap:wrap;gap:8px;margin-top:0}'
            '#live .live-bar button{min-height:34px;padding:6px 14px}'
            '.badge-row{flex-wrap:nowrap;gap:4px;overflow-x:auto;'
                'scrollbar-width:none;-webkit-overflow-scrolling:touch}'
            '.badge-row::-webkit-scrollbar{display:none}'
            '.badge{flex:0 0 auto;font-size:10.5px;padding:4px 6px}'
            '#tf-pane{padding:10px 10px 12px}'
            '.trig-line{font-size:12px;margin:2px 0 6px}'
            '.rbtn{padding:6px 14px;font-size:12px}'
        '}'
    )

    js = r"""
const INFO = INFO_JS_PLACEHOLDER;
/* 手機：390px 寬的螢幕上，那排工具列會吃掉圖表頂端一整條，而它上面每一顆
   （框選縮放、自動縮放、存圖）在觸控上不是做不到就是有更自然的手勢。捏合縮放
   和拖曳平移不需要它。 */
const NARROW = window.matchMedia('(max-width:760px)').matches;
const PLY_CFG = {
  displayModeBar: !NARROW, displaylogo: false, scrollZoom: true,
  responsive: false,
  modeBarButtonsToRemove: ['lasso2d','select2d'],
  toImageButtonOptions: {
    format:'png', filename:'twstock_TS_PH', height:760, width:1400, scale:2
  }
};

/* 手機上的圖例。
   375px 寬的螢幕上，七項會折成四行、量出來 87px 高——而上留白只有 72px，
   所以它壓在 K 線圖最上面那一段上。把字縮小沒有用（實測還是 87px：plotly
   的每一列有最小高度），要減的是**項數**。

   留下帶數值的三個（20MA／60MA／建議停損），其餘四個拿掉：K 棒長得就像 K 棒、
   成交量在自己那一格、均量是量圖上唯一一條線、布林上下軌是圖上唯二的虛線。
   它們在桌機上值得標，在手機上佔的位置比它們講的事情貴。

   減成三項之後量出來 49px、底端在 60px，所以上留白給 68px 就一定不重疊。
   這幾個數字是在 375×812 的實機版面上量的，不是估的。 */
function tuneForNarrow(fig) {
  if (!NARROW) return fig;
  fig.layout = fig.layout || {};
  (fig.data || []).forEach(function(t) {
    if (t.showlegend && !/20MA|60MA|建議停損/.test(t.name || '')) {
      t.showlegend = false;
    }
    /* 線細一點。
       桌機上價格那一格有四百多像素高，2.0 的線是一條線；手機上同一格只有
       一百多像素，同樣粗細的兩條均線加上停損線、布林上下軌，五條線疊起來
       會把 K 棒蓋掉——「解析度不足」看起來就是這樣來的，而它其實是線太粗。 */
    if (t.line && typeof t.line.width === 'number' && t.line.width > 1.2) {
      t.line = Object.assign({}, t.line, {width: 1.2});
    }
  });
  fig.layout.legend = Object.assign({}, fig.layout.legend, {font: {size: 10}});
  fig.layout.margin = Object.assign({}, fig.layout.margin,
                                    {t: 62, l: 36, r: 8, b: 22});
  fig.layout.font = Object.assign({}, fig.layout.font, {size: 10});
  /* 刻度數。
     plotly 是按**像素**決定要標幾個刻度的，而它算的是「桌機上這麼寬要標幾個」。
     手機上同一條軸只有三百出頭的寬度、價格那一格只有兩百多的高度，於是它標出
     來的刻度會互相壓字——實測縱軸的「20」和「50」直接疊成一團（使用者那張截圖
     上就是這個）。
     四個日期、五個價位、三個量的刻度，是在 366px 寬上量出來不重疊的組合。 */
  fig.layout.xaxis = Object.assign({}, fig.layout.xaxis, {nticks: 4});
  fig.layout.xaxis2 = Object.assign({}, fig.layout.xaxis2, {nticks: 4});
  fig.layout.yaxis = Object.assign({}, fig.layout.yaxis,
                                   {title: {text: ''}, nticks: 5});
  fig.layout.yaxis2 = Object.assign({}, fig.layout.yaxis2,
                                    {title: {text: ''}, nticks: 3});
  /* 兩個縱軸標題拿掉。
     它們是**直排**的，各佔 43px 與 51px 的高度，而手機上兩個子圖加起來只有
     一百多像素——於是「價格 (元)」的下緣和「成交量(張)」的上緣疊在一起
     （實測 464–507 對 506–557，真的重疊）。
     拿掉之後左留白從 46 縮到 36，繪圖區橫向多出 10px。
     單位沒有消失：刻度是 55/50/45 和 4000/2000，而圖例上就寫著 20MA、60MA。 */
  return fig;
}

/* ── 依實際 #hd 高度設定 CSS 變數，讓 #main 精準填滿剩餘視窗高度 ── */
function syncHdHeight() {
  var hd = document.getElementById('topbar');
  var h = hd ? hd.offsetHeight : 0;
  document.documentElement.style.setProperty('--hd-h', h + 'px');
}

/* ── 讓圖表填滿 .plot 容器的實際像素高度（而非寫死的 760）────

   尺寸沒變就不要 relayout。一次 relayout 會把五百根 K 棒整個重畫一遍
   （candlestick 的每一根都是一條 path），而切換股票時視窗尺寸根本沒變——
   那是這張圖最貴的一次白工。 */
function fitPlotSize(div) {
  if (!div) return;
  var w = div.clientWidth, h = div.clientHeight;
  if (!(w > 0 && h > 0)) return;
  if (div._twW === w && div._twH === h) return;
  div._twW = w; div._twH = h;
  Plotly.relayout(div, {width: w, height: h});
}

/* 容器一變大小就重新量一次。
   ## 為什麼只掛 window.resize 不夠
   這件事原本只掛在 `window.resize` 上，而**視窗大小沒變**的那些時刻才是問題：
   字型載入完、側欄那排卡片畫出來、外層網站按下「切換手機版」（那會把 iframe
   的寬度釘成 430px）——每一次容器的高度都變了，視窗卻一次都沒有 resize。
   於是圖表停在它第一次被量到的那個尺寸，而 `_twW/_twH` 那個快取讓它**永久**
   停在那裡。
   ## 實測（2026-09-20 那份報告）
       桌機 1400×900   容器 659px，圖 755px  → 高出 96px，蓋住下面那排〔時間範圍〕
       手機  390×844   容器 405px，圖 181px  → 只用了 45%，價格那一格剩一百出頭
   同一個原因，兩個相反的症狀：使用者說的「圖蓋到時間範圍」和「手機上看不清楚」
   是同一件事。
   ResizeObserver 只在**真的**變了的時候才響，而 `fitPlotSize` 自己還有一層
   尺寸相同就跳過的守門，所以不會多畫一次那五百根 K 棒。 */
function observePlotSize(div) {
  if (!div || div._twObs || typeof ResizeObserver === 'undefined') return;
  div._twObs = new ResizeObserver(function () { fitPlotSize(div); });
  div._twObs.observe(div);
}

/* ── 台灣時間（UTC+8）─────────────────────────────────────── */
function twNow() {
  var now = new Date();
  /* 加上 UTC+8 偏移，再修正本機時區偏移，得到台灣當地時間 */
  return new Date(now.getTime() + (8 * 60 + now.getTimezoneOffset()) * 60000);
}
function toISO(d) { return d.toISOString().slice(0, 10); }

/* ── 設定時間範圍（台灣時間往回推）──────────────────────── */
function setRange(months, years, btn) {
  var rbWrap = document.getElementById('rb-tf');
  if (rbWrap) rbWrap.querySelectorAll('.rbtn').forEach(function(b){ b.classList.remove('rba'); });
  if (btn) btn.classList.add('rba');

  /* 圖還沒畫出來的時候按這幾顆是沒有意義的（`_twBound` 是「已經畫過一次」的
     記號）。硬 relayout 一個空的 div，plotly 會丟例外出來。 */
  var wrap = document.getElementById('tf-plot');
  if (!wrap || !wrap._twBound) return;

  var end   = twNow();
  var start = new Date(end);
  if (months) start.setMonth(start.getMonth() - months);
  if (years)  start.setFullYear(start.getFullYear() - years);

  /* 只設定 X 軸範圍；Y 軸交由 attachAutoY 的 relayout 監聽器，
     依「目前可視範圍內的實際最高/最低值」自動計算最佳上下界，避免留白過多 */
  Plotly.relayout(wrap, {
    /* 主圖 xaxis.matches='x2'，故 range 必須設在 xaxis2 才會真正生效並同步 */
    'xaxis2.range[0]': toISO(start),
    'xaxis2.range[1]': toISO(end)
  });
}

/* ── 從一份數列組出一張圖 ─────────────────────────────────────
   這一頁上**每一張**圖都從這裡出來：今天過篩那幾檔（數列嵌在 tf-series 裡），
   和調完門檻才多出來的那些（數列從 Pages 抓回來）。同一條路，所以不會有
   「有些股票的圖長得不一樣」這種事。

   樣式完全不在這裡。`tf-figtpl` 是 Python 的 `_chart_figure()` 用一份空資料跑
   出來的樣板——兩百行的軌道顏色、停損線樣式、圖例位置、上下子圖高度比都已經
   畫好了，這裡只把數字填進對應的 trace。要改樣式就改 Python 那一支，兩種圖
   一起改。

   trace 的順序是和 `_chart_figure` 約好的（① 布林上軌 …… ⑪ 量圖 hover 軌）。
   約定會走鐘，所以 tests/test_chart_js.py 拿同一份 series 讓 Python 和這裡各
   組一張，逐一比對——順序錯了那條測試就紅。 */
var TF_TPL = null;
function tfTemplate() {
  if (TF_TPL) return TF_TPL;
  var el = document.getElementById('tf-figtpl');
  if (!el) { console.error('找不到 tf-figtpl'); return null; }
  try { TF_TPL = JSON.parse(el.textContent); }
  catch (e) { console.error('圖表樣板解析失敗', e); return null; }
  return TF_TPL;
}

var TF_SER = null;
function tfSeriesOf(code) {
  if (TF_SER === null) {
    var el = document.getElementById('tf-series');
    try { TF_SER = el ? JSON.parse(el.textContent) : {}; }
    catch (e) { console.error('數列解析失敗', e); TF_SER = {}; }
  }
  return TF_SER[code] || null;
}

/* 近三個月那個預設視窗的 Y 軸範圍。和 Python 的 `_chart_window` 同一條公式——
   樣板裡那組 range 是拿空資料算出來的，填了數列之後一定要重算，否則每一張圖的
   Y 軸都停在 0 附近，畫面上是一條貼著底的平線。 */
function tfWindow(s) {
  var N = s.d.length, from = Math.max(0, N - 63), i;
  var hi = [], lo = [], vv = [];
  for (i = from; i < N; i++) {
    if (s.h[i] !== null) hi.push(s.h[i]);
    if (s.bu[i] !== null) hi.push(s.bu[i]);
    if (s.l[i] !== null) lo.push(s.l[i]);
    if (s.bd[i] !== null) lo.push(s.bd[i]);
    if (s.vl[i] !== null) vv.push(s.vl[i]);
  }
  if (s.stop !== null && s.stop !== undefined) lo.push(s.stop);
  var winHi = hi.length ? Math.max.apply(null, hi) : 1.0;
  var winLo = lo.length ? Math.min.apply(null, lo) : 0.0;
  var pad = (winHi - winLo) * 0.06;
  if (!pad) pad = (Math.abs(winHi) || 1.0) * 0.05;
  var vhi = (vv.length ? Math.max.apply(null, vv) : 1.0) * 1.12;
  return { lo: winLo - pad, hi: winHi + pad, vhi: vhi };
}

function tfFix(v) { return (v === null || v === undefined) ? '0.00' : v.toFixed(2); }

function tfSeriesFig(s) {
  var tpl = tfTemplate();
  if (!tpl || !s || !s.d || !s.d.length) return null;
  /* 深拷貝：樣板只解析一次，所有股票共用它。直接改的話第二檔會蓋掉第一檔的
     資料，而畫面上看起來只是「切過去圖沒變」。 */
  var fig = JSON.parse(JSON.stringify(tpl));
  var d = s.d, N = d.length, t = fig.data, i;

  var cd = new Array(N), cdv = new Array(N), vcol = new Array(N);
  for (i = 0; i < N; i++) {
    cd[i] = [d[i], s.o[i], s.h[i], s.l[i], s.c[i],
             s.m20[i], s.m60[i], s.bu[i], s.bm[i], s.bd[i],
             s.vl[i], s.vm[i], s.vr[i]];
    cdv[i] = [d[i], s.vl[i], s.vm[i], s.vr[i]];
    vcol[i] = ((s.c[i] || 0) >= (s.o[i] || 0)) ? '#3fb950' : '#f85149';
  }

  t[0].x = d; t[0].y = s.bu;                     /* ① 布林上軌 */
  t[1].x = d; t[1].y = s.bd;                     /* ② 布林下軌 */
  t[2].x = d; t[2].y = s.bm;                     /* ③ 布林中軌 */
  t[3].x = d;                                    /* ④ K 棒 */
  t[3].open = s.o; t[3].high = s.h; t[3].low = s.l; t[3].close = s.c;
  t[4].x = d; t[4].y = s.m20;                    /* ⑤ 20MA */
  t[4].name = '20MA 月線 ' + tfFix(s.ma20_last);
  t[5].x = d; t[5].y = s.m60;                    /* ⑥ 60MA */
  t[5].name = '60MA 季線 ' + tfFix(s.ma60_last);
  t[6].x = [d[0], d[N - 1]];                     /* ⑦ 停損線 */
  t[6].y = [s.stop, s.stop];
  t[6].name = '建議停損 ' + tfFix(s.stop);
  t[7].x = d; t[7].y = s.vl;                     /* ⑧ 成交量 */
  t[7].marker = t[7].marker || {};
  t[7].marker.color = vcol;
  t[8].x = d; t[8].y = s.vm;                     /* ⑨ 20日均量 */
  t[9].x = d; t[9].y = s.c; t[9].customdata = cd;      /* ⑩ 價格 hover 軌 */
  t[10].x = d; t[10].y = s.vl; t[10].customdata = cdv; /* ⑪ 量圖 hover 軌 */

  var w = tfWindow(s);
  fig.layout.xaxis2 = fig.layout.xaxis2 || {};
  fig.layout.xaxis2.range = [d[Math.max(0, N - 63)], d[N - 1]];
  fig.layout.yaxis = fig.layout.yaxis || {};
  fig.layout.yaxis.range = [w.lo, w.hi];
  fig.layout.yaxis.autorange = false;
  fig.layout.yaxis2 = fig.layout.yaxis2 || {};
  fig.layout.yaxis2.range = [0, w.vhi];
  fig.layout.yaxis2.autorange = false;
  return fig;
}



/* ── 圖表上方那排徽章與觸發條件 ─────────────────────────────

   以代號為鍵，不是以索引。索引那一版只認得「排程當天過篩的那幾檔」——調寬門檻
   多出來的股票沒有索引，所以它們的圖上面什麼都沒有。

   量比／布林寬／ATR 三個數字每一檔都有（它們在快照裡，而快照是全市場的），
   所以三個徽章對每一檔都畫得出來。`INFO` 只有排程那幾檔，拿它當補充：有就用
   它的（那是當天收盤算的），沒有就用快照的。 */
function tfBadges(code) {
  var badges = document.getElementById('tf-badges');
  var trig = document.getElementById('tf-trig');
  if (!badges) return;
  var hit = (typeof TF_HITS !== 'undefined')
    ? TF_HITS.find(function (h) { return h[0][C.code] === code; }) : null;
  var row = hit ? hit[0] : null;
  var info = null, i;
  for (i = 0; i < INFO.length; i++) { if (INFO[i].code === code) { info = INFO[i]; break; } }

  var vratio = info ? info.vratio : (row ? row[C.vol_ratio] : null);
  var atr    = info ? info.atr    : (row ? row[C.atr14] : null);
  /* 布林寬：INFO 存的是百分比，快照存的是一整串比例（壓縮那一關要看一段時間，
     不是只看今天），所以取最後一個再乘一百。單位弄混的話畫面上會出現
     「布林寬 0.1%」這種看起來像真的、其實差一百倍的數字。 */
  var bw = info ? info.bw
         : (row && row[C.bw] && row[C.bw].length
            ? row[C.bw][row[C.bw].length - 1] * 100 : null);
  var url = info ? info.url : (TF_LINK ? TF_LINK + '/' + code + '.html' : '');

  var html = '';
  if (vratio !== null && vratio !== undefined)
    html += '<span class="badge">量比 ' + vratio.toFixed(2) + 'x</span>';
  if (bw !== null && bw !== undefined)
    html += '<span class="badge">布林寬 ' + bw.toFixed(1) + '%</span>';
  if (atr !== null && atr !== undefined)
    html += '<span class="badge gold">ATR ' + atr.toFixed(2) + '</span>';
  /* 技術面看完，下一個問題是這家公司的體質——那個答案在隔壁那個網站上。
     target="_blank"：這一頁的圖表是有狀態的（選了哪一檔、拉到哪個範圍），
     在原地跳走會把那些全部丟掉。

     字是「6168詳細資訊↗」，不是「六大財務指標評等 6168↗」：它開的是整份個股
     頁（十個分頁），不只是六大那一頁——六大那一頁由卡片底下那顆〔六大〕直達。
     短一點也才放得進手機上量比／布林寬／ATR 那一列（見手機版的 .badge-row）。 */
  if (url)
    html += '<a class="badge link" href="' + url + '" target="_blank" rel="noopener"' +
            ' title="開啟 ' + tfEsc(code) + ' 的個股頁">' +
            tfEsc(code) + '詳細資訊↗</a>';
  badges.innerHTML = html;

  /* 觸發條件用**現在這組門檻**算出來的那幾個（tfPass 的回傳值），不是排程當天
     那一份。門檻是使用者剛剛自己調的，這一行要說的是「它為什麼出現在這組條件
     下」——拿排程當天的理由去回答那個問題是答非所問。 */
  if (trig) {
    var txt = hit ? hit[1].join('｜') : (info ? info.trigger : '');
    trig.innerHTML = txt
      ? '<span class="trig-arrow">▶︎</span> ' + tfEsc(txt) : '';
  }
}

/* ── 貫穿上下兩圖的十字線 ───────────────────────────────────

   原本這條線是一個 plotly 的 shape，每移到新的一天就 Plotly.relayout 一次。
   而一次 relayout 會把整張圖重畫——五百根 K 棒，每一根都是一條 path。滑鼠
   從清單移到圖上、或橫著掃過去，一秒鐘可以跨過三、四十天，那就是三、四十
   次整張圖重畫。

   這也正是「手機很順、桌機很卡」的原因：手機沒有 hover。

   改成一個絕對定位的 div 蓋在圖上，移動它只是改一個 CSS 的 left。plotly
   完全不參與，而且因為它是蓋在整個 .plot 容器上，它本來就貫穿上下兩張圖，
   比原本用 paper 座標的 shape 還準。 */
function xhairEl(div) {
  var el = div._twXH;
  if (!el || !el.parentNode) {
    el = document.createElement('div');
    el.className = 'xh';
    if (!div.style.position) div.style.position = 'relative';
    div.appendChild(el);
    div._twXH = el;
  }
  return el;
}
function drawXLine(div, pt, ev) {
  var px = null;
  /* 先問 plotly 這個資料點落在哪個像素——這樣線會**貼齊那一天**，
     而不是貼齊游標。取不到（plotly 換版動了內部 API）就退回用游標位置：
     差幾個像素，總比整條線消失好。 */
  try {
    var ax = pt.xaxis;
    if (ax && typeof ax.d2p === 'function') px = ax.d2p(pt.x) + (ax._offset || 0);
  } catch (e) { px = null; }
  if (px === null || isNaN(px)) {
    if (!ev) { return; }
    px = ev.clientX - div.getBoundingClientRect().left;
  }
  var el = xhairEl(div);
  el.style.left = Math.round(px) + 'px';
  el.style.display = 'block';
}
function clearXLine(div) {
  if (div._twXH) div._twXH.style.display = 'none';
}

/* ── Hover：繪製貫穿十字線 ─────────────────────────────────── */
function attachHover(div) {
  div.on('plotly_hover', function(data) {
    var pts = data.points || [];
    var pp = null;
    for (var k = 0; k < pts.length; k++) {
      if (pts[k].customdata && pts[k].customdata.length >= 10) { pp = pts[k]; break; }
    }
    if (!pp) return;
    drawXLine(div, pp, data.event);
  });
  div.on('plotly_unhover', function() { clearXLine(div); });
}

/* ── 依目前可視範圍內的實際數值，計算最佳 Y 軸上下界 ─────── */
function padRange(lo, hi, ratio) {
  if (lo === hi) {
    var eps = (Math.abs(lo) || 1) * 0.05;
    return [lo - eps, hi + eps];
  }
  var pad = (hi - lo) * ratio;
  return [lo - pad, hi + pad];
}
function computeVisibleYRange(div, xStart, xEnd) {
  var loP = Infinity, hiP = -Infinity, loV = Infinity, hiV = -Infinity;
  var t0 = xStart.getTime(), t1 = xEnd.getTime();
  (div.data || []).forEach(function(tr) {
    if (tr.name === '__hover_price__' || tr.name === '__hover_vol__') return;
    var xs = tr.x || [];
    var isVol = (tr.yaxis === 'y2');
    /* 日期字串 → 毫秒，每條線只算一次就存起來。原本每次縮放都要把
       五百個 "YYYY-MM-DD" 重新 parse 一遍，乘上八條線就是四千次；
       而那五百個日期從頭到尾都是同一批。 */
    var ms = tr._twMs;
    if (!ms || ms.length !== xs.length) {
      ms = new Array(xs.length);
      for (var q = 0; q < xs.length; q++) ms[q] = new Date(xs[q]).getTime();
      tr._twMs = ms;
    }
    for (var i = 0; i < xs.length; i++) {
      if (ms[i] < t0 || ms[i] > t1) continue;
      var vals;
      if (tr.type === 'candlestick') vals = [tr.high[i], tr.low[i]];
      else if (tr.y) vals = [tr.y[i]];
      else continue;
      for (var j = 0; j < vals.length; j++) {
        var v = vals[j];
        if (v === null || v === undefined || isNaN(v)) continue;
        if (isVol) { if (v < loV) loV = v; if (v > hiV) hiV = v; }
        else       { if (v < loP) loP = v; if (v > hiP) hiP = v; }
      }
    }
  });
  return {
    y1: (isFinite(loP) && isFinite(hiP)) ? padRange(loP, hiP, 0.06) : null,
    /* 量圖固定以 0 為底，只在頂端留一點空間，符合成交量圖慣例 */
    y2: (isFinite(loV) && isFinite(hiV)) ? [0, hiV * 1.12] : null
  };
}

/* ── 縮放/拖曳後自動調整 Y 軸範圍（依目前可視範圍內的資料自動縮放）───
   用 setTimeout 延後到下一個 tick 才真正套用，並用世代計數器捨棄過期
   的計算結果，避免在拖曳/縮放手勢「進行中」對圖表下達 relayout 而
   造成畫面彈回原位。 ────────────────────────────────────────── */
var autoYGen = 0;
function attachAutoY(div) {
  div.on('plotly_relayout', function(ev) {
    var r0 = ev['xaxis2.range[0]'] !== undefined ? ev['xaxis2.range[0]'] : ev['xaxis.range[0]'];
    var r1 = ev['xaxis2.range[1]'] !== undefined ? ev['xaxis2.range[1]'] : ev['xaxis.range[1]'];
    var xAutorange = (ev['xaxis2.autorange'] === true || ev['xaxis.autorange'] === true);
    if (r0 === undefined && r1 === undefined && !xAutorange) return;

    var xStart, xEnd;
    if (xAutorange && (r0 === undefined || r1 === undefined)) {
      /* 雙擊重置等情況：無明確範圍，視為顯示全部資料 */
      xStart = new Date(-8640000000000000);
      xEnd   = new Date(8640000000000000);
    } else {
      xStart = new Date(r0);
      xEnd   = new Date(r1);
    }

    var myGen = ++autoYGen;
    setTimeout(function() {
      if (myGen !== autoYGen) return;   /* 期間又有新的縮放/拖曳發生，捨棄這次結果 */
      var rng = computeVisibleYRange(div, xStart, xEnd);
      var upd = {};
      if (rng.y1) { upd['yaxis.range']  = rng.y1;  upd['yaxis.autorange']  = false; }
      if (rng.y2) { upd['yaxis2.range'] = rng.y2;  upd['yaxis2.autorange'] = false; }
      if (Object.keys(upd).length) Plotly.relayout(div, upd);
    }, 0);
  });
}

/* ── 把一份數列畫到那唯一的圖表區 ───────────────────────────

   `Plotly.react` 而不是 `newPlot`：同一格連續換好幾檔的時候，react 沿用已經在
   那裡的畫布只更新資料；newPlot 每次都把整張圖拆掉重蓋，而六百根 K 棒每一根都
   是一條 path，那是整張圖最貴的部分。

   十字線與自動 Y 軸的監聽器只掛一次（`_twBound`）——react 不會換掉那個 div，
   所以掛過的還在；每次都掛的話同一個事件會被處理好幾遍。 */
function tfDraw(code, s) {
  var wrap = document.getElementById('tf-plot');
  if (!wrap) return;
  if (typeof Plotly === 'undefined') {
    wrap.innerHTML =
      '<p class="ld" style="color:#f85149">⚠ 圖表函式庫' +
      '沒有載入（plotly.js）。' +
      '<br><span style="color:#8b949e;font-size:13px">' +
      '可能是網路、CDN、或瀏覽器' +
      '擋掉了外部腳本。' +
      '重新整理頁面試試看。</span></p>';
    return;
  }
  var fig = tfSeriesFig(s);
  if (!fig) {
    wrap.innerHTML = '<p class="ld" style="color:#f85149">⚠ 圖表資料解析失敗</p>';
    return;
  }
  /* 預設視窗（近三個月）與它的 Y 軸範圍是 tfSeriesFig 算好的，所以這裡把尺寸也
     一起放進 layout 再畫——畫出來就是最終的樣子，不必畫完再 relayout 一次。
     原本這一步要跑三次完整 relayout（尺寸、X 範圍、Y 範圍）。 */
  tuneForNarrow(fig);
  var w0 = wrap.clientWidth, h0 = wrap.clientHeight;
  if (w0 > 0 && h0 > 0) {
    fig.layout.width = w0; fig.layout.height = h0;
    wrap._twW = w0; wrap._twH = h0;
  }
  var ph = wrap.querySelector('.ld');
  if (ph) ph.remove();          /* 清掉「載入中」佔位文字 */
  tfBadges(code);
  /* 換一檔就回到預設的三個月。那是每一張圖被算好的視窗，而上一檔拉到的範圍
     對這一檔沒有意義（兩檔的上市日期、停損位置都不一樣）。 */
  var rb = document.getElementById('rb-tf');
  if (rb) {
    var btns = rb.querySelectorAll('.rbtn');
    for (var i = 0; i < btns.length; i++) btns[i].classList.remove('rba');
    if (btns[1]) btns[1].classList.add('rba');   /* 〔3月〕 */
  }
  Plotly.react(wrap, fig.data, fig.layout, PLY_CFG)
    .then(function () {
      /* 畫完**之後**才量得到容器真正的高度。畫之前量到的是還沒排好的版面，
         而那正是圖表停在 181px（手機）或 755px（桌機）的原因。
         兩層 rAF：第一層等這一次的樣式套用，第二層等版面真的排完。 */
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { fitPlotSize(wrap); });
      });
      if (wrap._twBound) return;
      wrap._twBound = true;
      observePlotSize(wrap);
      attachHover(wrap);
      attachAutoY(wrap);
    })
    .catch(function (e) {
      wrap.innerHTML = '<p class="ld" style="color:#f85149">⚠ Plotly 繪製失敗: ' + e.message + '</p>';
    });
}

window.addEventListener('resize', function() {
  syncHdHeight();
  var a = document.getElementById('tf-plot');
  if (a) fitPlotSize(a);
});

/* `showChart(0)` 拿掉了。哪一檔先顯示現在由 tfApply() 決定——它畫完側欄之後
   會叫 tfOpen() 開第一張卡片，而那一張未必是 results[0]。
   
   兩邊都做的症狀很安靜：側欄亮著 2317，右邊畫的是 2330。這一頁的 DOMContentLoaded
   有兩個監聽器（〔調整篩選條件〕那段在文件裡比較前面，所以先跑），後跑的這一個
   會把前一個開好的圖蓋掉，但蓋不掉側欄上的那個亮框。 */
document.addEventListener('DOMContentLoaded', function() {
  syncHdHeight();
});
"""
    js = js.replace('INFO_JS_PLACEHOLDER', info_js)\
           .replace('TS_PH', ts.replace('_', ''))

    parts = [
        '<!DOCTYPE html><html lang="zh-TW"><head>',
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>\u53f0\u80a1\u9806\u52e2\u4ea4\u6613\u4e92\u52d5\u7dda\u5716 ' + ts_display + '</title>',
        '<style>' + css + '</style>',
        pljs,
        '</head><body>',
        '<div id="topbar">',
        '<div id="hd">',
        '<div class="meta">\u7be9\u9078\u65e5\u671f\uff1a' + ts_display + ' ' +
        now.strftime('%H:%M') +
        (('&nbsp;|&nbsp;\u8cc7\u6599\u57fa\u6e96\uff1a<b style="color:#f0c27f">'
          + str(asof).replace('-', '/') + '</b>') if asof and str(asof)[:10] != today_str
         else '') +
        '&nbsp;|&nbsp;\u9810\u8a2d\u9580\u6abb\u5171&nbsp;<b style="color:#3fb950">' +
        str(n) + '</b>&nbsp;\u6a94\u901a\u904e</div>',
        _live_block(rules, snapshots=snapshots,
                    drawn={s['code']: i for i, s in enumerate(stock_infos)},
                    link_base=link_base, data_base=data_base, data_ver=ts,
                    cross=cross),
        '</div>',
        '</div>',
        '<div id="main">',
        '<div id="sidebar">',
        '<div id="sb-list"></div>',
        '</div>',
        ('<div id="chartcol">'
         '<div id="tf-note" hidden></div>'
         '<div id="tf-pane" hidden>'
         '<div class="tf-pane-head"><b id="tf-plot-title"></b>'
         '<span id="tf-plot-msg"></span></div>'
         + chart_divs + '</div></div>'),
        '</div>',
        _script_json('tf-figtpl', chart_template()),
        _script_json('tf-series', series_map),
        '<script>' + js + '</script>',
        '</body></html>',
    ]

    html_path = os.path.join(output_dir, 'TW_Stock_Trend_Following_Trading_Filter_Result_V3.1_' + ts + '.html')
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(''.join(parts))
    if written:
        print(f'另外寫了 {written:,} 檔的圖表資料到 {data_dir}（點下去才抓）')
    mb = os.path.getsize(html_path) / 1024 / 1024
    print('\u4e92\u52d5\u7dda\u5716\u5132\u5b58\uff1a{} ({:.1f} MB)'.format(html_path, mb))
    return html_path


