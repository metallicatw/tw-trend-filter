"""保險那一班（2026-09-23）：GitHub 延遲或丟掉 15:07 那一班的時候，18:17 再試一次；
那一班開頭先看 Pages 上的報告是不是今天篩的，是就不重跑。"""
import re
from pathlib import Path

WF = (Path(__file__).resolve().parents[1] / ".github/workflows/daily.yml").read_text("utf-8")


def test_有主班和保險兩班():
    crons = re.findall(r'cron:\s*"([^"]+)"', WF)
    assert crons == ["07 7 * * 1-5", "17 10 * * 1-5"], crons


def test_保險那一班先看今天發布了沒有():
    assert 'SCHED" != "17 10 * * 1-5"' in WF, "gate 比對的 cron 和排程對不上——保險那一班永遠不會跳過"
    assert "篩選日期：$today" in WF
    assert "date +%Y/%m/%d" in WF, "報告上的日期是 2026/09/23 這種寫法"


def test_報告上真的印著那個字():
    """gate 找的字串要是報告真的會印的——改了報告的文字，gate 就永遠找不到。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_chart_js import _build_page, _series

    assert "篩選日期：" in _build_page(_series())


def test_篩選那個job等gate():
    assert "needs: gate" in WF and "needs.gate.outputs.skip != 'yes'" in WF
    assert re.search(r"^concurrency:\n  group: daily", WF, re.M), "沒有共用的鎖，兩班可能一起跑"
