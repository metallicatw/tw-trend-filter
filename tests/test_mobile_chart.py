"""手機上那張趨勢圖。

## 使用者看到的症狀

> 手機上顯示的趨勢圖完全不行

## 量到的（嵌在〔六大〕網站的 iframe 裡，iframe 高 78vh，無頭瀏覽器）

改之前：

    390x844   iframe 內 366x658   頁首 195px  →  圖 334x213，價格那一格 62px
    360x640   iframe 內 336x499   頁首 219px  →  圖 336x  0
    1400x900  iframe 內 1240x702  頁首 122px  →  圖 908x431（桌機是對的）

改之後：

    390x844   頁首 121px  →  圖 334x432，價格那一格 229px
    360x640   頁首 121px  →  圖 304x396，價格那一格 193px
    430x932   頁首 121px  →  圖 374x479，價格那一格 259px
    1400x900  頁首 122px  →  圖 908x431（沒有動到）

## 為什麼

圖的高度本來是「視窗高度減掉頁首」。那個算法在桌機上對，在手機上是**把圖當成
剩菜**：同一排輸入框在 366px 寬上折成五、六列，頁首因此有 195~219px 高，而
iframe 裡總共只有 658px（小一點的手機 499px）。剩下的分給卡片列、個股抬頭、
時間範圍之後，圖拿到的是兩百像素——扣掉 62px 上留白、再讓成交量那一格分掉三成，
價格那一格只剩六十幾像素。五條線疊在一起、縱軸刻度互相壓字。360 那一台更直接：
整個算成 0。

改成兩件事：

1. **圖先拿到它該有的高度，頁面再去捲。** 高度由寬度決定（118vw，約 4:5），
   夾在 320~520 之間——每一台手機上是同一個形狀，不是「看頁首今天多長」。
2. **手機上輸入框預設收起來。** 收起來的是輸入框，不是功能：〔篩選〕
   〔回到預設〕〔幾檔符合〕和那顆燈泡都留在同一列上。頁首 195 → 121。
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
from test_chart_size import _prelude, needs_node

MOBILE = "@media(max-width:760px)"


def _css():
    m = re.search(r"<style>(.*?)</style>", _build_page(_series()), re.DOTALL)
    assert m, "頁面上沒有 <style>"
    return m.group(1)


CSS = _css()


def _block(css, head):
    """挑出某個 @media 區塊的內容。這份樣式表裡的 @media 沒有巢狀。"""
    i = css.find(head)
    assert i >= 0, f"找不到 {head}"
    j = css.find("}}", i)
    assert j > i, f"{head} 沒有收尾"
    return css[i:j + 1]


MOB = _block(CSS, MOBILE)


def _decl(css, selector):
    found = re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert found, f"找不到規則 {selector}"
    assert len(found) == 1, f"{selector} 出現 {len(found)} 次"
    return re.sub(r"\s+", " ", found[0]).strip()


# ---------------------------------------------------------------------------
# 圖的高度不再是「剩下多少」


def test_手機上的圖有自己的高度():
    """`flex:1 1 auto` ＝ 「把剩下的都給我」。剩下是 0 的時候，圖就是 0。"""
    plot = _decl(MOB, ".plot")
    assert "height:clamp(" in plot, f"手機上的 .plot 沒有自己的高度：{plot}"
    lo, mid, hi = re.search(
        r"height:clamp\(([^,]+),([^,]+),([^)]+)\)", plot).groups()
    assert lo.strip().endswith("px") and int(lo.strip()[:-2]) >= 300, lo
    assert "vw" in mid, f"中間那一項要由**寬度**決定，現在是 {mid}"
    assert "flex:1 1 auto" not in plot, f"還在搶剩下的空間：{plot}"


def test_手機上不再拿視窗高度減頁首():
    """這一條就是「圖 336x0」那個數字的來源。

    `height:calc(100vh - var(--hd-h))` 在頁首 219px、視窗 499px 的時候，
    分給圖的是 280px；再扣掉卡片列和個股抬頭就沒有了。
    """
    main = _decl(MOB, "#main")
    assert "100vh" not in main, f"#main 還在算視窗高度：{main}"
    assert "height:auto" in main, main


def test_手機上頁面要捲得動():
    """`overflow:hidden` ＋ 固定高度 ＝ 放不下的東西直接看不到，而且捲不回來。

    圖拿到固定高度之後，整頁一定比視窗高（實測 803px vs 658px），所以捲動
    不是可有可無的，是這個版面成立的前提。
    """
    body = _decl(MOB, "html,body")
    assert "overflow:hidden" not in body, f"手機上還鎖著捲動：{body}"
    assert "overflow:visible" in body, body
    col = _decl(MOB, "#chartcol")
    assert "overflow-y:auto" not in col, f"圖表欄自己又開了一層捲軸：{col}"


def test_桌機那一套沒有被動到():
    """桌機上「填滿剩餘視窗高度」是對的——量到 908x431，那一版沒有問題。"""
    base = CSS[:CSS.find("@media")]
    assert "height:calc(100vh - var(--hd-h, 46px))" in base
    assert "flex:1 1 auto" in _decl(base, ".plot")


# ---------------------------------------------------------------------------
# 手機上輸入框收起來


def test_收合鈕只在手機上出現():
    assert _decl(CSS[:CSS.find("@media")], ".tf-fold") == "display:none"
    assert "display:inline-flex" in _decl(MOB, ".tf-fold")


def test_收合用class而不是hidden():
    """`[hidden]{display:none}` 是 (0,1,0)，而 `#live .live-fields{display:flex}`
    是 (1,1,0)——`hidden` 會輸，輸入框照樣攤在那裡。

    這個坑這個專案踩過兩次（market-monitor 的 `.mode-toggle-btn[hidden]`、
    tw-six 的 `.star-cell .mv[hidden]`），所以這裡直接寫成測試。
    """
    rule = _decl(MOB, "#live.folded .live-fields")
    assert rule == "display:none", rule
    assert ".live-fields[hidden]" not in CSS


def test_收起來的只有輸入框():
    """〔篩選〕〔回到預設〕〔幾檔符合〕和燈泡要留在畫面上。

    收合把功能一起收掉的話，手機上等於沒有「調門檻」這件事。
    """
    html = _build_page(_series())
    bar = html[html.index('class="live-bar"'):]
    bar = bar[:bar.index("</div>")]
    for must in ("tfApply()", "tfReset()", "live-count"):
        assert must in bar, f"{must} 不在那一列上"
    assert 'id="tf-fold"' in bar
    # 收合的是 .live-fields，而 input 全部還在 DOM 裡——門檻照樣算得到。
    assert 'id="tf-fields"' in html and 'id="f_min_price"' in html


# ---------------------------------------------------------------------------
# 刻度不要互相壓字


@needs_node
def test_手機上刻度要變少():
    """plotly 是按**像素**決定標幾個刻度的，而它算的是桌機的像素。

    366px 寬、價格那一格兩百多像素高的時候，它標出來的縱軸刻度會疊成一團
    ——使用者那張截圖上的「2050」就是「20」和「50」疊在一起。
    """
    got = _tuned(narrow=True)
    assert got["yaxis"]["nticks"] == 5, got["yaxis"]
    assert got["yaxis2"]["nticks"] == 3, got["yaxis2"]
    assert got["xaxis"]["nticks"] == 4, got["xaxis"]


@needs_node
def test_桌機不動刻度():
    got = _tuned(narrow=False)
    assert "nticks" not in got["yaxis"], got["yaxis"]
    assert "nticks" not in (got.get("xaxis") or {}), got.get("xaxis")


@needs_node
def test_手機上兩個直排的縱軸標題還是要拿掉():
    """加刻度那一段是後來插進 tuneForNarrow 的，很容易把原本清標題那兩行蓋掉。"""
    got = _tuned(narrow=True)
    assert got["yaxis"]["title"]["text"] == ""
    assert got["yaxis2"]["title"]["text"] == ""


def _tuned(narrow):
    driver = (
        "var fig = {layout:{yaxis:{title:{text:'價格 (元)'}},"
        "yaxis2:{title:{text:'成交量(張)'}},xaxis:{},xaxis2:{},margin:{}},data:[]};\n"
        "tuneForNarrow(fig);\n"
        "process.stdout.write(JSON.stringify(fig.layout));\n"
    )
    html = _build_page(_series())
    js = _prelude(narrow) + _page_js(html) + "\n" + driver
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drive.js")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(js)
        run = subprocess.run([shutil.which("node"), p], check=False,
                             capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


# ---------------------------------------------------------------------------
# 收合之後要重新量一次圖


@needs_node
def test_收合之後要重新量一次圖():
    """頁首高度變了，plotly 的畫布不會自己知道。

    `fitPlotSize` 有一層「尺寸一樣就跳過」的快取，所以漏掉這一步的症狀是
    「按了收合，圖停在原來的大小，下面空一塊」——看起來像沒反應。
    """
    # 整份 HTML 裡找，不是 `_page_js()`：那個挑的是帶 `tfPass` 的主腳本，
    # 而 `tfFoldToggle` 在〔調整門檻〕那一塊自己的 <script> 裡。
    fn = re.search(r"function tfFoldToggle\(\)\s*\{(.*?)\n      \}",
                   _build_page(_series()), re.DOTALL)
    assert fn, "頁面上沒有 tfFoldToggle"
    body = fn.group(1)
    assert "classList.toggle('folded')" in body, body
    # 要找**真的呼叫**，不是那句 `typeof fitPlotSize === 'function'` 的守門
    # ——只找名字的話，把呼叫整個拿掉、守門留著，測試照樣是綠的（試過）。
    assert re.search(r"fitPlotSize\(\s*a\s*\)", body), f"收合之後沒有重新量圖：{body}"
    assert "aria-expanded" in body, body
