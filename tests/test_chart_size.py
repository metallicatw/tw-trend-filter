"""圖表要填滿它的容器——不多也不少。

## 這條測試在守什麼

報告頁上那張圖的高度不是 CSS 說了算。plotly 的 svg 是**寫死像素**的，所以有
一段 JS（`fitPlotSize`）負責量容器、再叫 `Plotly.relayout` 把圖調成一樣大。
量錯了不會報錯，畫面上只是「怪怪的」，而且兩個方向的症狀看起來毫不相干：

    桌機 1400×900   容器 659px，圖 755px  → 高出 96px，蓋住下面那排〔時間範圍〕
    手機  390×844   容器 405px，圖 181px  → 只用了 45%，價格那一格剩一百出頭

（這兩行是 2026-09-20 那份實際報告在無頭瀏覽器裡量到的，不是估的。）

同一個原因：`fitPlotSize` 只掛在 `window.resize` 上，所以它**永遠沒有在畫完
之後跑過**——而容器真正的高度要等版面排完才量得到。更糟的是它自己有一層
`_twW/_twH` 快取，第一次量到的錯誤尺寸就此凍住，之後再也不會修正。

所以這裡守三件事：

1. 畫完之後一定會重新量一次，而且量到的是**畫完之後**的容器尺寸；
2. 容器之後又變大小（字型載入完、外層切手機版、側欄卡片畫出來——這些都不會
   觸發 `window.resize`）也要跟著調；
3. 但尺寸沒變的時候不准重畫。一次 relayout 是六百根 K 棒各一條 path，
   那是整張圖最貴的一次白工，而切換股票時尺寸根本沒變。

另外兩條守 `tuneForNarrow()`：手機上線要細、兩個直排的縱軸標題要拿掉
（實測「價格 (元)」佔 464–507、「成交量(張)」佔 506–557，真的疊在一起）。

## 為什麼要 node

跑的是**真的送到瀏覽器的那段 JS**，不是它的 Python 翻譯——和
`test_chart_js.py` 同一個理由、同一套做法。`Plotly`、`ResizeObserver`、
`requestAnimationFrame` 都是假的，但被測的那幾個函式是真的。
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

from test_chart_js import _build_page, _page_js, _series

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="這台機器沒有 node。CI 的 ubuntu runner 內建 node，那裡會跑到。",
)


def _prelude(narrow):
    """最小的 DOM ＋ 假的 Plotly／ResizeObserver／requestAnimationFrame。

    `narrow` 決定 `window.matchMedia('(max-width:760px)').matches`，也就是頁面
    上那個 `const NARROW`——手機版的所有調整都掛在它下面。
    """
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
    """在 node 裡跑真正那段 JS，回傳 driver 印出來的 JSON。"""
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


#: 畫一檔、等 promise 跑完、把排到的 rAF 都跑掉，然後回報結果。
#: `pre` 是畫之前量到的容器尺寸，`post` 是版面排完之後的——真實情形就是這樣：
#: `tfDraw` 在版面還沒排好的時候量一次（量到 0 或量到舊的），畫完才是準的。
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
    """圖比容器高就會壓到下面那排〔時間範圍〕。實測 659 的容器裡塞了 755 的圖。

    畫之前量到 0（版面還沒排好），所以 `tfDraw` 那一段 `w0>0 && h0>0` 不成立，
    layout 裡留著樣板寫死的 760——這正是線上那份報告的狀況。
    """
    got = _draw(False, (0, 0), (1180, 659))
    assert (got["figW"], got["figH"]) == (1180, 659), (
        f"容器 1180×659，圖卻是 {got['figW']}×{got['figH']}——"
        "高出來的部分會蓋住下面那排〔時間範圍〕"
    )


@needs_node
def test_手機_畫完之後圖也填滿容器():
    """同一個原因的另一個症狀：405px 的容器裡只畫了 181px 的圖，剩下 55% 空白。"""
    got = _draw(True, (0, 0), (390, 405))
    assert (got["figW"], got["figH"]) == (390, 405), (
        f"容器 390×405，圖卻是 {got['figW']}×{got['figH']}"
    )


@needs_node
def test_容器之後才變大小也要跟著調():
    """會讓容器變高／變矮、但**不會**觸發 window.resize 的事情不少：

    字型載入完、側欄那排卡片畫出來、外層網站按下〔切換手機版〕（那會把 iframe
    的寬度釘成 430px）。原本只掛 window.resize，這些時刻一個都接不到，而
    `_twW/_twH` 那層快取讓第一次量到的尺寸**永久**凍在那裡。
    """
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
    """一次 relayout 是六百根 K 棒各重畫一條 path，是整張圖最貴的一步。

    容器沒變的時候響的 ResizeObserver（有些瀏覽器在 observe 當下就會響一次）
    不可以真的去動那張圖。
    """
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
    """`tfDraw` 每換一檔就跑一次；監聽器掛兩次的症狀是同一件事做兩遍。"""
    extra = r"""
  tfDraw('9999', S);
  await new Promise(function (r) { setTimeout(r, 0); });
  flushRaf();
"""
    got = _draw(True, (0, 0), (390, 405), extra=extra)
    assert got["observers"] == 1, f"掛了 {got['observers']} 個 ResizeObserver"


@needs_node
def test_手機版把兩個直排的縱軸標題拿掉():
    """「價格 (元)」佔 464–507、「成交量(張)」佔 506–557——兩個直排的標題疊在

    一起。手機上兩個子圖加起來只有一百多像素，裝不下兩個直排標題。
    單位沒有消失：刻度自己就是數字，圖例上也寫著 20MA／60MA。
    """
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
    assert narrow["left"] == 34, f"拿掉標題之後左留白應該縮到 34，現在是 {narrow['left']}"
    # 桌機不受影響——那一格有四百多像素高，標題該留著。
    assert wide["y1"] and wide["y2"], (
        f"桌機版的縱軸標題被一起拿掉了：{wide['y1']!r} / {wide['y2']!r}"
    )


@needs_node
def test_手機版把線壓細():
    """手機上價格那一格只有一百多像素高，2.0 的線疊五條會把 K 棒蓋掉。

    使用者說的「解析度不足」其實是線太粗——把線壓細之後 K 棒才看得見。
    """
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
    # 沒有這一行的話，「原本就沒有粗線」也會讓上面那條綠燈。
    assert any(w is not None and w > 1.2 for w in wide), (
        f"桌機版本來就沒有粗線，上面那條測試等於沒測到東西：{wide}"
    )
    # 細的不要被「壓細」反而弄粗了。
    for n, w in zip(narrow, wide):
        if n is not None and w is not None and w <= 1.2:
            assert n == w, f"本來就只有 {w} 的線被動到，變成 {n}"
