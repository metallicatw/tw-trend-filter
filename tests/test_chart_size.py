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

from test_chart_js import _build_page, _page_js, _series

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="這台機器沒有 node。CI 的 ubuntu runner 內建 node，那裡會跑到。",
)


def _prelude(narrow):
    return r"""
var REG = {};
var RAF = [];
var OBS = [];
var RELAYOUT = [];
var document = {
  getElementById: function (id) { return REG[id] || null; },
  addEventListener: function () {},
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  documentElement: { style: { setProperty: function () {} } },
};
var window = {
  addEventListener: function () {},
  matchMedia: function () { return { matches: __NARROW__, addListener: function () {} }; },
  innerWidth: __NARROW__ ? 390 : 1400, innerHeight: __NARROW__ ? 844 : 900,
};
var navigator = { userAgent: 'node' };
var console = { error: function () {}, warn: function () {}, log: function () {} };
var localStorage = {
  getItem: function () { return null; }, setItem: function () {}, removeItem: function () {},
};
function requestAnimationFrame(cb) { RAF.push(cb); return RAF.length; }
function flushRaf() {
  /* 真的瀏覽器一幀一幀地跑；這裡一口氣把排到的都跑完，跑出來的新的再跑一輪。
     上限只是為了不要在寫壞的時候無限迴圈。 */
  for (var i = 0; i < 8 && RAF.length; i++) {
    var q = RAF; RAF = [];
    q.forEach(function (f) { f(); });
  }
}
function ResizeObserver(cb) { this.cb = cb; this.targets = []; OBS.push(this); }
ResizeObserver.prototype.observe = function (t) { this.targets.push(t); };
ResizeObserver.prototype.disconnect = function () { this.targets = []; };
var Plotly = {
  react: function (div, data, layout) {
    div.data = data;
    div.layout = JSON.parse(JSON.stringify(layout));
    return Promise.resolve(div);
  },
  relayout: function (div, upd) {
    RELAYOUT.push(upd);
    Object.keys(upd).forEach(function (k) { div.layout[k] = upd[k]; });
    return Promise.resolve(div);
  },
};
function mkDiv(w, h) {
  return {
    clientWidth: w, clientHeight: h, innerHTML: '', data: [], layout: {},
    on: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    addEventListener: function () {},
  };
}
""".replace("__NARROW__", "true" if narrow else "false")


