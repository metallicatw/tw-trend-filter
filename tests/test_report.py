
import datetime
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from test_snapshot import PRELUDE

from tw_trend_filter.pipeline import build_interactive_html, compute_bollinger


def _fake_result(code='2330', name='台積電', days=900):
    idx = pd.bdate_range('2022-01-03', periods=days)
    base = np.linspace(100.0, 200.0, days)
    close = pd.Series(base, index=idx)
    df = pd.DataFrame({
        'Open':  close * 0.99,
        'High':  close * 1.02,
        'Low':   close * 0.98,
        'Close': close,
        'Volume': pd.Series(np.full(days, 5_000_000.0), index=idx),
    }, index=idx)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    bmid, bup, bdn, _bw = compute_bollinger(close)
    return {
        'ticker': f'{code}.TW', 'code': code, 'name': name,
        'industry': '半導體業',
        'close': 200.0, 'ma20_last': 195.0, 'ma60_last': 180.0,
        'boll_up_last': 205.0, 'boll_mid_last': 195.0, 'boll_dn_last': 185.0,
        'boll_bw_pct': 10.2, 'vol_today': 6_000_000, 'vol20_avg': 5_000_000,
        'vol_ratio': 1.35, 'amt_M': 900.0, 'atr14': 4.2, 'stop_loss': 187.4,
        'golden_cross': True, 'squeeze': False, 'trigger': '黃金交叉 ｜ 突破20日高點',
        '_df': df, '_ma20': ma20, '_ma60': ma60,
        '_boll_up': bup, '_boll_mid': bmid, '_boll_dn': bdn,
    }


def _fake_snap(code='2330', name='台積電'):
    from tw_trend_filter.pipeline import SNAPSHOT_COLUMNS, SNAPSHOT_DAYS, reward_risk

    snap = {
        'code': code, 'name': name, 'industry': '半導體業',
        'close': 200.0, 'vol20': 5000.0, 'amt20': 9e8,
        'ma20': 195.0, 'ma60': 180.0,
        'cross_ago': 3, 'bw': [0.30] * (SNAPSHOT_DAYS - 1) + [0.05],
        'boll_up': 196.0, 'donchian': 198.0,
        'vol_ratio': 1.35, 'atr14': 4.2,
        'chg': 1.0, 'chg_pct': 0.5,
    }
    snap['trend_ok'] = snap['close'] > snap['ma60'] and snap['ma20'] > snap['ma60']
    snap['brk_boll'] = snap['close'] > snap['boll_up']
    snap['brk_don'] = snap['donchian'] is not None and snap['close'] > snap['donchian']
    snap['six'] = 3.5
    rr, free = reward_risk(320.0, 150.0, snap['close'])
    snap['rr'] = rr if rr is None else round(rr, 6)
    snap['rr_free'] = free
    assert set(snap) == set(SNAPSHOT_COLUMNS), '快照欄位和 SNAPSHOT_COLUMNS 對不上'
    return snap


_WHEN = datetime.datetime(2026, 9, 4, 15, 30)  # noqa: DTZ001（沒有時區的測試固定值）


def _page_path(tmp_path, results=None, **kw):
    from tw_trend_filter.pipeline import DEFAULT_RULES

    kw.setdefault('rules', DEFAULT_RULES)
    kw.setdefault('snapshots', [_fake_snap()])
    path = build_interactive_html(
        [_fake_result()] if results is None else results,
        '2026-09-04', str(tmp_path), _WHEN, **kw)
    return path


def _build(tmp_path, **kw):
    path = _page_path(tmp_path, **kw)
    assert path, 'build_interactive_html 回傳 None'
    return Path(path).read_text(encoding='utf-8')


def _has_badge_link(html, url):
    return f'"url": "{url}"' in html


def test_link_base_變成每一檔的個股頁連結(tmp_path):
    html = _build(tmp_path, link_base='https://example.org/stock')
    assert 'const TF_LINK = "https://example.org/stock"' in html
    assert _has_badge_link(html, 'https://example.org/stock/2330.html')


