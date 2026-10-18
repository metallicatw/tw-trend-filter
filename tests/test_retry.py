import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tw_trend_filter.pipeline as pl

RATE_LIMIT = "YFRateLimitError: Too Many Requests. Rate limited. Try after a while."




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
    assert pl._worth_retry(RATE_LIMIT)
    assert pl._worth_retry("ConnectionError: Connection reset by peer")
    assert not pl._worth_retry(pl.NO_DATA), "空表不值得等——那個代號就是沒有資料"
    assert not pl._worth_retry("")




def _frame(days=150):
    idx = pd.bdate_range("2025-01-01", periods=days)
    closes = [100.0 + i * 0.3 for i in range(days)]
    return pd.DataFrame(
        {"Open": closes, "High": [c * 1.01 for c in closes],
         "Low": [c * 0.99 for c in closes], "Close": closes,
         "Volume": [600_000] * days},
        index=idx,
    )


def _run(tmp_path, monkeypatch, download, codes, retry_rounds=((0, 2),),
         breaker=None):
    monkeypatch.setattr(pl, "load_tw_stock_universe", lambda *a, **k: (
        [f"{c}.TW" for c in codes],
        {f"{c}.TW": f"測試{c}" for c in codes},
        {c: "測試業" for c in codes},
    ))
    monkeypatch.setattr(pl.yf, "download", download)
    return pl.run(str(tmp_path), workers=2,
                  retry_rounds=retry_rounds,
                  breaker=breaker or pl.RateLimitBreaker(sleep=lambda s: None),
                  open_when_done=False, plotly_cdn=False)


CODES = ["1101", "1102", "1103", "1104", "1105"]

MANY = [str(1101 + i) for i in range(200)]


def test_第一輪被限流_補問拿得到就算掃到(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, _limited_until(3, CODES), CODES)
    assert res["scanned"] == len(CODES), (
        f"補問之後還是只掃到 {res['scanned']}/{len(CODES)} 檔"
    )
    assert not res["error_kinds"], f"補問拿到了卻還記著失敗：{res['error_kinds']}"


def _limited_until(n_limited, codes):
    seen = {}

    def download(ticker, *a, **k):
        code = ticker.split(".")[0]
        idx = codes.index(code) if code in codes else 99
        seen[ticker] = seen.get(ticker, 0) + 1
        if idx < n_limited and seen[ticker] <= 2:
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
        return _frame()

    return download


def test_補問還是拿不到就記成限流(tmp_path, monkeypatch):
    def always_limited(ticker, *a, **k):
        if ticker.startswith(("1104", "1105")):
            raise RuntimeError("Too Many Requests. Rate limited.")
        return _frame()

    res = _run(tmp_path, monkeypatch, always_limited, CODES)
    assert res["error_kinds"].get("RateLimited") == 2, res["error_kinds"]
    assert "DownloadFailed" not in res["error_kinds"], res["error_kinds"]
    assert res["scanned"] == 3


def test_零星的空表不記成限流_也不重問(tmp_path, monkeypatch):
    calls = {}

    def one_empty(ticker, *a, **k):
        calls[ticker] = calls.get(ticker, 0) + 1
        return pd.DataFrame() if ticker.startswith("1104") else _frame()

    res = _run(tmp_path, monkeypatch, one_empty, MANY)
    assert res["error_kinds"].get("DownloadFailed") == 1, res["error_kinds"]
    assert "RateLimited" not in res["error_kinds"], res["error_kinds"]
    assert calls["1104.TW"] == 2, (
        f"沒有資料的代號被重問了（總共問了 {calls['1104.TW']} 次）——"
        "那是每天十幾檔、注定拿不到的東西"
    )


def test_失敗訊息要留著原因(tmp_path, monkeypatch):
    def limited(ticker, *a, **k):
        if ticker.startswith("1105"):
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
        return _frame()

    res = _run(tmp_path, monkeypatch, limited, CODES)
    blob = " ".join(res.get("error_samples") or [])
    assert "Too Many Requests" in blob, f"樣本裡沒有真正的原因：{blob!r}"


