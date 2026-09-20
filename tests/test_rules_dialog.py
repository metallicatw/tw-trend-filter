"""〔💡 預設篩選條件〕：按下去要真的看得到東西。

## 它以前壞在哪裡

那顆燈泡本來是一個 `<details>`，展開的內容 `position:absolute` 掛在按鈕底下。
在**每一個**視窗寬度上它都是壞的，而且壞得很安靜——按下去像沒反應。

原因是頁首 `#topbar` 有 `max-height:66vh;overflow-y:auto`。那條 CSS 是保險絲，
擋的是「頁首長高 → `#main` 的高度算成負數 → 圖表區整個消失而且捲不回來」。
但一個 `overflow` 不是 `visible` 的祖先，會把絕對定位的子孫一起裁掉。

無頭瀏覽器量 2026-09-20 那份報告（把 `<details>` 打開再量盒子）：

    桌機 1400×900   盒子 y 111–247，#topbar 只到 116  → 看得見 5px
    手機  390×844   盒子 y 186–415，#topbar 只到 189  → 看得見 3px
    小手機 360×640  盒子 y 186–434，#topbar 只到 189  → 看得見 3px

## 改成什麼

`<dialog>` ＋ `showModal()`。那會把元素放進瀏覽器的 **top layer**，那一層不在
任何祖先的裁切範圍內——所以這個問題從根上消失，而 Esc 關閉、焦點鎖在視窗內、
背景遮罩三件事都變成瀏覽器的責任。

改完量到的（同一份報告、同一支腳本）：

    桌機   560×197 置中，完全在視窗內
    手機   390 寬、貼著底的抽屜，292 高，完全在視窗內
    小手機 360 寬，同上

## 這裡守什麼

版面本身要靠真的瀏覽器才量得到，而 CI 上沒有。所以這裡守的是**那些量測背後的
不變式**：視窗不靠祖先定位（`position:fixed`）、寬度沒有被瀏覽器預設樣式夾住
（`max-width`）、手機上貼著底、以及開關那幾條真的接上了——最後這一項是在 node
裡跑真正送到瀏覽器的那段 JS。
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

from tw_trend_filter.pipeline import DEFAULT_RULES, _live_block

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="這台機器沒有 node。CI 的 ubuntu runner 內建 node，那裡會跑到。",
)


def _css(html):
    m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    assert m, "頁面上沒有 <style>"
    return m.group(1)


def _rule(css, selector, inside_media=None):
    """把某一條規則的宣告挑出來。

    `inside_media` 限定在某個 @media 區塊裡找；不給就只找 @media **之前**那一段
    ——也就是所有寬度都吃的那一條。兩者一定要分得開，因為手機那一條會把桌機那
    一條的好幾個屬性蓋掉，混在一起找等於在測另一條規則。
    """
    if inside_media:
        i = css.find(inside_media)
        assert i >= 0, f"找不到 {inside_media}"
        # 這份樣式表裡的 @media 沒有巢狀，所以第一個 `}}` 就是收尾。
        # `+1`：那兩個右括號的第一個是**最後一條規則自己的**，要留著。
        j = css.find("}}", i)
        assert j > i, f"{inside_media} 沒有收尾"
        hay = css[i:j + 1]
    else:
        m = re.search(r"@media", css)
        hay = css[:m.start()] if m else css
    found = re.findall(re.escape(selector) + r"\{([^}]*)\}", hay)
    assert found, f"找不到規則 {selector}" + (f"（在 {inside_media} 裡）" if inside_media else "")
    assert len(found) == 1, f"{selector} 在同一段裡出現 {len(found)} 次，測到哪一條說不準"
    return found[0]


# ────────────────────────────────────────────────────────────────
# 結構
# ────────────────────────────────────────────────────────────────

def test_是彈出視窗不是內嵌展開():
    """`<details>` 那一版的內容會被 `#topbar` 裁掉，在每一個寬度上都是。"""
    html = _live_block(DEFAULT_RULES)
    assert "<dialog id=\"rules\"" in html, "〔預設篩選條件〕不是一個 <dialog>"
    assert "<details" not in html, "還留著 <details>——那一版會被頁首裁掉"
    assert "<summary" not in html
    # 按鈕要說得出它開的是哪一個視窗，不然讀螢幕的人按下去不知道發生了什麼。
    assert 'id="rules-btn"' in html
    assert 'aria-haspopup="dialog"' in html
    assert 'aria-controls="rules"' in html
    # 關閉鈕。手機上沒有鍵盤，Esc 不是出路。
    assert 'class="rules-x"' in html


def test_四道關卡的說明都在視窗裡():
    """視窗換了殼，內容不能跟著掉。"""
    html = _live_block(DEFAULT_RULES)
    body = html.split("<dialog", 1)[1]
    for label, _text in DEFAULT_RULES.describe():
        assert label in body, f"〔{label}〕沒有出現在視窗裡"
    assert "停損" in body


def test_頁首還是捲動容器_所以視窗不能靠祖先定位(tmp_path):
    """這兩件事是綁在一起的，拆開任何一邊都會把 bug 放回來。

    `#topbar` 的 `overflow-y:auto` 是保險絲（頁首長高會把 `#main` 的高度算成
    負數），所以它不會消失；而只要它在，掛在頁首裡的面板就**不能**用
    `position:absolute` ——會被裁掉。`position:fixed` 不受祖先 overflow 影響。

    寫死 `position:fixed` 而不是只靠 `showModal()` 的 top layer：拿不到
    `<dialog>` 的舊瀏覽器走 `open` 屬性那條路，那時候它是 `position:absolute`。
    """
    html = _build_page(_series())
    css = _css(html)
    assert "overflow-y:auto" in _rule(css, "#topbar"), (
        "#topbar 不再是捲動容器了——那這條測試的前提要重新確認"
    )
    dlg = _rule(css, ".rules-dlg")
    assert "position:fixed" in dlg, f"視窗不是 position:fixed：{dlg}"
    assert "position:absolute" not in dlg


def test_寬度沒有被瀏覽器的預設樣式夾住(tmp_path):
    """`max-width` 一定要自己寫一條。

    瀏覽器的預設樣式表給 `<dialog>` 的是 `max-width:calc(100% - 6px - 2em)`。
    那是**另一個屬性**，只寫 `width` 蓋不掉它——它會回頭把 width 夾住。
    實測 390px 的螢幕上量到 360（390 − 6 − 2×12），右邊白白少一條。
    """
    css = _css(_build_page(_series()))
    for media in (None, "@media(max-width:760px)"):
        decl = _rule(css, ".rules-dlg", media)
        assert "max-width" in decl, (
            f"{media or '桌機'} 那一條沒有自己寫 max-width，"
            "會被瀏覽器預設的 calc(100% - 6px - 2em) 夾住"
        )


def test_手機上是貼著底的抽屜():
    """置中的視窗在直式螢幕上上下各留一大塊死白，而且離拇指最遠。

    高度要寫兩次，第二次用 `dvh`：`vh` 量的是網址列收起來之後那個比較大的
    高度，只寫 `vh` 的抽屜在網址列還在的時候底部有一截在畫面外。
    """
    css = _css(_build_page(_series()))
    decl = _rule(css, ".rules-dlg", "@media(max-width:760px)")
    assert "inset:auto 0 0 0" in decl, f"手機上不是貼著底：{decl}"
    assert "dvh" in decl, f"高度沒有用 dvh，網址列還在的時候底部會被切掉：{decl}"


def test_關閉鈕手指按得到():
    """36×36。手機上這顆是唯一的出路（沒有 Esc），小於 36 按不準。"""
    css = _css(_build_page(_series()))
    decl = _rule(css, ".rules-x")
    for prop in ("min-width", "min-height"):
        m = re.search(re.escape(prop) + r":(\d+)px", decl)
        assert m and int(m.group(1)) >= 36, f"{prop} 不足 36px：{decl}"


# ────────────────────────────────────────────────────────────────
# 行為（在 node 裡跑真正送到瀏覽器的那段 JS）
# ────────────────────────────────────────────────────────────────

#: 只有開關那幾條用得到的最小 DOM。`REG` 是 getElementById 的查表，
#: 每個假元素自己記下掛到它身上的監聽器，測試再手動「派發」事件。
PRELUDE = r"""
var REG = {};
var DOCL = {};
function mkEl(id, cls) {
  return {
    id: id || '', className: cls || '', open: false, _L: {}, _attr: {},
    addEventListener: function (t, f) { (this._L[t] = this._L[t] || []).push(f); },
    fire: function (t, ev) {
      var self = this;
      (this._L[t] || []).forEach(function (f) { f(ev || {target: self}); });
    },
    setAttribute: function (k, v) { this._attr[k] = v; if (k === 'open') this.open = true; },
    removeAttribute: function (k) { delete this._attr[k]; if (k === 'open') this.open = false; },
    querySelector: function (s) { return (this._q || {})[s] || null; },
    querySelectorAll: function () { return []; },
  };
}
var document = {
  getElementById: function (id) { return REG[id] || null; },
  addEventListener: function (t, f) { (DOCL[t] = DOCL[t] || []).push(f); },
  fire: function (t, ev) { (DOCL[t] || []).forEach(function (f) { f(ev || {}); }); },
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  documentElement: { style: { setProperty: function () {} } },
};
var window = {
  addEventListener: function () {},
  matchMedia: function () { return { matches: false, addListener: function () {} }; },
  innerWidth: 1400, innerHeight: 900,
};
var navigator = { userAgent: 'node' };
var console = { error: function () {}, warn: function () {}, log: function () {} };
var localStorage = {
  getItem: function () { return null; }, setItem: function () {}, removeItem: function () {},
};
function requestAnimationFrame(cb) { return 0; }
var Plotly = { react: function () { return Promise.resolve(); }, relayout: function () {} };

