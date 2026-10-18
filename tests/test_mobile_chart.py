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




def test_手機上的圖有自己的高度():
    plot = _decl(MOB, ".plot")
    assert "height:clamp(" in plot, f"手機上的 .plot 沒有自己的高度：{plot}"
    lo, mid, hi = re.search(
        r"height:clamp\(([^,]+),([^,]+),([^)]+)\)", plot).groups()
    assert lo.strip().endswith("px") and int(lo.strip()[:-2]) >= 300, lo
    assert "vw" in mid, f"中間那一項要由**寬度**決定，現在是 {mid}"
    assert "flex:1 1 auto" not in plot, f"還在搶剩下的空間：{plot}"


def test_手機上不再拿視窗高度減頁首():
    main = _decl(MOB, "#main")
    assert "100vh" not in main, f"#main 還在算視窗高度：{main}"
    assert "height:auto" in main, main


def test_手機上頁面要捲得動():
    body = _decl(MOB, "html,body")
    assert "overflow:hidden" not in body, f"手機上還鎖著捲動：{body}"
    assert "overflow:visible" in body, body
    col = _decl(MOB, "#chartcol")
    assert "overflow-y:auto" not in col, f"圖表欄自己又開了一層捲軸：{col}"


def test_桌機那一套沒有被動到():
    base = CSS[:CSS.find("@media")]
    assert "height:calc(100vh - var(--hd-h, 46px))" in base
    assert "flex:1 1 auto" in _decl(base, ".plot")




def test_收合鈕只在手機上出現():
    assert _decl(CSS[:CSS.find("@media")], ".tf-fold") == "display:none"
    assert "display:inline-flex" in _decl(MOB, ".tf-fold")


def test_收合用class而不是hidden():
    rule = _decl(MOB, "#live.folded .live-fields")
    assert rule == "display:none", rule
    assert ".live-fields[hidden]" not in CSS


def test_收起來的只有輸入框():
    html = _build_page(_series())
    bar = html[html.index('class="live-bar"'):]
    bar = bar[:bar.index("</div>")]
    for must in ("tfApply()", "tfReset()", "live-count"):
        assert must in bar, f"{must} 不在那一列上"
    assert 'id="tf-fold"' in bar
    assert 'id="tf-fields"' in html and 'id="f_min_price"' in html




@needs_node
def test_手機上刻度要變少():
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




@needs_node
def test_收合之後要重新量一次圖():
    fn = re.search(r"function tfFoldToggle\(\)\s*\{(.*?)\n      \}",
                   _build_page(_series()), re.DOTALL)
    assert fn, "頁面上沒有 tfFoldToggle"
    body = fn.group(1)
    assert "classList.toggle('folded')" in body, body
    assert re.search(r"fitPlotSize\(\s*a\s*\)", body), f"收合之後沒有重新量圖：{body}"
    assert "aria-expanded" in body, body