def test_不補問的時候不會等(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(pl._time, "sleep", lambda s: slept.append(s))

    def limited(ticker, *a, **k):
        raise RuntimeError("Too Many Requests. Rate limited.")

    _run(tmp_path, monkeypatch, limited, CODES, retry_rounds=())
    assert slept == [], f"關掉補問卻還是睡了 {slept}"


def test_全部順利的日子一秒都不等(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(pl._time, "sleep", lambda s: slept.append(s))
    _run(tmp_path, monkeypatch, lambda *a, **k: _frame(), CODES,
         retry_rounds=pl.RETRY_ROUNDS)
    assert slept == [], f"一檔都沒漏卻睡了 {slept}"


def test_零星的空表不必等(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(pl._time, "sleep", lambda s: slept.append(s))

    def one_empty(ticker, *a, **k):
        return pd.DataFrame() if ticker.startswith("1105") else _frame()

    _run(tmp_path, monkeypatch, one_empty, MANY, retry_rounds=pl.RETRY_ROUNDS)
    assert slept == [], f"零星的空表卻睡了 {slept}"




def test_空表多到不正常就要當成被擋掉重問(tmp_path, monkeypatch):
    seen = {}

    def empty_then_fine(ticker, *a, **k):
        seen[ticker] = seen.get(ticker, 0) + 1
        idx = MANY.index(ticker.split(".")[0])
        if idx < 100 and seen[ticker] <= 2:
            return pd.DataFrame()
        return _frame()

    res = _run(tmp_path, monkeypatch, empty_then_fine, MANY)
    assert res["scanned"] == len(MANY), (
        f"只掃到 {res['scanned']}/{len(MANY)}——幾百檔空表沒有被當成「被擋掉」，"
        "所以一檔都沒有補問"
    )
    assert not res["error_kinds"], res["error_kinds"]


def test_空表的數量門檻踩在邊界上():
    assert not pl._mass_empty(19, 1988)
    assert pl._mass_empty(20, 1988)
    assert not pl._mass_empty(15, 1988), "健康那一趟的 15 檔不該觸發"
    assert pl._mass_empty(308, 1988), "壞掉那一趟的 308 檔一定要觸發"
    assert not pl._mass_empty(1, 100), "剛好 1% 就觸發的話，門檻實際上是 0.99%"
    assert pl._mass_empty(2, 100)
    assert not pl._mass_empty(0, 0)


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
    assert pl.RETRY_ROUNDS, "預設沒有補問"
    import inspect
    sig = inspect.signature(pl.run)
    assert sig.parameters["retry_rounds"].default == pl.RETRY_ROUNDS
    waits = [w for w, _ in pl.RETRY_ROUNDS]
    assert waits == sorted(waits) and waits[0] > 0, (
        f"等的秒數要遞增而且大於零，現在是 {waits}——"
        "限流是按時間窗算的，等 0 秒等於沒等"
    )
    assert len(pl.RETRY_ROUNDS) >= 3, f"只有 {len(pl.RETRY_ROUNDS)} 輪補問"
    assert sum(waits) >= 300, (
        f"三輪加起來只等 {sum(waits)} 秒。限流的時間窗比這長，"
        "而等不夠的那一趟會整個白跑"
    )


def test_命令列關得掉(tmp_path, monkeypatch):
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




class _Clock:

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _breaker(**kw):
    c = _Clock()
    b = pl.RateLimitBreaker(sleep=c.sleep, clock=c.now, **kw)
    return b, c


def test_連續被限流才踩煞車():
    b, c = _breaker(streak=5, cooldown=60)
    for _ in range(4):
        b.note_failure(RATE_LIMIT)
    assert b.trips == 0, "才 4 檔就踩了煞車"
    assert b.wait_if_cooling() == 0
    b.note_failure(RATE_LIMIT)
    assert b.trips == 1, "連續 5 檔被限流卻沒有踩煞車"
    assert b.wait_if_cooling() == 60
    assert c.slept == [60]


def test_中間問到一檔就重新數():
    b, _ = _breaker(streak=3)
    b.note_failure(RATE_LIMIT)
    b.note_failure(RATE_LIMIT)
    b.note_ok()
    b.note_failure(RATE_LIMIT)
    b.note_failure(RATE_LIMIT)
    assert b.trips == 0, "成功之後沒有重新數，兩段不連續的失敗被接在一起"


def test_別種失敗既不算也不清空():
    b, _ = _breaker(streak=3)
    b.note_failure(RATE_LIMIT)
    b.note_failure(pl.NO_DATA)
    b.note_failure("ConnectionError: Connection reset by peer")
    assert b.trips == 0
    b.note_failure(RATE_LIMIT)
    b.note_failure(RATE_LIMIT)
    assert b.trips == 1, "夾在中間的別種錯誤把連續計數清掉了"


def test_停的是整批不是每一條連線各停一次():
    b, c = _breaker(streak=1, cooldown=60)
    b.note_failure(RATE_LIMIT)
    c.t += 20
    assert b.wait_if_cooling() == 40, "記的是「要睡多久」，不是「停到什麼時候」"
    waits = [b.wait_if_cooling() for _ in range(8)]
    assert waits == [0] * 8, f"後面八條連線又各停了一次：{waits}"
    assert c.slept == [40]


def test_連踩連停會加倍_但有上限():
    b, c = _breaker(streak=1, cooldown=60, max_cooldown=240)
    waited = []
    for _ in range(5):
        b.note_failure(RATE_LIMIT)
        waited.append(b.wait_if_cooling())
    assert waited == [60, 120, 240, 240, 240], waited


def test_停完之後問到了就從頭算起():
    b, _ = _breaker(streak=1, cooldown=60, max_cooldown=240)
    b.note_failure(RATE_LIMIT)
    assert b.wait_if_cooling() == 60
    b.note_ok()
    b.note_failure(RATE_LIMIT)
    assert b.wait_if_cooling() == 60, "額度回來過了，下一次卻還是用加倍後的秒數"


def test_順利的時候一秒都不停():
    b, c = _breaker(streak=5)
    for _ in range(100):
        b.note_ok()
        assert b.wait_if_cooling() == 0
    assert c.slept == []
    assert b.trips == 0




def test_尾巴被限流的時候真的會踩煞車(tmp_path, monkeypatch):
    def tail_limited(ticker, *a, **k):
        if MANY.index(ticker.split(".")[0]) >= 100:
            raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
        return _frame()

    b, c = _breaker(streak=5, cooldown=60)
    _run(tmp_path, monkeypatch, tail_limited, MANY, retry_rounds=(), breaker=b)
    assert b.trips >= 1, "後面 100 檔全部被限流，卻一次煞車都沒踩——還在硬打"
    assert c.slept, "踩了煞車卻沒有真的停"


def test_煞車沒有拖慢順利的那一趟(tmp_path, monkeypatch):
    b, c = _breaker(streak=5)
    _run(tmp_path, monkeypatch, lambda *a, **k: _frame(), MANY,
         retry_rounds=(), breaker=b)
    assert b.trips == 0 and c.slept == [], f"一檔都沒漏卻停了 {c.slept}"


def test_零星的限流不會踩煞車(tmp_path, monkeypatch):
    def a_few(ticker, *a, **k):
        idx = MANY.index(ticker.split(".")[0])
        if idx in (7, 40, 133):
            raise RuntimeError("Too Many Requests. Rate limited.")
        return _frame()

    b, c = _breaker(streak=5)
    _run(tmp_path, monkeypatch, a_few, MANY, retry_rounds=(), breaker=b)
    assert b.trips == 0 and c.slept == [], f"三檔零星限流卻停了 {c.slept}"


def test_每送一個請求之前都會先問煞車(tmp_path, monkeypatch):
    class Counting(pl.RateLimitBreaker):
        asked = 0

        def wait_if_cooling(self):
            Counting.asked += 1
            return super().wait_if_cooling()

    b = Counting(sleep=lambda s: None)
    _run(tmp_path, monkeypatch, lambda *a, **k: _frame(), CODES,
         retry_rounds=(), breaker=b)
    assert Counting.asked >= len(CODES), (
        f"{len(CODES)} 檔只問了煞車 {Counting.asked} 次——有請求沒有經過它"
    )


def test_煞車預設是開著的():
    import inspect
    sig = inspect.signature(pl.run)
    assert sig.parameters["breaker"].default is None, "預設值不該是共用的同一顆"
    assert pl.RATE_LIMIT_STREAK >= 2, "連續 1 檔就停的話，每天都會停"
    assert pl.RATE_LIMIT_COOLDOWN > 0, "停 0 秒等於沒停——限流是按時間窗算的"
    assert pl.RATE_LIMIT_MAX_COOLDOWN >= pl.RATE_LIMIT_COOLDOWN
