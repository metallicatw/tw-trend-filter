"""同一份數列，Python 組一張圖、瀏覽器組一張圖——兩張必須一模一樣。

## 這條測試在守什麼

報告頁上的圖不再是 Python 送過去的成品。送過去的是兩塊東西：

* 一份**樣板**（`chart_template()`）——`_chart_figure()` 用空資料跑出來的，
  兩百行樣式全部畫好、數列是空的。一頁一份，所有股票共用。
* 每一檔的**數列**（`_chart_series()`）——日期、OHLC、三條布林、兩條均線、
  量與均量。嵌在頁面上（今天過篩的那幾檔），或放在 Pages 上點下去才抓。

瀏覽器的 `tfSeriesFig()` 把數列填進樣板。這一步是新的、是 JS 寫的，而且它填錯
的時候**不會報錯**：把 20MA 填進 60MA 那條 trace，畫出來仍然是一張漂亮的圖，
只是兩條線互換了；把 customdata 的欄位順序弄反，hover 上去「開」的位置顯示的
是「高」。這些都要有人盯著看才發現得了，而多數時候沒有人在看。

所以這裡拿同一份 series 讓兩邊各組一張圖，逐一比對 trace 與 layout。Python 那
一支（`_chart_figure`）本來就是樣板的來源，所以它同時是這條測試的參考答案——
沒有「為了測試而存在的第二份實作」。

## 為什麼要 node

因為要跑的是**真的送到瀏覽器的那段 JS**，不是它的 Python 翻譯。node 在 GitHub
的 ubuntu runner 上是內建的，不必安裝任何東西；本機沒有 node 的時候這條會大聲
地跳過，而不是假裝通過。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tw_trend_filter.pipeline import (  # noqa: E402
    SERIES_ARRAYS,
    _chart_figure,
    _chart_series,
    _chart_window,
    chart_template,
)


def _sf(v, d=2):
    """和 build_interactive_html 裡那個同名的一樣：轉不出數字就 None。"""
    import math

    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, d)


def _series(days=300, keep=200):
    """一檔有漲有跌、量能不平均的假股票。

    刻意不用一條直線：`vcol`（每一根量棒的顏色）看的是收盤有沒有站上開盤，
    而一條單調上揚的線讓每一根都是紅的——那樣就算 JS 把那個判斷寫反了，
    兩邊的顏色陣列仍然完全相同。
    """
    import numpy as np
    import pandas as pd

    from tw_trend_filter.pipeline import compute_bollinger

    idx = pd.bdate_range("2023-01-02", periods=days)
    rng = np.random.default_rng(20260914)
    close = pd.Series(100 + np.cumsum(rng.normal(0.15, 2.0, days)), index=idx)
    close = close.clip(lower=5.0)
    jitter = rng.normal(0, 1.0, days)
    df = pd.DataFrame(
        {
            "Open": close + jitter,
            "High": np.maximum(close, close + jitter) + abs(rng.normal(0, 1.0, days)),
            "Low": np.minimum(close, close + jitter) - abs(rng.normal(0, 1.0, days)),
            "Close": close,
            "Volume": pd.Series(rng.integers(2e6, 9e6, days).astype(float), index=idx),
        },
        index=idx,
    )
    bmid, bup, bdn, _bw = compute_bollinger(close)
    # 停損跟著資料走，不是寫死一個數字。寫死的話它多半落在視窗外面，而視窗的
    # 下緣是**把停損算進去**才決定的（見 `_chart_window`）——一個永遠在外面的
    # 停損會讓那條規則整條測不到。真實的停損是「收盤 − 3 × ATR」，本來就在
    # 最後那根 K 棒附近。
    last = float(close.iloc[-1])
    res = {
        "code": "9999", "name": "測試", "industry": "其他",
        "ma20_last": float(close.rolling(20).mean().iloc[-1]),
        "ma60_last": float(close.rolling(60).mean().iloc[-1]),
        "stop_loss": round(last * 0.92, 2),
        "_df": df,
        "_ma20": close.rolling(20).mean(),
        "_ma60": close.rolling(60).mean(),
        "_boll_up": bup, "_boll_mid": bmid, "_boll_dn": bdn,
    }
    return _chart_series(res, keep, _sf)


#: 極小的 DOM。只要夠 `tfTemplate()` / `tfSeriesOf()` 找得到那兩個
#: `<script type="application/json">` 就好——刻意不引 jsdom，這個 repo 沒有
#: node_modules，而多餘的相容性只會讓失敗訊息變難讀。
PRELUDE = r"""
var REG = {};
var document = {
  getElementById: function (id) { return REG[id] || null; },
  addEventListener: function () {},
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
};
var window = {
  addEventListener: function () {},
  matchMedia: function () { return { matches: false, addListener: function () {} }; },
  innerWidth: 1440, innerHeight: 900,
};
var navigator = { userAgent: 'node' };
var console = { error: function () {}, warn: function () {}, log: function () {} };
var localStorage = {
  getItem: function () { return null; }, setItem: function () {}, removeItem: function () {},
};
"""


def _page_js(html):
    """把報告頁上那段主要的 JS 挖出來。

    頁面上有好幾個 `<script>`：plotly 的 CDN 標籤、`_live_block` 那一段、還有
    最後這一段主程式。要的是最後一段——`tfSeriesFig` 在那裡。
    """
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    for body in reversed(blocks):
        if "function tfSeriesFig" in body:
            return body
    raise AssertionError("報告頁上找不到 tfSeriesFig()")


def _build_page(series):
    """產一份只有這一檔的報告頁，回傳 HTML 字串。"""
    import datetime

    from test_report import _fake_result, _fake_snap
    from tw_trend_filter.pipeline import DEFAULT_RULES, build_interactive_html

    with tempfile.TemporaryDirectory() as tmp:
        path = build_interactive_html(
            [_fake_result()], "2026-09-04", tmp,
            datetime.datetime(2026, 9, 4, 15, 30),
            rules=DEFAULT_RULES, snapshots=[_fake_snap()])
        return open(path, encoding="utf-8").read()


def _js_figure(html, series):
    """在 node 裡跑真正那段 JS，回傳 `tfSeriesFig(series)` 的結果。"""
    tpl = re.search(
        r'<script type="application/json" id="tf-figtpl">(.*?)</script>', html, re.S)
    assert tpl, "頁面上沒有圖表樣板 tf-figtpl"
    driver = (
        "REG['tf-figtpl'] = { textContent: " + json.dumps(tpl.group(1)) + " };\n"
        "REG['tf-series'] = { textContent: '{}' };\n"
        "var S = " + json.dumps(series, ensure_ascii=False) + ";\n"
        "console.log = function(){};\n"
        "process.stdout.write(JSON.stringify(tfSeriesFig(S)));\n"
    )
    js = PRELUDE + _page_js(html) + "\n" + driver
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drive.js")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(js)
        run = subprocess.run([shutil.which("node"), p],
                             capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, f"node 跑不起來：\n{run.stderr[-3000:]}"
    return json.loads(run.stdout)


def _py_figure(series):
    import plotly.io as pio

    return json.loads(pio.to_json(_chart_figure(series)))


needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="這台機器沒有 node。CI 的 ubuntu runner 內建 node，那裡會跑到。",
)


@needs_node
def test_前端填出來的圖和_python_組出來的一模一樣():
    s = _series()
    html = _build_page(s)
    got = _js_figure(html, s)
    want = _py_figure(s)

    assert len(got["data"]) == len(want["data"]) == 11, (
        f"trace 數量對不上：JS {len(got['data'])} vs Python {len(want['data'])}"
    )

    for i, (g, w) in enumerate(zip(got["data"], want["data"])):
        # 名字先比：填錯 trace 最常見的症狀就是圖例上的數字接到別條線上。
        assert g.get("name") == w.get("name"), (
            f"trace {i} 的名字不一樣：JS {g.get('name')!r} vs Python {w.get('name')!r}"
        )
        assert g.get("type") == w.get("type"), f"trace {i} 型別不一樣"
        for key in ("x", "y", "open", "high", "low", "close", "customdata"):
            assert (key in g) == (key in w), f"trace {i} 的 {key} 一邊有一邊沒有"
            if key in g:
                assert g[key] == w[key], (
                    f"trace {i}（{w.get('name')}）的 {key} 不一樣，"
                    f"第一個差在第 {_first_diff(g[key], w[key])} 個"
                )
        # 量棒的顏色是逐根算的（收 >= 開 才是紅的），寫反了圖上會整片換色。
        gm, wm = g.get("marker") or {}, w.get("marker") or {}
        assert gm.get("color") == wm.get("color"), f"trace {i} 的 marker 顏色不一樣"

    # 預設視窗：三個 range 是前端重算的（樣板裡那組是空資料算出來的）。
    # 算錯的話圖打得開、線也在，只是 Y 軸停在 0 附近，畫面上一條貼著底的平線。
    for axis in ("xaxis2", "yaxis", "yaxis2"):
        assert got["layout"][axis]["range"] == want["layout"][axis]["range"], (
            f"{axis} 的預設範圍不一樣："
            f"JS {got['layout'][axis]['range']} vs Python {want['layout'][axis]['range']}"
        )


def _first_diff(a, b):
    if not isinstance(a, list) or not isinstance(b, list):
        return "?"
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


@needs_node
def test_樣板本身不帶任何一檔的數字():
    """樣板漏了清空的話，每一張圖都會混進那一檔假資料。

    `chart_template()` 是拿兩根 K 棒的假資料跑出來再清空的。清空那一步漏掉一個
    欄位，症狀是所有股票的圖上都多一根 2000 年的 K 棒——在最左邊，而預設視窗
    只看近三個月，所以要拉到「2年」才看得到。
    """
    tpl = chart_template()
    for i, tr in enumerate(tpl["data"]):
        for key in ("x", "y", "open", "high", "low", "close", "customdata"):
            if key in tr:
                assert tr[key] == [], f"樣板的 trace {i} 的 {key} 沒有清空：{tr[key]!r}"
        mk = tr.get("marker")
        if isinstance(mk, dict) and isinstance(mk.get("color"), list):
            assert mk["color"] == [], f"樣板的 trace {i} 還帶著逐根的顏色"
    assert "2000-01" not in json.dumps(tpl), "樣板裡還留著造樣板用的那兩個日期"


def test_數列裡每一條都一樣長():
    """長度不一致的話 plotly 不會抱怨，它會靜靜地把短的那條畫到一半就停。"""
    s = _series()
    n = len(s["d"])
    assert n > 0
    for key in SERIES_ARRAYS:
        assert len(s[key]) == n, f"{key} 有 {len(s[key])} 個，日期有 {n} 個"


def test_數列比整張圖小得多():
    """這整個改動的理由就是這個數字，所以讓它有人守著。

    以前每一檔在 Pages 上是一整張 plotly 圖：日期那幾百個字串被十一條 trace 各
    存一份，customdata 又把每條數列原封不動再存一次，然後同一套兩百行的樣式
    每一檔都帶一份。量出來 240 KB。全市場一千九百檔，每天往 Pages 推四百多 MB。

    這裡不釘死絕對數字（資料一變就會紅一次，而那不是壞掉），釘的是比例。
    """
    s = _series(days=900, keep=634)
    ser = len(json.dumps(s, ensure_ascii=False, separators=(",", ":")).encode())
    fig = len(json.dumps(_py_figure(s), ensure_ascii=False).encode())
    assert ser * 3 < fig, (
        f"數列 {ser:,} bytes、整張圖 {fig:,} bytes——沒有小到三分之一以下，"
        "這個改動的理由就不成立了"
    )


def test_視窗範圍不會把停損線切掉():
    """停損線是這份報告的重點之一，落在視窗外面等於沒畫。"""
    s = _series()
    lo, hi, _vhi = _chart_window(s)
    assert lo <= s["stop"] <= hi, f"停損 {s['stop']} 落在 [{lo}, {hi}] 外面"