def test_側欄連結不會順便切換圖表(tmp_path):
    html = _build(tmp_path, link_base='https://example.org/stock')
    assert 'event.stopPropagation()' in html


def test_沒給_link_base_就完全不畫連結(tmp_path):
    html = _build(tmp_path, link_base='')
    assert 'const TF_LINK = ""' in html
    assert _has_badge_link(html, '')
    assert 'example.org' not in html


def test_結尾斜線不會生出雙斜線(tmp_path):
    html = _build(tmp_path, link_base='https://example.org/stock/')
    assert 'const TF_LINK = "https://example.org/stock"' in html
    assert 'https://example.org/stock/2330.html' in html
    assert 'stock//2330' not in html


CDN_TAG = ('<script src="https://cdnjs.cloudflare.com/ajax/libs/'
           'plotly.js/2.35.3/plotly.min.js"')


def test_cdn_模式不內嵌_plotly(tmp_path):
    html = _build(tmp_path, plotly_cdn=True)
    assert CDN_TAG in html
    assert len(html) < 3_000_000


def test_離線模式把_plotly_內嵌進來(tmp_path):
    html = _build(tmp_path, plotly_cdn=False)
    assert CDN_TAG not in html
    assert len(html) > 3_000_000


def test_裁切之後_60ma_仍然是完整的一條線(tmp_path):
    html = _build(tmp_path, chart_years=1)
    ser = re.search(r'<script type="application/json" id="tf-series">(.*?)</script>',
                    html, re.DOTALL)
    assert ser, '找不到數列'
    assert 'null' not in ser.group(1), '裁切後出現 null，指標是在裁切之後才算的'


def test_裁切真的讓檔案變小(tmp_path):
    long_html  = _build(tmp_path, chart_years=5)
    short_html = _build(tmp_path, chart_years=1)
    assert len(short_html) < len(long_html)


def test_報告頁上沒有_excel_連結(tmp_path):
    html = _build(tmp_path)
    assert 'class="dl"' not in html
    assert 'Excel 報表' not in html


def test_排程不再產_Excel_也不裝字型():
    ci = (ROOT / '.github/workflows/daily.yml').read_text('utf-8')
    assert 'upload-artifact' not in ci, 'Excel 的 artifact 還在'
    assert 'fonts-noto-cjk' not in ci, '還在裝只有 Excel 圖才用得到的中文字型'
    assert '.xlsx' not in ci
    step = ci[ci.index('發布全市場快照到 Releases'):]
    step = step[: step.index('- name: 發布報告到 report 分支')]
    assert '--clobber' in step, '同一天重跑會堆出第二個附件'
    assert 'gh release view' in step, '沒有先判斷 release 在不在'
    assert 'github.token' in step
    assert 'GDRIVE' not in ci, 'Drive 的殘留沒清乾淨'


def test_舊的_Excel_旗標照打不會壞():
    from tw_trend_filter import __main__ as m
    ap_text = (ROOT / 'tw_trend_filter/__main__.py').read_text('utf-8')
    for flag in ('--no-excel', '--no-excel-charts', '--excel-url'):
        assert f"'{flag}'" in ap_text
    assert not hasattr(m, '_env_excel_url')


def test_零檔過篩照樣要產出報告(tmp_path):
    from tw_trend_filter.pipeline import DEFAULT_RULES

    path = _page_path(tmp_path, results=[], rules=DEFAULT_RULES)
    assert path, '零檔過篩就不產報告了——那一天網站會整個不更新'
    html = Path(path).read_text(encoding='utf-8')
    assert 'id="tf-snap"' in html, '沒有快照，側欄畫不出任何東西'
    assert 'id="live"' in html and 'function tfPass' in html, '調門檻那一整塊不見了'
    assert '0' in html.split('預設門檻共')[1][:40]


def _live_js(html):
    for body in re.findall(r'<script>(.*?)</script>', html, re.DOTALL):
        if 'function tfPass' in body:
            return body
    raise AssertionError('報告裡找不到含 tfPass 的那一段 <script>')


