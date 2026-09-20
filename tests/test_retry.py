"""被 Yahoo 限流的那幾檔，等一下再問一次。

## 使用者看到的症狀

> 手動執行兩次，前後可能只差 10 分鐘，篩出來的個股數量卻差很多

## 為什麼

2026-09-20 那一份 log：

    篩選完成！今日共 6 檔標的通過（真的掃到 1822/1988 檔）
    ⚠ 有 162 檔沒有掃成（不等於沒過篩）：
        DownloadFailed                 162 檔
        例：4121.TWO：兩次下載都沒拿到資料

而同一份 log 再往上幾百行，yfinance 自己印的是：

    YFRateLimitError('Too Many Requests. Rate limited. Try after a while.')

兩件事因此同時成立：

1. **摘要說不出真正的原因。** 「兩次下載都沒拿到資料」這句話對「被限流」和
   「這個代號根本沒有資料」是同一句，所以看 log 的人沒辦法判斷要不要重跑。
2. **重試的方式對限流無效。** 原本的重試是**當場**換一條連線再送一次，而
   Yahoo 的限流是按時間窗算的——當場再問拿到的是同一句 Too Many Requests。

限流又是**按 IP** 算的，而 GitHub runner 的出口 IP 和別人共用。所以每一次跑被
擋掉的是隨機的一批，十分鐘後再跑又是另一批——這就是「差很多」。

## 改法

沒問到的先記進 `_failed`，整輪跑完之後隔 45 秒用 2 條連線補問一次、再隔 90 秒
用 1 條補問一次，最後才結算。結算時分成 `RateLimited` 與 `DownloadFailed`
兩類，因為它們該做的事不一樣（前者是母體少了，後者是常態）。

**只補問出過例外的那幾檔。** Yahoo 好好地回一份空表代表這個代號沒有資料
（下市、剛掛牌、代號打錯），等多久都一樣；每天都有幾十檔是這樣，連它們也等
就變成每天無條件多花兩分鐘去問一批注定拿不到的東西。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tw_trend_filter.pipeline as pl

RATE_LIMIT = "YFRateLimitError: Too Many Requests. Rate limited. Try after a while."


# ---------------------------------------------------------------------------
# 分類：這一則失敗是什麼


@pytest.mark.parametrize("why", [
    RATE_LIMIT,
    "YFRateLimitError('Too Many Requests')",
    "HTTPError: 429 Client Error: Too Many Requests for url",
    "YFRateLimitError: rate limited",
])
def test_認得出限流(why):
    assert pl._is_rate_limited(why), why


@pytest.mark.parametrize("why", [
    pl.NO_DATA,
    "",
    "JSONDecodeError: Expecting value",
    "KeyError: 'Close'",
])
def test_不把別的失敗當成限流(why):
    assert not pl._is_rate_limited(why), why


def test_只補問出過例外的():
    """Yahoo 回一份空表 ＝ 這個代號沒有資料，等多久都一樣。

    這條分界決定了「要不要付那 135 秒」。全市場每天都有幾十檔是沒有資料的
    正常代號，連它們也等的話，順利的日子也會每天多花兩分鐘。
    """
    assert pl._worth_retry(RATE_LIMIT)
    assert pl._worth_retry("ConnectionError: Connection reset by peer")
    assert not pl._worth_retry(pl.NO_DATA), "空表不值得等——那個代號就是沒有資料"
    assert not pl._worth_retry("")


# ---------------------------------------------------------------------------
# 真的跑一趟


def _frame(days=150):
    idx = pd.bdate_range("2025-01-01", periods=days)
    closes = [100.0 + i * 0.3 for i in range(days)]
    return pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes],
         "Low": [c * 0.99 for c in closes], "Close": closes,
         "Volume": [600_000] * days},
        index=idx,
    )


def _run(tmp_path, monkeypatch, download, codes, retry_rounds=((0, 2),)):
    """跑一趟 `pipeline.run`，下載行為由呼叫端決定。

    `retry_rounds` 預設等 0 秒——等待本身不是這裡要測的（那是 `_time.sleep`
    的事），要測的是「有沒有真的再問一次、問到了算不算數」。
    """
    monkeypatch.setattr(pl, "load_tw_stock_universe", lambda *a, **k: (
        [f"{c}.TW" for c in codes],
        {f"{c}.TW": f"測試{c}" for c in codes},
        {c: "測試業" for c in codes},
    ))
    monkeypatch.setattr(pl.yf, "download", download)
    return pl.run(str(tmp_path), make_excel=False, workers=2,
                  retry_rounds=retry_rounds,
                  open_when_done=False, plotly_cdn=False)


CODES = ["1101", "1102", "1103", "1104", "1105"]


def test_第一輪被限流_補問拿得到就算掃到(tmp_path, monkeypatch):
    """這一條是那個症狀的直接迴歸：同樣的市場、同樣的門檻，第一輪被擋掉的那

    幾檔在補問時拿得到，所以**兩次跑會得到同一份名單**。
    """
    res = _run(tmp_path, monkeypatch, _limited_until(3, CODES), CODES)
    assert res["scanned"] == len(CODES), (
        f"補問之後還是只掃到 {res['scanned']}/{len(CODES)} 檔"
    )
    assert not res["error_kinds"], f"補問拿到了卻還記著失敗：{res['error_kinds']}"


def _limited_until(n_limited, codes):
    """前 `n_limited` 檔在第一次被問到時丟限流，第二次就正常。

    模擬的是時間窗過去之後限流解除——而那正是 `RETRY_ROUNDS` 在等的東西。
    """
    seen = {}

    def download(ticker, *a, **k):
        code = ticker.split(".")[0]
        idx = codes.index(code) if code in codes else 99
        seen[ticker] = seen.get(ticker, 0) + 1
        # `_download` 自己一次會試兩下（自訂 session ＋ 預設連線），
        # 所以「第一輪」是前兩次。
        if idx < n_limited and seen[ticker] <= 2:
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
        return _frame()

    return download


def test_補問還是拿不到就記成限流(tmp_path, monkeypatch):
    """記成 `RateLimited` 而不是 `DownloadFailed`。

    兩者混在一起的時候，「162 檔沒掃到」這句話沒辦法回答「要不要重跑」。
    """
    def always_limited(ticker, *a, **k):
        if ticker.startswith(("1104", "1105")):
            raise RuntimeError("Too Many Requests. Rate limited.")
        return _frame()

    res = _run(tmp_path, monkeypatch, always_limited, CODES)
    assert res["error_kinds"].get("RateLimited") == 2, res["error_kinds"]
    assert "DownloadFailed" not in res["error_kinds"], res["error_kinds"]
    assert res["scanned"] == 3


def test_沒有資料的代號不記成限流_也不重問(tmp_path, monkeypatch):
    """空表 ＝ 這個代號沒有資料。它要記成 DownloadFailed，而且不該被重問。"""
    calls = {}

    def empty_for_two(ticker, *a, **k):
        calls[ticker] = calls.get(ticker, 0) + 1
        if ticker.startswith(("1104", "1105")):
            return pd.DataFrame()
        return _frame()

    res = _run(tmp_path, monkeypatch, empty_for_two, CODES)
    assert res["error_kinds"].get("DownloadFailed") == 2, res["error_kinds"]
    assert "RateLimited" not in res["error_kinds"], res["error_kinds"]
    # `_download` 一檔試兩下（自訂 session ＋ 預設連線）。補問會變成四下。
    assert calls["1104.TW"] == 2, (
        f"沒有資料的代號被重問了（總共問了 {calls['1104.TW']} 次）——"
        "那是每天幾十檔、注定拿不到的東西"
    )


def test_失敗訊息要留著原因(tmp_path, monkeypatch):
    """「兩次下載都沒拿到資料」這句話對限流和查無此股是同一句。

    留著例外原文，看 log 的人才判斷得出要不要重跑。
    """
    def limited(ticker, *a, **k):
        if ticker.startswith("1105"):
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
        return _frame()

    res = _run(tmp_path, monkeypatch, limited, CODES)
    blob = " ".join(res.get("error_samples") or [])
    assert "Too Many Requests" in blob, f"樣本裡沒有真正的原因：{blob!r}"


def test_不補問的時候不會等(tmp_path, monkeypatch):
    """`--no-retry`（`retry_rounds=()`）要真的一秒都不等。

    排程不該關它，但煙霧測試會——而一個「關了還是等兩分鐘」的開關等於沒有。
    """
    slept = []
    monkeypatch.setattr(pl._time, "sleep", lambda s: slept.append(s))

    def limited(ticker, *a, **k):
        raise RuntimeError("Too Many Requests. Rate limited.")

    _run(tmp_path, monkeypatch, limited, CODES, retry_rounds=())
    assert slept == [], f"關掉補問卻還是睡了 {slept}"


def test_全部順利的日子一秒都不等(tmp_path, monkeypatch):
    """補問的成本只有在真的有檔數沒問到的時候才付。"""
    slept = []
    monkeypatch.setattr(pl._time, "sleep", lambda s: slept.append(s))
    _run(tmp_path, monkeypatch, lambda *a, **k: _frame(), CODES,
         retry_rounds=pl.RETRY_ROUNDS)
    assert slept == [], f"一檔都沒漏卻睡了 {slept}"


def test_只有空表的日子也不等(tmp_path, monkeypatch):
    """每天都有幾十檔是「沒有資料的正常代號」。為它們等 135 秒是純粹的浪費。"""
    slept = []
    monkeypatch.setattr(pl._time, "sleep", lambda s: slept.append(s))

    def some_empty(ticker, *a, **k):
        return pd.DataFrame() if ticker.startswith("1105") else _frame()

    _run(tmp_path, monkeypatch, some_empty, CODES, retry_rounds=pl.RETRY_ROUNDS)
    assert slept == [], f"只有空表卻睡了 {slept}"


def test_有限流才等_而且照著設定等(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(pl._time, "sleep", lambda s: slept.append(s))

    def limited(ticker, *a, **k):
        if ticker.startswith("1105"):
            raise RuntimeError("Too Many Requests. Rate limited.")
        return _frame()

    _run(tmp_path, monkeypatch, limited, CODES, retry_rounds=((45, 2), (90, 1)))
    assert slept == [45, 90], f"等的秒數不對：{slept}"


def test_預設就是會補問():
    """排程走的是預設值。預設關掉的話，這整個修正對排程等於不存在。"""
    assert pl.RETRY_ROUNDS, "預設沒有補問"
    import inspect
    sig = inspect.signature(pl.run)
    assert sig.parameters["retry_rounds"].default == pl.RETRY_ROUNDS
    waits = [w for w, _ in pl.RETRY_ROUNDS]
    assert waits == sorted(waits) and waits[0] > 0, (
        f"等的秒數要遞增而且大於零，現在是 {waits}——"
        "限流是按時間窗算的，等 0 秒等於沒等"
    )


def test_命令列關得掉(tmp_path, monkeypatch):
    """`--no-retry` 要真的接到 `run(retry_rounds=...)` 上。

    參數加了、但忘了接進去，是這種開關最常見的壞法——而它沒有任何症狀，
    只是那兩分鐘永遠都在。
    """
    seen = {}

    def fake_run(*a, **k):
        seen.update(k)
        raise SystemExit(0)

    monkeypatch.setattr(pl, "load_tw_stock_universe", lambda *a, **k: ([], {}, {}))
    import tw_trend_filter.__main__ as m
    monkeypatch.setattr(m, "run", fake_run)
    for argv, want in ((["--no-retry"], ()), ([], pl.RETRY_ROUNDS)):
        seen.clear()
        with pytest.raises(SystemExit):
            m.main(["--output-dir", str(tmp_path), "--no-excel", *argv])
        assert seen.get("retry_rounds") == want, (
            f"{argv or '預設'} 應該是 {want}，實際傳進去的是 {seen.get('retry_rounds')!r}"
        )