def _run(narrow, driver, series):
    html = _build_page(series)
    tpl = re.search(
        r'<script type="application/json" id="tf-figtpl">(.*?)</script>', html, re.DOTALL)
    assert tpl, "頁面上沒有圖表樣板 tf-figtpl"
    head = (
        "REG['tf-figtpl'] = { textContent: " + json.dumps(tpl.group(1)) + " };\n"
        "REG['tf-series'] = { textContent: '{}' };\n"
        "var S = " + json.dumps(series, ensure_ascii=False) + ";\n"
    )
    js = _prelude(narrow) + head + _page_js(html) + "\n" + driver
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drive.js")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(js)
        run = subprocess.run([shutil.which("node"), p], check=False,
                             capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, f"node 跑不起來：\n{run.stderr[-3000:]}"
    return json.loads(run.stdout)


_DRAW = r"""
var div = mkDiv(__PRE_W__, __PRE_H__);
REG['tf-plot'] = div;
(async function () {
  tfDraw('9999', S);
  div.clientWidth = __POST_W__; div.clientHeight = __POST_H__;
  await new Promise(function (r) { setTimeout(r, 0); });
  flushRaf();
  __EXTRA__
  process.stdout.write(JSON.stringify({
    figW: div.layout.width, figH: div.layout.height,
    relayouts: RELAYOUT, observers: OBS.length,
    observed: OBS.length ? OBS[0].targets.length : 0,
    observing_plot: OBS.length ? (OBS[0].targets[0] === div) : false,
    html: div.innerHTML,
  }));
})();
"""


def _draw(narrow, pre, post, extra="", series=None):
    s = series if series is not None else _series()
    driver = (_DRAW
              .replace("__PRE_W__", str(pre[0])).replace("__PRE_H__", str(pre[1]))
              .replace("__POST_W__", str(post[0])).replace("__POST_H__", str(post[1]))
              .replace("__EXTRA__", extra))
    got = _run(narrow, driver, s)
    assert "⚠" not in (got.get("html") or ""), f"圖畫不出來：{got['html']}"
    return got


@needs_node
def test_桌機_畫完之後圖的高度等於容器的高度():
    got = _draw(False, (0, 0), (1180, 659))
    assert (got["figW"], got["figH"]) == (1180, 659), (
        f"容器 1180×659，圖卻是 {got['figW']}×{got['figH']}——"
        "高出來的部分會蓋住下面那排〔時間範圍〕"
    )


@needs_node
def test_手機_畫完之後圖也填滿容器():
    got = _draw(True, (0, 0), (390, 405))
    assert (got["figW"], got["figH"]) == (390, 405), (
        f"容器 390×405，圖卻是 {got['figW']}×{got['figH']}"
    )


@needs_node
def test_容器之後才變大小也要跟著調():
    extra = r"""
  div.clientWidth = 430; div.clientHeight = 812;
  OBS.forEach(function (o) { o.cb([], o); });
  flushRaf();
"""
    got = _draw(True, (0, 0), (390, 405), extra=extra)
    assert got["observers"] == 1, f"沒有掛 ResizeObserver（掛了 {got['observers']} 個）"
    assert got["observing_plot"], "ResizeObserver 沒有盯著圖表那個容器"
    assert (got["figW"], got["figH"]) == (430, 812), (
        f"容器變成 430×812 之後，圖還是 {got['figW']}×{got['figH']}"
    )


@needs_node
def test_尺寸沒變就不准重畫():
    extra = r"""
  var before = RELAYOUT.length;
  OBS.forEach(function (o) { o.cb([], o); });
  flushRaf();
  var after = RELAYOUT.length;
"""
    extra += "  if (after !== before) { throw new Error('尺寸沒變卻重畫了 ' + (after - before) + ' 次'); }\n"
    _draw(True, (0, 0), (390, 405), extra=extra)


@needs_node
def test_換一檔不會再掛一個觀察者():
    extra = r"""
  tfDraw('9999', S);
  await new Promise(function (r) { setTimeout(r, 0); });
  flushRaf();
"""
    got = _draw(True, (0, 0), (390, 405), extra=extra)
    assert got["observers"] == 1, f"掛了 {got['observers']} 個 ResizeObserver"


@needs_node
def test_手機版把兩個直排的縱軸標題拿掉():
    driver = r"""
var fig = tfSeriesFig(S);
tuneForNarrow(fig);
process.stdout.write(JSON.stringify({
  y1: ((fig.layout.yaxis || {}).title || {}).text,
  y2: ((fig.layout.yaxis2 || {}).title || {}).text,
  left: (fig.layout.margin || {}).l,
}));
"""
    s = _series()
    narrow = _run(True, driver, s)
    wide = _run(False, driver, s)

    assert narrow["y1"] == "" and narrow["y2"] == "", (
        f"手機版還留著縱軸標題：{narrow['y1']!r} / {narrow['y2']!r}"
    )
    assert narrow["left"] < 46, f"左留白沒有縮：{narrow['left']}"
    assert narrow["left"] == 36, f"左留白是 {narrow['left']}"
    assert wide["y1"] and wide["y2"], (
        f"桌機版的縱軸標題被一起拿掉了：{wide['y1']!r} / {wide['y2']!r}"
    )


@needs_node
def test_手機版把線壓細():
    driver = r"""
var fig = tfSeriesFig(S);
tuneForNarrow(fig);
process.stdout.write(JSON.stringify({
  widths: (fig.data || []).map(function (t) {
    return (t.line && typeof t.line.width === 'number') ? t.line.width : null;
  }),
}));
"""
    s = _series()
    narrow = _run(True, driver, s)["widths"]
    wide = _run(False, driver, s)["widths"]

    thick = [w for w in narrow if w is not None and w > 1.2]
    assert not thick, f"手機版還有比 1.2 粗的線：{thick}"
    assert any(w is not None and w > 1.2 for w in wide), (
        f"桌機版本來就沒有粗線，上面那條測試等於沒測到東西：{wide}"
    )
    for n, w in zip(narrow, wide):
        if n is not None and w is not None and w <= 1.2:
            assert n == w, f"本來就只有 {w} 的線被動到，變成 {n}"