/* `modern` = 這個假瀏覽器認不認得 <dialog>.showModal()。 */
function buildDom(modern) {
  var btn = mkEl('rules-btn', 'rules-btn');
  var x = mkEl('', 'rules-x');
  var dlg = mkEl('rules', 'rules-dlg');
  dlg._q = {'.rules-x': x};
  if (modern) {
    dlg.showModal = function () { this.open = true; this.modal = true; };
    dlg.close = function () { this.open = false; this.modal = false; };
  }
  REG['rules-btn'] = btn; REG['rules'] = dlg;
  return {btn: btn, x: x, dlg: dlg};
}
"""


def _run(driver):
    html = _build_page(_series())
    js = PRELUDE + _page_js(html) + "\n" + driver
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drive.js")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(js)
        run = subprocess.run([shutil.which("node"), p], check=False,
                             capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, f"node 跑不起來：\n{run.stderr[-3000:]}"
    return json.loads(run.stdout)


@needs_node
def test_按燈泡打開_按叉關掉():
    """而且是 DOMContentLoaded 把它接上的——沒有那一步，按鈕是顆死鈕。"""
    got = _run(r"""
var D = buildDom(true);
var out = {before: D.dlg.open};
document.fire('DOMContentLoaded');
D.btn.fire('click');
out.afterClick = D.dlg.open;
out.usedShowModal = !!D.dlg.modal;
D.x.fire('click');
out.afterClose = D.dlg.open;
process.stdout.write(JSON.stringify(out));
""")
    assert got["before"] is False
    assert got["afterClick"] is True, "按了燈泡視窗沒打開"
    assert got["usedShowModal"] is True, "沒走 showModal()，等於放棄 top layer"
    assert got["afterClose"] is False, "按了✕視窗沒關掉"


@needs_node
def test_點遮罩會關掉_點內容不會():
    """遮罩的點擊事件 target 就是 `<dialog>` 自己；點到內容時 target 是子元素。

    分不清楚的症狀很煩：捲動說明文字的時候手指一放開視窗就關了。
    """
    got = _run(r"""
