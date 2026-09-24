"""報告頁上的三件事（2026-09-23 使用者的要求）。

1. **〔💡 預設篩選條件〕整個拿掉**，內容併進外層網站〔趨勢×六大×報酬〕標題旁
   那顆燈泡。內容仍然在這裡產生（四道門檻的數字只有這邊知道），放在一個
   `<template id="tf-rules">` 裡：不會被畫出來，由外層網站建站時抽出去。
2. **卡片最底下兩顆連結鈕**：〔六大 2.17 ↗〕→ 個股頁 `#six`（六大財務指標評等），
   〔報酬/風險 1.77 ↗〕→ `#eps`（EPS預估與估價）。手機與桌機同一個位置；
   原本那顆〔六大↗〕拿掉。
3. **圖表區那顆出口**改叫「6168詳細資訊↗」，手機上和量比／布林寬／ATR 同一列。

另外守桌機上圖的高度下限——「壓太扁平了，看不出變化」。
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

from test_chart_js import _build_page, _series
from test_snapshot import PRELUDE, _snap

from tw_trend_filter.pipeline import DEFAULT_RULES, _live_block

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="這台機器沒有 node。CI 的 ubuntu runner 內建 node，那裡會跑到。",
)

MOBILE = "@media(max-width:760px)"


def _css(html):
    m = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    assert m, "頁面上沒有 <style>"
    return m.group(1)


def _split_media(css):
    """(桌機那一段, 手機那一段)。這份樣式表裡的 @media 沒有巢狀。"""
    i = css.find(MOBILE)
    assert i >= 0
    j = css.find("}}", i)
    return css[:i], css[i:j + 1]


PAGE = _build_page(_series())
DESK, MOB = _split_media(_css(PAGE))


# ── 1. 預設篩選條件 ────────────────────────────────────────────────

def test_預設篩選條件的按鈕與視窗都拿掉了():
    for gone in ('id="rules-btn"', '<dialog', 'class="rules-x"', "rules-dlg",
                 "rulesOpen", "bindRules", ">預設篩選條件<"):
        assert gone not in PAGE, f"報告頁上還有 {gone}"


def test_內容還在_放在外層網站抽得出去的地方():
    """外層網站用 `<template id="tf-rules">…</template>` 這個樣子去抽。

    這是兩個 repo 之間的約定：tw-six-metrics 的 `trend_rules_html()` 找的就是
    它。改了名字或拿掉，那邊的燈泡會退回一段寫死的舊說明——不會報錯。
    """
    got = re.findall(r'<template id="tf-rules">(.*?)</template>', PAGE, re.S)
    assert len(got) == 1, f"tf-rules 應該剛好一份，找到 {len(got)} 份"
    body = got[0]
    for label, _text in DEFAULT_RULES.describe():
        assert label in body, f"〔{label}〕沒有在裡面"
    assert "3 × ATR(14)" in body and "停損" in body
    # 要被原樣塞進另一個網站的燈泡裡，所以不能帶腳本、不能巢狀 template。
    assert "<script" not in body and "<template" not in body


def test_沒有快照也照樣帶著那份內容():
    html = _live_block(DEFAULT_RULES)
    assert '<template id="tf-rules">' in html
    assert 'id="live"' not in html


# ── 2. 卡片最底下兩顆連結鈕 ───────────────────────────────────────

def _card_html(link_base, **snap):
    """在 node 裡跑真正的 tfCard，回傳那張卡片的 HTML。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("本機沒有 node；這一支會在 CI 上跑")
    block = _live_block(DEFAULT_RULES, snapshots=[_snap(**snap)], link_base=link_base)
    head, _, tail = block.rpartition("<script>")
    js, _, _ = tail.partition("</script>")
    assert "function tfCard" in js
    driver = (
        "const rows = " + json.dumps(json.loads(
            re.search(r'id="tf-snap">(.*?)</script>', block, re.S).group(1))) + ";\n"
        "process.stdout.write(tfCard(rows[0], ['黃金交叉'], 0, ''));\n"
    )
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "card.js")
        with open(src, "w", encoding="utf-8") as f:
            f.write(PRELUDE + "\n" + js + "\n" + driver)
        r = subprocess.run([node, src], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    return r.stdout


@needs_node
def test_六大與報酬風險是連到個股頁分頁的按鈕():
    html = _card_html("https://x.test/stock", code="6168", six=2.17, rr=1.77)
    assert 'href="https://x.test/stock/6168.html#six"' in html, "六大沒有連到 #six"
    assert 'href="https://x.test/stock/6168.html#eps"' in html, "報酬/風險沒有連到 #eps"
    assert html.count('class="nb-lnk"') == 2
    # 按鈕在卡片裡：不擋下冒泡的話，點按鈕會同時觸發「選這一檔」。
    # 三顆：六大、報酬/風險、收盤價旁邊的 Yahoo 技術分析（2026-09-24）
    assert html.count("event.stopPropagation()") == 3
    assert "2.17" in html and "1.77" in html


@needs_node
def test_收盤價旁邊有Yahoo技術分析圖示():
    """點圖示開 Yahoo 那一檔的〔技術分析〕；卡片本身點下去照舊是開這一頁的圖。"""
    html = _card_html("https://x.test/stock", code="6168", six=2.17, rr=1.77)
    assert 'href="https://tw.stock.yahoo.com/quote/6168/technical-analysis"' in html
    yf = html[html.index('class="nb-yf"'):]
    yf = yf[:yf.index("</a>")]
    assert 'target="_blank"' in yf and "event.stopPropagation()" in yf
    # 圖示緊貼在收盤價後面（同一個 .nb-px 裡）
    px = html[html.index('class="nb-px"'):]
    assert px.index('class="nb-close') < px.index('class="nb-yf"') < px.index("</span></div>")


@needs_node
def test_兩顆按鈕在卡片最底下_舊的六大箭頭拿掉了():
    html = _card_html("https://x.test/stock", code="6168", six=2.17, rr=1.77)
    assert "六大↗" not in html and "nb-ext" not in html
    assert html.index('class="nb-tagrow"') < html.index('class="nb-cross"'), (
        "六大／報酬風險不在觸發訊號底下——桌機和手機的位置又不一樣了"
    )
    assert html.rstrip().endswith("</div></div>"), "卡片最後一個區塊不是那兩顆按鈕"


@needs_node
def test_沒有個股頁網址就是標籤不是壞連結():
    html = _card_html("", code="6168", six=2.17, rr=1.77)
    # 唯一的連結是收盤價旁邊那顆 Yahoo 技術分析——它不依賴個股頁網址
    assert html.count("<a ") == 1 and '<a class="nb-yf"' in html
    assert html.count('class="nb-lnk"') == 2


@needs_node
def test_無風險與沒有值照樣有按鈕():
    free = _card_html("https://x.test/stock", code="1101", six=None, rr=None, rr_free=True)
    assert "無風險" in free and "#eps" in free
    assert "六大</span> <b>—</b>" in free


def test_卡片按鈕的樣式_桌機並排_手機上下():
    assert re.search(r"\.nb-cross\{display:flex;", DESK)
    assert re.search(r"\.nb-cross\{flex-direction:column", MOB), "手機上兩顆沒有上下排"
    assert ".nb-ext" not in PAGE


# ── 3. 圖表區那顆出口 ─────────────────────────────────────────────

def test_出口改叫詳細資訊():
    assert "'詳細資訊↗</a>'" in PAGE
    assert "'六大財務指標評等 ' + tfEsc(code)" not in PAGE


def test_手機上四顆徽章同一列不折行():
    row = re.search(r"\.badge-row\{([^}]*)\}", MOB)
    assert row and "flex-wrap:nowrap" in row.group(1), "手機上徽章那一列還會折行"


# ── 桌機上圖的高度 ────────────────────────────────────────────────

def test_桌機上圖有一個跟著寬度走的下限():
    """1440×900 的桌機、78vh 的 iframe 裡，圖本來只有 880×431（約 4:1）。"""
    rule = re.search(r"#tf-plot\{([^}]*)\}", DESK)
    assert rule, "桌機上沒有 #tf-plot 的規則"
    decl = rule.group(1)
    assert "cqw" in decl and "min-height" in decl, f"沒有跟著寬度走的下限：{decl}"
    # flex-basis 一定要是 0。auto 會拿圖上一次的高度當起點，#tf-pane 能長高
    # 之後就變成只會變大的棘輪（實測 1240×900 多出 120px 要捲）。
    assert "flex:1 1 0" in decl, f"#tf-plot 的 flex-basis 不是 0：{decl}"
    assert "#chartcol{container-type:inline-size}" in DESK, "cqw 沒有容器可以量"


def test_手機上那個下限歸零():
    rule = re.search(r"#tf-plot\{([^}]*)\}", MOB)
    assert rule and "min-height:0" in rule.group(1), (
        "手機上沒有把桌機那個下限拿掉——圖高會被 440px 撐開"
    )
    assert "flex:0 0 auto" in rule.group(1), "手機上圖高要由 .plot 的 height 決定"
