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
    import math

    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, d)


def _series(days=300, keep=200):
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
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    for body in reversed(blocks):
        if "function tfSeriesFig" in body:
            return body
    raise AssertionError("報告頁上找不到 tfSeriesFig()")


def _build_page(series):
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
        gm, wm = g.get("marker") or {}, w.get("marker") or {}
        assert gm.get("color") == wm.get("color"), f"trace {i} 的 marker 顏色不一樣"

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
    s = _series()
    n = len(s["d"])
    assert n > 0
    for key in SERIES_ARRAYS:
        assert len(s[key]) == n, f"{key} 有 {len(s[key])} 個，日期有 {n} 個"


def test_數列比整張圖小得多():
    s = _series(days=900, keep=634)
    ser = len(json.dumps(s, ensure_ascii=False, separators=(",", ":")).encode())
    fig = len(json.dumps(_py_figure(s), ensure_ascii=False).encode())
    assert ser * 3 < fig, (
        f"數列 {ser:,} bytes、整張圖 {fig:,} bytes——沒有小到三分之一以下，"
        "這個改動的理由就不成立了"
    )


def test_視窗範圍不會把停損線切掉():
    s = _series()
    lo, hi, _vhi = _chart_window(s)
    assert lo <= s["stop"] <= hi, f"停損 {s['stop']} 落在 [{lo}, {hi}] 外面"


def test_布林三條軌道上下軌都在圖例上():
    fig = _py_figure(_series())
    shown = {t.get("name") for t in fig["data"] if t.get("showlegend")}
    assert {"布林上軌", "布林下軌"} <= shown, shown