var D = buildDom(true);
document.fire('DOMContentLoaded');
D.btn.fire('click');
var out = {opened: D.dlg.open};
D.dlg.fire('click', {target: mkEl('', 'tipbox')});   /* 點在內容上 */
out.afterContentClick = D.dlg.open;
D.dlg.fire('click', {target: D.dlg});                /* 點在遮罩上 */
out.afterBackdropClick = D.dlg.open;
process.stdout.write(JSON.stringify(out));
""")
    assert got["opened"] is True
    assert got["afterContentClick"] is True, "點內容就把視窗關掉了"
    assert got["afterBackdropClick"] is False, "點遮罩關不掉"


@needs_node
def test_沒有_showModal_的瀏覽器退回_open_屬性():
    """退回那條路沒有原生的 Esc，所以自己補一個；CSS 那邊已經是 position:fixed。"""
    got = _run(r"""
var D = buildDom(false);
document.fire('DOMContentLoaded');
D.btn.fire('click');
var out = {opened: D.dlg.open, attr: D.dlg._attr.open !== undefined};
document.fire('keydown', {key: 'a'});
out.afterOtherKey = D.dlg.open;
document.fire('keydown', {key: 'Escape'});
out.afterEsc = D.dlg.open;
process.stdout.write(JSON.stringify(out));
""")
    assert got["opened"] is True, "舊瀏覽器上按燈泡沒反應"
    assert got["attr"] is True, "沒有用 open 屬性"
    assert got["afterOtherKey"] is True, "按到別的鍵就關掉了"
    assert got["afterEsc"] is False, "舊瀏覽器上 Esc 關不掉（那條路沒有原生的）"


@needs_node
def test_按兩次燈泡不會開兩次():
    """`showModal()` 對一個已經開著的 dialog 會丟 InvalidStateError。

    真的瀏覽器上這會變成一個紅色的 console 錯誤加上視窗沒反應。
    """
    got = _run(r"""
var D = buildDom(true);
D.dlg.showModal = function () {
  if (this.open) throw new Error('InvalidStateError');
  this.open = true; this.modal = true;
};
document.fire('DOMContentLoaded');
D.btn.fire('click');
var err = null;
try { D.btn.fire('click'); } catch (e) { err = e.message; }
process.stdout.write(JSON.stringify({open: D.dlg.open, err: err}));
""")
    assert got["err"] is None, f"開著的時候又叫了一次 showModal()：{got['err']}"
    assert got["open"] is True
