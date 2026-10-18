import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tw_trend_filter.pipeline as pl

CODES = ['2330', '2317', '2412', '2882', '1301'] + [
    str(1400 + i) for i in range(60)
]
PROBES = tuple(f'{c}.TW' for c in CODES[:5])


def _series(days=150, start='2025-01-01'):
    closes = [100.0 + i * 0.3 for i in range(days)]
    vols = [600_000] * days
    vols[-1] = 900_000
    idx = pd.bdate_range(start, periods=days)
    return pd.DataFrame(
        {'Open': [c * 0.995 for c in closes],
         'High': [c * 1.001 for c in closes],
         'Low': [c * 0.98 for c in closes],
         'Close': closes,
         'Volume': vols},
        index=idx,
    )


def _with_stub(df):
    prev = df.iloc[-1]
    stub = pd.DataFrame(
        {'Open': [float(prev['Open'])],
         'High': [float(prev['Close']) * 1.0996],
         'Low': [float(prev['Low'])],
         'Close': [float(prev['Close'])],
         'Volume': [float(df['Volume'].iloc[-21:-1].mean()) * 0.1]},
        index=[df.index[-1] + pd.Timedelta(days=2)],
    )
    return pd.concat([df, stub])




def test_認得出還沒開盤的那一根():
    assert pl._looks_like_stub(_with_stub(_series()))


def test_真的收過盤的那一根不是佔位棒():
    assert not pl._looks_like_stub(_series())


def test_量沒有縮就不算():
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('Volume')] = 600_000
    assert not pl._looks_like_stub(df)


def test_收盤不一樣就不算():
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('Close')] += 0.5
    assert not pl._looks_like_stub(df)


def test_開盤不一樣就不算():
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('Open')] += 0.5
    assert not pl._looks_like_stub(df)


def test_最高最低不拿來判():
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('High')] = 1e6
    df.iloc[-1, df.columns.get_loc('Low')] = 0.01
    assert pl._looks_like_stub(df), "判定被最高最低影響了"


def test_資料太短就不判():
    assert not pl._looks_like_stub(_series(days=10))
    assert not pl._looks_like_stub(None)




def test_多數說沒開盤就算沒開盤():
    frames = [_with_stub(_series())] * 3 + [_series()] * 2
    assert pl.stub_vote(frames)


def test_少數說沒開盤不算():
    frames = [_with_stub(_series())] + [_series()] * 4
    assert not pl.stub_vote(frames)


def test_一檔都沒問到就不要亂動資料():
    assert not pl.stub_vote([])
    assert not pl.stub_vote([None, pd.DataFrame()])




def _run(tmp_path, monkeypatch, download, probes=PROBES):
    monkeypatch.setattr(pl, 'load_tw_stock_universe', lambda *a, **k: (
        [f'{c}.TW' for c in CODES],
        {f'{c}.TW': f'測試{c}' for c in CODES},
        {c: '測試業' for c in CODES},
    ))
    monkeypatch.setattr(pl.yf, 'download', download)
    return pl.run(str(tmp_path), workers=2, retry_rounds=(),
                  session_probes=probes,
                  breaker=pl.RateLimitBreaker(sleep=lambda s: None),
                  open_when_done=False, plotly_cdn=False)


def test_還沒開盤的那一天要算到前一個交易日(tmp_path, monkeypatch):
    base = _series()
    res = _run(tmp_path, monkeypatch, lambda *a, **k: _with_stub(base))
    assert res['asof'] == f'{base.index[-1]:%Y-%m-%d}', (
        f"資料基準是 {res['asof']}，應該是最後一個真的收過盤的日子"
    )
    assert len(res['results']) == len(CODES), (
        f"砍掉佔位棒之後還是只過了 {len(res['results'])}/{len(CODES)} 檔"
    )


def test_吃到佔位棒就是一檔都篩不到(tmp_path, monkeypatch):
    base = _series()
    res = _run(tmp_path, monkeypatch, lambda *a, **k: _with_stub(base), probes=())
    assert res['asof'] is None
    assert res['results'] == [], (
        '佔位棒沒有把量比打下去——那這組測試守的東西就不是真的症狀'
    )


def test_有收過盤的日子一根都不砍(tmp_path, monkeypatch):
    base = _series()
    res = _run(tmp_path, monkeypatch, lambda *a, **k: base)
    assert res['asof'] is None, f"好好的一天卻被砍到 {res['asof']}"
    assert len(res['results']) == len(CODES)


