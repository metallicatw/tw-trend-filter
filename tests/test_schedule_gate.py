import re
from pathlib import Path

WF = (Path(__file__).resolve().parents[1] / ".github/workflows/daily.yml").read_text("utf-8")


def test_主班與保險兩班():
    crons = re.findall(r'cron:\s*"([^"]+)"', WF)
    assert crons == ["07 7 * * 1-5", "17 10 * * 1-5"], crons


def test_保險那一班看_report_分支今天發布了沒有():
    assert 'SCHED" != "17 10 * * 1-5"' in WF
    assert "篩選日期：$today" in WF and "date +%Y/%m/%d" in WF
    assert "fetch -q --depth 1" in WF and " report " in WF
    assert "github.io" not in WF.split("  screen:")[0]


def test_報告上真的印著那個字():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_chart_js import _build_page, _series

    assert "篩選日期：" in _build_page(_series())


def test_篩選那個job等gate():
    assert "needs: gate" in WF and "needs.gate.outputs.skip != 'yes'" in WF
    assert re.search(r"^concurrency:\n  group: daily", WF, re.M)


def test_Pages_不放報告頁_只放圖表資料():
    step = WF[WF.index("準備 Pages 內容"):]
    step = step[: step.index("- uses:")]
    assert "cp index.html pages/" not in step
    assert "noindex" in step
