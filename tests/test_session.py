"""還沒開盤的那一根「佔位棒」，會讓整份報告變成 0 檔。

## 使用者看到的症狀

> 好慘~1檔都撈不到???

## 抓到的東西（2026-09-20 星期日 21:58 台北時間，真的打 Yahoo 抓回來的）

    2330.TW
    2026-09-17  收 2425.0  開 2405.0  高 2445.0  低 2400.0  量 16,009,127
    2026-09-18  收 2460.0  開 2460.0  高 2460.0  低 2435.0  量 35,352,856
    2026-09-20  收 2460.0  開 2460.0  高 2705.0  低 2435.0  量  5,242,511  ← 星期日

八檔權值股一起看，這一根的長相一致：**收盤和開盤都和前一根一模一樣**（8/8），
量只有二十日均量的 3%~29%。而兩年的歷史裡週末的列只有這一天——它不是歷史髒
資料，是那一刻還在跳的即時報價。

## 為什麼這會讓報告變成 0 檔

第四關是「當日量 ≥ 20 日均量 × 1.2」，而這一根的量是均量的一成。
40 檔權值股實測：

    吃到佔位棒        量比中位數 0.094   ≥1.2 的 2/40   通過 0 檔
    砍到 9/18 收盤    量比中位數 1.268   ≥1.2 的 22/40  通過 2 檔

一關全滅。而且收盤停在前一天，所以第二關和第四關的突破判定，全部是拿昨天的
價格在回答今天的問題——報告上卻寫著今天的日期。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tw_trend_filter.pipeline as pl

#: 母體要夠大，空表才落在 1% 以下的常態區（見 `pipeline._mass_empty`）。
CODES = ['2330', '2317', '2412', '2882', '1301'] + [
    str(1400 + i) for i in range(60)
]
PROBES = tuple(f'{c}.TW' for c in CODES[:5])


def _series(days=150, start='2025-01-01'):
    """一檔穩穩上漲、四關都過得了的假股票。

    最後一根量放大到均量的 1.5 倍，才過得了第四關的量比 1.2——這是這一組
    測試的支點：佔位棒把它壓成 0.1，整檔就過不了。
    """
    closes = [100.0 + i * 0.3 for i in range(days)]
    vols = [600_000] * days
    vols[-1] = 900_000
    idx = pd.bdate_range(start, periods=days)
    return pd.DataFrame(
        {'Open': [c * 0.995 for c in closes],
         'High': [c * 1.02 for c in closes],
         'Low': [c * 0.98 for c in closes],
         'Close': closes,
         'Volume': vols},
        index=idx,
    )


def _with_stub(df):
    """在後面接一根「今天還沒開盤」的佔位棒，照實測抓到的長相做。

    收盤、開盤原封不動抄前一根；量壓到均量的一成；最高最低是一團亂
    （實測 2330 的最高是漲停價、最低是前一根的最低），所以刻意做得不規則
    ——判定**不可以**依賴它們。
    """
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


# ---------------------------------------------------------------------------
# 認不認得出那一根


def test_認得出還沒開盤的那一根():
    assert pl._looks_like_stub(_with_stub(_series()))


def test_真的收過盤的那一根不是佔位棒():
    assert not pl._looks_like_stub(_series())


def test_量沒有縮就不算():
    """開平收平但量是正常的——那是真的有人在交易，只是收在平盤。

    少了這一條，誤判率從 0.134% 跳到 0.812%（40 檔兩年實測）。
    """
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('Volume')] = 600_000
    assert not pl._looks_like_stub(df)


def test_收盤不一樣就不算():
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('Close')] += 0.5
    assert not pl._looks_like_stub(df)


def test_開盤不一樣就不算():
    """Yahoo 是把**前一根的開盤**原封不動搬過來的，不是拿前一根的收盤。

    只比收盤的話，一檔真的開高走低收平的股票會被誤判成沒開盤。
    """
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('Open')] += 0.5
    assert not pl._looks_like_stub(df)


def test_最高最低不拿來判():
    """第一版猜「最高最低就是漲跌停價」，實測推翻了：2330 的最低是前一根的
    最低，2412 兩邊都不是。所以把它們改成任意值，判定不可以跟著變。
    """
    df = _with_stub(_series())
    df.iloc[-1, df.columns.get_loc('High')] = 1e6
    df.iloc[-1, df.columns.get_loc('Low')] = 0.01
    assert pl._looks_like_stub(df), "判定被最高最低影響了"


def test_資料太短就不判():
    assert not pl._looks_like_stub(_series(days=10))
    assert not pl._looks_like_stub(None)


# ---------------------------------------------------------------------------
# 投票


def test_多數說沒開盤就算沒開盤():
    frames = [_with_stub(_series())] * 3 + [_series()] * 2
    assert pl.stub_vote(frames)


def test_少數說沒開盤不算():
    """單一檔的誤判率是 0.134%（18,599 個真實交易日實測），不是零。

    所以一檔說了不算——那正是「問五檔、多數決」存在的理由。
    """
    frames = [_with_stub(_series())] + [_series()] * 4
    assert not pl.stub_vote(frames)


def test_一檔都沒問到就不要亂動資料():
    """投不出來的時候寧可照舊。

    這道防線的代價是「少算一天」，而在沒有證據的情況下少算一天，比在有證據
    的情況下多算一根假的更難被發現。
    """
    assert not pl.stub_vote([])
    assert not pl.stub_vote([None, pd.DataFrame()])


# ---------------------------------------------------------------------------
# 接到 run() 上


def _run(tmp_path, monkeypatch, download, probes=PROBES):
    monkeypatch.setattr(pl, 'load_tw_stock_universe', lambda *a, **k: (
        [f'{c}.TW' for c in CODES],
        {f'{c}.TW': f'測試{c}' for c in CODES},
        {c: '測試業' for c in CODES},
    ))
    monkeypatch.setattr(pl.yf, 'download', download)
    return pl.run(str(tmp_path), make_excel=False, workers=2, retry_rounds=(),
                  session_probes=probes,
                  breaker=pl.RateLimitBreaker(sleep=lambda s: None),
                  open_when_done=False, plotly_cdn=False)


def test_還沒開盤的那一天要算到前一個交易日(tmp_path, monkeypatch):
    """這一條就是「1 檔都撈不到」的直接迴歸。"""
    base = _series()
    res = _run(tmp_path, monkeypatch, lambda *a, **k: _with_stub(base))
    assert res['asof'] == f'{base.index[-1]:%Y-%m-%d}', (
        f"資料基準是 {res['asof']}，應該是最後一個真的收過盤的日子"
    )
    assert len(res['results']) == len(CODES), (
        f"砍掉佔位棒之後還是只過了 {len(res['results'])}/{len(CODES)} 檔"
    )


def test_吃到佔位棒就是一檔都篩不到(tmp_path, monkeypatch):
    """反過來證明那一根的殺傷力：不投票（沒有權值股可問）就是 0 檔。

    沒有這一條，上面那一條可能只是在測「四關過得了」。
    """
    base = _series()
    res = _run(tmp_path, monkeypatch, lambda *a, **k: _with_stub(base), probes=())
    assert res['asof'] is None
    assert res['results'] == [], (
        '佔位棒沒有把量比打下去——那這組測試守的東西就不是真的症狀'
    )


def test_有收過盤的日子一根都不砍(tmp_path, monkeypatch):
    """成本是零。真的收過盤的日子，這整段只多問五檔權值股。"""
    base = _series()
    res = _run(tmp_path, monkeypatch, lambda *a, **k: base)
    assert res['asof'] is None, f"好好的一天卻被砍到 {res['asof']}"
    assert len(res['results']) == len(CODES)


def test_按日期砍_不是砍掉最後一根(tmp_path, monkeypatch):
    """40 檔實測裡有一檔的最後一根本來就停在前一個交易日（它沒有佔位棒）。

    「砍掉最後一根」會把它真正的收盤砍掉，於是它的量比變成前一天的，
    答案安靜地錯掉。按日期砍對兩種情形都對。
    """
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


# ---------------------------------------------------------------------------
# 報告不可以說謊：20 日均量那一關的單位


def test_均量門檻的單位要和資料一致():
    """yfinance 的 Volume 是**股數**，不是張數。

    實測 2330 的二十日均量是 18,089,518（約 18,000 張）。而門檻 1000 直接
    和這個數字比，所以這一關實際上是「> 1 張」——幾乎不擋任何東西。

    數字本身沒有錯（真正在擋流動性的是日均成交金額 5,000 萬），錯的是說明：
    Excel 第一分頁和網頁的輸入框上都印著「張」。一份印著「> 1,000 張」、
    實際跑「> 1 張」的報告，比一份門檻訂得寬的報告危險得多——後者看得出來。
    """
    line = dict(pl.Rules().describe())['① 基礎流動性防禦']
    assert '股' in line and '張' not in line, line
    units = {row[0]: row[2] for row in pl.LIVE_FIELDS}
    assert units['min_vol20'] == '股', units['min_vol20']