def test_按日期砍_不是砍掉最後一根(tmp_path, monkeypatch):
    base = _series()
    lagging = '1401.TW'

    def download(ticker, *a, **k):
        return base if ticker == lagging else _with_stub(base)

    res = _run(tmp_path, monkeypatch, download)
    got = {r['code']: r for r in res['results']}
    assert lagging.split('.')[0] in got, '沒有佔位棒的那一檔被多砍了一根'
    assert got['1401']['vol_ratio'] == got['2330']['vol_ratio'], (
        '兩檔的資料一模一樣，量比卻不同——代表其中一檔被砍到不同的日子'
    )




def test_均量門檻的單位要和資料一致():
    line = dict(pl.Rules().describe())['① 基礎流動性防禦']
    assert '股' in line and '張' not in line, line
    units = {row[0]: row[2] for row in pl.LIVE_FIELDS}
    assert units['min_vol20'] == '股', units['min_vol20']




def _at(h, m=0):
    tz = pl.datetime.timezone(pl.datetime.timedelta(hours=8))
    return pl.datetime.datetime(2026, 9, 21, h, m, tzinfo=tz)


def _today_frame(days=150):
    df = _series(days=days, start='2026-02-20')
    idx = list(df.index[:-1]) + [pd.Timestamp('2026-09-21')]
    df.index = pd.DatetimeIndex(idx)
    return df


def test_盤中就是還沒結束():
    assert pl.session_unfinished([_today_frame()], now=_at(11, 30))


def test_開盤前也算還沒結束():
    assert pl.session_unfinished([_today_frame()], now=_at(8, 55))


def test_收盤之後就算結束():
    assert not pl.session_unfinished([_today_frame()], now=_at(14, 0))
    assert not pl.session_unfinished([_today_frame()], now=_at(15, 7))


def test_最後一根不是今天就不管現在幾點():
    assert not pl.session_unfinished([_series()], now=_at(10, 0))


def test_一檔都沒問到就不要亂動資料_盤中版():
    assert not pl.session_unfinished([], now=_at(11, 0))
    assert not pl.session_unfinished([None, pd.DataFrame()], now=_at(11, 0))


def test_盤中跑要砍到前一個交易日(tmp_path, monkeypatch):
    base = _today_frame()
    monkeypatch.setattr(pl, '_taipei_now', lambda: _at(11, 30))
    res = _run(tmp_path, monkeypatch, lambda *a, **k: base)
    assert res['asof'] == f'{base.index[-2]:%Y-%m-%d}', (
        f"盤中跑卻用了今天的資料（asof={res['asof']}）"
    )


def test_收盤之後跑一根都不砍(tmp_path, monkeypatch):
    base = _today_frame()
    monkeypatch.setattr(pl, '_taipei_now', lambda: _at(15, 7))
    res = _run(tmp_path, monkeypatch, lambda *a, **k: base)
    assert res['asof'] is None, f"收盤後跑卻被砍到 {res['asof']}"


def test_砍到哪一天由所有權值股一起決定(tmp_path, monkeypatch):
    full = _today_frame()
    short = full.iloc[:-1]
    first = PROBES[0]

    def download(ticker, *a, **k):
        return short if ticker == first else full

    monkeypatch.setattr(pl, '_taipei_now', lambda: _at(11, 30))
    res = _run(tmp_path, monkeypatch, download)
    assert res['asof'] == f'{full.index[-2]:%Y-%m-%d}', (
        f"被第一檔的倒數第二根帶走了（asof={res['asof']}，"
        f"應該是 {full.index[-2]:%Y-%m-%d}）"
    )


def test_算到一半丟例外的那幾檔不可以算成掃到(tmp_path, monkeypatch):
    base = _series()
    bad = {f'{c}.TW' for c in CODES[:3]}

    class Boom(pd.DataFrame):
        pass

    def download(ticker, *a, **k):
        if ticker in bad:
            df = base.copy()
            df['Close'] = df['Close'].astype(object)
            df.iloc[-1, df.columns.get_loc('Close')] = '壞掉'
            return df
        return base

    res = _run(tmp_path, monkeypatch, download)
    assert res['errors'] == len(bad), res['error_kinds']
    assert res['scanned'] == len(CODES) - len(bad), (
        f"丟例外的 {len(bad)} 檔被算成掃到了（scanned={res['scanned']}／"
        f"母體 {len(CODES)}）"
    )
    assert res['scanned'] + res['errors'] + res['too_short'] == len(CODES), (
        '記帳對不起來：scanned + errors + too_short != 母體'
    )