def test_零檔的那一天_調鬆門檻還是找得到股票(tmp_path):
    import json as _json
    import os as _os
    import shutil as _shutil
    import subprocess as _subprocess
    import tempfile as _tempfile

    from tw_trend_filter.pipeline import DEFAULT_RULES, LIVE_FIELDS, snapshot_row

    node = _shutil.which('node')
    if node is None:
        pytest.skip('本機沒有 node；CI 的 ubuntu runner 有')

    snaps = [_fake_snap(code='2330')]
    path = _page_path(tmp_path, results=[], snapshots=snaps,
                      rules=replace(DEFAULT_RULES, min_price=99999))
    html = Path(path).read_text(encoding='utf-8')

    driver = (
        "const rows = " + _json.dumps([snapshot_row(s) for s in snaps]) + ";\n"
        "const strict = " + _json.dumps(
            {k: (99999 if k == 'min_price' else getattr(DEFAULT_RULES, k))
             for k, *_ in LIVE_FIELDS}) + ";\n"
        "const loose = Object.assign({}, strict, {min_price: 1});\n"
        "process.stdout.write(JSON.stringify({\n"
        "  strict: rows.filter(function (r) { return tfPass(r, strict); }).length,\n"
        "  loose:  rows.filter(function (r) { return tfPass(r, loose); }).length}));\n"
    )
    with _tempfile.TemporaryDirectory() as d:
        src = _os.path.join(d, 'z.js')
        with open(src, 'w', encoding='utf-8') as f:
            f.write(PRELUDE + '\n' + _live_js(html) + '\n' + driver)
        r = _subprocess.run([node, src], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr[-2000:]
    got = _json.loads(r.stdout)
    assert got['strict'] == 0, '這個 fixture 在預設門檻下應該一檔都不過'
    assert got['loose'] >= 1, (
        '調鬆門檻之後還是零檔——那這一頁在零檔的日子確實沒有用，'
        '上面那條測試的理由就不成立了'
    )



def test_預設視窗和它的_y_軸範圍一開始就是對的(tmp_path):
    import json

    from tw_trend_filter.pipeline import _chart_figure, chart_template

    html = _build(tmp_path)
    ser = json.loads(
        re.search(r'id="tf-series">(.*?)</script>', html, re.DOTALL)
        .group(1).replace(r"<\/", "</")
    )
    (s,) = ser.values()
    layout = json.loads(
        __import__("plotly.io", fromlist=["io"]).to_json(_chart_figure(s))
    )["layout"]
    assert layout["xaxis2"].get("range"), "X 軸沒有預設範圍，前端就得自己按一次按鈕"
    assert layout["yaxis"].get("range"), "Y 軸沒有預設範圍"
    assert layout["yaxis"].get("autorange") is False
    assert layout["yaxis2"]["range"][0] == 0, "量圖要以 0 為底"

    lo, hi = layout["xaxis2"]["range"]
    assert (
        __import__("datetime").date.fromisoformat(hi)
        - __import__("datetime").date.fromisoformat(lo)
    ).days < 130

    tpl = chart_template()["layout"]
    for axis in ("xaxis2", "yaxis", "yaxis2"):
        assert "range" not in tpl.get(axis, {}), f"樣板的 {axis} 還留著範圍"

    js = html[html.index("function tfSeriesFig"):]
    js = js[: js.index("\nfunction tfDraw")]
    for axis in ("xaxis2", "yaxis", "yaxis2"):
        assert f"fig.layout.{axis}.range" in js, f"tfSeriesFig 沒有設 {axis} 的範圍"


def test_換一檔就回到那一檔自己的預設視窗(tmp_path):
    html = _build(tmp_path)
    draw = html[html.index("function tfDraw("):]
    draw = draw[: draw.index("\n}")]
    assert "Plotly.react" in draw, "又改回 newPlot 了，換一檔要把整張圖拆掉重蓋"
    assert ".click()" not in draw, "又去模擬點了一次時間範圍按鈕"
    assert "rba" in draw, "換一檔的時候時間範圍按鈕沒有跟著回到預設的那一顆"


def test_尺寸沒變就不重新排版(tmp_path):
    html = _build(tmp_path)
    fit = html[html.index("function fitPlotSize"):]
    fit = fit[: fit.index("\n}")]
    assert "_twW" in fit and "return" in fit, "fitPlotSize 每次都無條件 relayout"


def test_十字線不透過_plotly_畫(tmp_path):
    html = _build(tmp_path)
    draw = html[html.index("function drawXLine"):]
    draw = draw[: draw.index("\nfunction clearXLine")]
    assert "Plotly.relayout" not in draw, "十字線又改回用 shape + relayout 了"
    assert "style.left" in draw, "十字線應該是移動一個絕對定位的元素"
    assert "pointer-events:none" in html


def test_日期只解析一次(tmp_path):
    html = _build(tmp_path)
    fn = html[html.index("function computeVisibleYRange"):]
    fn = fn[: fn.index("\n}")]
    assert "_twMs" in fn, "沒有把日期→毫秒的結果存起來"



def test_cdn_版本要指到真的存在的檔案(tmp_path):
    from tw_trend_filter.pipeline import PLOTLY_CDN

    html = _build(tmp_path, plotly_cdn=True)
    assert "plotly.js/2.35.3/plotly.min.js" in PLOTLY_CDN, (
        "cdnjs 上 2.35.2 是空的（版本清單有、檔案沒有），只有 2.35.3 拿得到"
    )
    assert PLOTLY_CDN in html, "產出的 HTML 用的不是那個常數"


def test_ci_那一步問的是程式在用的那個值():
    from pathlib import Path as _P

    ci = (_P(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text("utf-8")
    assert "確認 plotly 的 CDN 網址拿得到" in ci, "那一步不見了"
    assert "from tw_trend_filter.pipeline import" in ci, "又改回用 grep 撈原始碼了"
    assert "grep -o 'https://" not in ci
    assert "PLOTLY_CDN_FALLBACK" in ci


def test_cdn_掛掉要退回另一個來源(tmp_path):
    html = _build(tmp_path, plotly_cdn=True)
    assert "onerror=" in html, "沒有退路"
    assert "cdn.plot.ly" in html, "退路沒有指到另一個來源"
    assert "this.onerror=null" in html


def test_圖表函式庫沒載入要說出來(tmp_path):
    html = _build(tmp_path)
    assert "typeof Plotly === 'undefined'" in html
    seg = html[html.index("typeof Plotly === 'undefined'"):][:900]
    assert "plotly.js" in seg, "訊息裡沒有說是哪個函式庫"


def test_手機上圖例只留帶數值的那幾項(tmp_path):
    html = _build(tmp_path)
    fn = html[html.index("function tuneForNarrow"):]
    fn = fn[: fn.index("\n}")]
    assert "showlegend = false" in fn, "沒有減少圖例項數"
    assert "20MA|60MA|建議停損" in fn, "留下的不是帶數值的那三個"
    m = re.search(r"\bt:\s*(\d+)", fn)
    assert m and 55 <= int(m.group(1)) <= 80, f"上留白 {m and m.group(1)}"



def test_missing_bars_become_null_not_zero():
    import json
    import math as _math
    import re

    src = (Path(__file__).resolve().parents[1]
           / "tw_trend_filter" / "pipeline.py").read_text(encoding="utf-8")
    body = re.search(r"    def sf\(v, d=2\):.*?\n        return round\(f, d\)",
                     src, re.DOTALL)
    assert body, "找不到 sf()，它被改名或改寫了"
    ns = {"_math": _math}
    exec("def _w():\n" + body.group(0) + "\n    return sf\nsf = _w()", ns)  # noqa: S102
    sf = ns["sf"]

    assert sf(12.345) == 12.35
    for bad in (float("nan"), float("inf"), float("-inf"), None, "", "abc"):
        assert sf(bad) is None, f"{bad!r} 應該是 None，拿到 {sf(bad)!r}"
    assert json.dumps([sf(1.0), sf(float("nan")), sf(2.0)]) == "[1.0, null, 2.0]"
