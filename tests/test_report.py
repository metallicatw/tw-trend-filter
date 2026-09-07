"""互動報告的組裝測試。不碰網路，用假資料餵進 build_interactive_html。

這裡守的是三件會靜靜壞掉的事：

1. **個股頁連結**——`--link-base` 傳了卻沒出現在 HTML 裡，讀者只會覺得
   「這個網站就是沒有那個連結」，不會來回報。
2. **CDN 與內嵌二選一**——搞反了就是每天讓每位讀者多下載 3.5 MB，或者
   本機版離線打不開。兩種都不會報錯。
3. **K 線裁切**——裁在算指標之前的話，圖上前六十根的 60MA 會是斷的。
"""

import datetime
import os
import re

import numpy as np
import pandas as pd

from tw_trend_filter.pipeline import build_interactive_html, compute_bollinger


def _fake_result(code='2330', name='台積電', days=900):
    """造一檔走勢平滑上揚的假股票，欄位和 screen_stock 回傳的一致。"""
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


def _build(tmp_path, **kw):
    path = build_interactive_html(
        [_fake_result()], '2026-09-04', str(tmp_path),
        datetime.datetime(2026, 9, 4, 15, 30), **kw)
    assert path, 'build_interactive_html 回傳 None'
    return open(path, encoding='utf-8').read()


#: 側欄那顆 ↗ 是 Python 端組出來的，沒有連結時整段不會出現。
#: 不能拿 class 名去找——CSS 裡永遠有一份 `.nb-ext` 的定義，那樣的測試
#: 對著一份沒有連結的報告也會過。
SIDEBAR_LINK = 'class="nb-ext" href='

#: 圖表區那顆是前端畫的，所以 JS 樣板一定在 HTML 裡；真正決定畫不畫的是
#: INFO 陣列裡那個 url 欄位。
def _has_badge_link(html, url):
    return f'"url": "{url}"' in html


def test_link_base_變成每一檔的個股頁連結(tmp_path):
    html = _build(tmp_path, link_base='https://example.org/stock')
    # 側欄那顆和圖表區那顆，兩個都要在。
    assert SIDEBAR_LINK in html
    assert 'https://example.org/stock/2330.html' in html
    assert _has_badge_link(html, 'https://example.org/stock/2330.html')


def test_側欄連結不會順便切換圖表(tmp_path):
    # 連結在 onclick="showChart(i)" 的卡片裡面，沒擋住冒泡的話點它會
    # 連帶切走圖表，讀者從新分頁回來時看的是另一檔。
    html = _build(tmp_path, link_base='https://example.org/stock')
    assert 'event.stopPropagation()' in html


def test_沒給_link_base_就完全不畫連結(tmp_path):
    html = _build(tmp_path, link_base='')
    assert SIDEBAR_LINK not in html
    assert _has_badge_link(html, '')          # url 是空的 → 前端不畫那顆
    assert 'example.org' not in html


def test_結尾斜線不會生出雙斜線(tmp_path):
    html = _build(tmp_path, link_base='https://example.org/stock/')
    assert 'https://example.org/stock/2330.html' in html
    assert 'stock//2330' not in html


#: plotly.js 的原始碼裡本來就有 'cdn.plot.ly' 這串字（它預設的資源路徑），
#: 所以判斷內嵌與否要看 <script src=>，不能看網域字串有沒有出現。
CDN_TAG = ('<script src="https://cdnjs.cloudflare.com/ajax/libs/'
           'plotly.js/2.35.3/plotly.min.js"')


def test_cdn_模式不內嵌_plotly(tmp_path):
    html = _build(tmp_path, plotly_cdn=True)
    assert CDN_TAG in html
    # 內嵌的話這份 HTML 會超過 3 MB。
    assert len(html) < 3_000_000


def test_離線模式把_plotly_內嵌進來(tmp_path):
    html = _build(tmp_path, plotly_cdn=False)
    assert CDN_TAG not in html
    assert len(html) > 3_000_000


def test_裁切之後_60ma_仍然是完整的一條線(tmp_path):
    # chart_years=1 會把 900 天裁到約 380 天。指標若是裁完才算，
    # 最前面 59 個點會是 null，圖上就是一截斷掉的線。
    html = _build(tmp_path, chart_years=1)
    fig = re.search(r'<script type="application/json" id="fig-0">(.*?)</script>',
                    html, re.S)
    assert fig, '找不到圖表資料'
    assert 'null' not in fig.group(1), '裁切後出現 null，指標是在裁切之後才算的'


def test_裁切真的讓檔案變小(tmp_path):
    long_html  = _build(tmp_path, chart_years=5)
    short_html = _build(tmp_path, chart_years=1)
    assert len(short_html) < len(long_html)


def test_excel_連結有給才出現(tmp_path):
    assert 'Excel' not in _build(tmp_path, excel_url='')
    html = _build(tmp_path, excel_url='https://example.org/runs/1')
    assert 'https://example.org/runs/1' in html
    # 30 天這件事要寫在連結旁邊，不能讓人點下去才知道過期了。
    assert '30 天' in html


def test_drive_的連結不寫_30_天(tmp_path):
    """「30 天內」是 artifact 的保留期限，不是 Drive 的。

    一句寫死的「30 天內」掛在一個永久連結旁邊，比不寫糟——它會讓人以為那份
    檔案會消失，而它不會。
    """
    html = _build(tmp_path, excel_url='https://drive.google.com/drive/folders/abc123')
    assert 'https://drive.google.com/drive/folders/abc123' in html
    assert '30 天' not in html
    assert 'Excel 報表' in html


def test_有_drive_資料夾就用它_沒有才退回執行頁面(monkeypatch=None):
    """報告是先產生、後上傳的，所以連結指的是**資料夾**——資料夾的網址在產生
    報告的當下就知道，某一天那個檔案的 id 要等上傳完才知道。"""
    import os

    from tw_trend_filter.__main__ import _env_excel_url

    keys = ('GDRIVE_FOLDER_ID', 'GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID')
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        # 兩個都沒有 → 沒有連結，報告上不會出現那顆按鈕
        assert _env_excel_url() == ''

        os.environ['GITHUB_REPOSITORY'] = 'a/b'
        os.environ['GITHUB_RUN_ID'] = '99'
        assert _env_excel_url() == 'https://github.com/a/b/actions/runs/99'

        # Drive 優先
        os.environ['GDRIVE_FOLDER_ID'] = 'FOLDER'
        assert _env_excel_url() == 'https://drive.google.com/drive/folders/FOLDER'
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_沒有憑證就跳過上傳而不是失敗():
    """fork 出去的人不必先去申請一組 Google 憑證才跑得動排程。"""
    import subprocess
    import sys

    env = dict(os.environ) if False else {}
    got = subprocess.run(
        [sys.executable, 'scripts/upload_to_drive.py', 'whatever.xlsx'],
        capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'},
    )
    assert got.returncode == 0, got.stderr
    assert '跳過上傳' in got.stdout


def test_沒有任何標的時不產生檔案(tmp_path):
    assert build_interactive_html([], '2026-09-04', str(tmp_path)) is None


# ─────────────────────────────────────────────────────────────────────────
# 切換個股要快。三件事各自都不會讓任何東西壞掉，只會讓桌機上每切一檔就卡
# 兩三秒——而那種問題沒有測試守著，下次有人「順手」改回去就回來了。
#
# 共通的成本：一次 Plotly.relayout 會把五百根 K 棒整個重畫（candlestick
# 的每一根都是一條 path）。所以規則是：能不 relayout 就不 relayout。
# ─────────────────────────────────────────────────────────────────────────

def test_預設視窗和它的_y_軸範圍直接寫在圖裡(tmp_path):
    """畫出來就要是最終樣子，不能畫完再 relayout 三次調成最終樣子。"""
    import json

    html = _build(tmp_path)
    fig = json.loads(
        re.search(r'id="fig-0">(.*?)</script>', html, re.S).group(1).replace(r"<\/", "</")
    )
    layout = fig["layout"]
    assert layout["xaxis2"].get("range"), "X 軸沒有預設範圍，前端就得自己按一次按鈕"
    assert layout["yaxis"].get("range"), "Y 軸沒有預設範圍"
    assert layout["yaxis"].get("autorange") is False
    assert layout["yaxis2"]["range"][0] == 0, "量圖要以 0 為底"

    # 而且那個範圍要真的是近三個月，不是整段歷史。
    lo, hi = layout["xaxis2"]["range"]
    assert (
        __import__("datetime").date.fromisoformat(hi)
        - __import__("datetime").date.fromisoformat(lo)
    ).days < 130


def test_切回已經畫過的圖不重新套用預設範圍(tmp_path):
    """讀者捲到的位置是他自己選的，切走再切回來不該被洗掉——而且重套一次
    就是一次完整 relayout。"""
    html = _build(tmp_path)
    switch = html[html.index("function showChart"):]
    switch = switch[: switch.index("\nwindow.addEventListener")]
    already = switch[switch.rindex("} else {"):]
    assert ".click()" not in already, "切回已畫過的圖時又去模擬點了一次時間範圍按鈕"


def test_尺寸沒變就不重新排版(tmp_path):
    html = _build(tmp_path)
    fit = html[html.index("function fitPlotSize"):]
    fit = fit[: fit.index("\n}")]
    assert "_twW" in fit and "return" in fit, "fitPlotSize 每次都無條件 relayout"


def test_十字線不透過_plotly_畫(tmp_path):
    """滑鼠橫著掃過去一秒可以跨三、四十天。每一天一次 relayout 的版本，在
    桌機上就是連續三、四十次整張圖重畫——手機沒有 hover，所以只有桌機卡。"""
    html = _build(tmp_path)
    draw = html[html.index("function drawXLine"):]
    draw = draw[: draw.index("\nfunction clearXLine")]
    assert "Plotly.relayout" not in draw, "十字線又改回用 shape + relayout 了"
    assert "style.left" in draw, "十字線應該是移動一個絕對定位的元素"
    # 蓋在圖上的東西如果會吃滑鼠事件，plotly 就收不到 hover，線反而不會動。
    assert "pointer-events:none" in html


def test_日期只解析一次(tmp_path):
    """每次縮放都把五百個日期字串重新 parse 一遍，乘上八條線就是四千次，
    而那五百個日期從頭到尾都是同一批。"""
    html = _build(tmp_path)
    fn = html[html.index("function computeVisibleYRange"):]
    fn = fn[: fn.index("\n}")]
    assert "_twMs" in fn, "沒有把日期→毫秒的結果存起來"


# ─────────────────────────────────────────────────────────────────────────
# plotly 的載入
#
# 這一組存在的理由很具體：cdnjs 的版本清單裡有 2.35.2，但那一版**一個檔案都
# 沒有**，所以 `<script src>` 回 404——而 404 的 script 不會報錯，只會讓
# `Plotly` 變成 undefined，然後每一張圖都是一塊空白。它上線過一次。
# ─────────────────────────────────────────────────────────────────────────

def test_cdn_版本要指到真的存在的檔案(tmp_path):
    """版本號寫錯的症狀是「圖表一片空白」，沒有任何錯誤訊息。

    這裡只驗**寫下來的是哪一版**。真的去打那個網址是 CI 的事（要網路），
    而那一步問的是同一個常數，不是拿 grep 去原始碼裡撈。
    """
    from tw_trend_filter.pipeline import PLOTLY_CDN

    html = _build(tmp_path, plotly_cdn=True)
    assert "plotly.js/2.35.3/plotly.min.js" in PLOTLY_CDN, (
        "cdnjs 上 2.35.2 是空的（版本清單有、檔案沒有），只有 2.35.3 拿得到"
    )
    assert PLOTLY_CDN in html, "產出的 HTML 用的不是那個常數"


def test_ci_那一步問的是程式在用的那個值():
    """守門的檢查如果是靠**重新解析原始碼**拿到要檢查的東西，它就有自己的
    一份 bug——而那份 bug 的症狀是「檢查通過」。

    第一版是 `grep -o 'https://...'` 去 pipeline.py 裡撈網址，而網址在原始碼
    裡是跨兩個字串常值寫的（`'.../libs/'` 接 `'plotly.js/...'`），所以 grep
    一輩子都撈不到。它撈到空字串、curl 收到空字串、報
    「Malformed input to a URL function」——那一次是當場紅的，算幸運；
    如果 curl 對空字串回 0，這個檢查會永遠綠燈而且守著空氣。
    """
    from pathlib import Path as _P

    ci = (_P(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text("utf-8")
    assert "確認 plotly 的 CDN 網址拿得到" in ci, "那一步不見了"
    assert "from tw_trend_filter.pipeline import" in ci, "又改回用 grep 撈原始碼了"
    # 整份 workflow 裡都不該再有 grep 撈網址那一招。
    assert "grep -o 'https://" not in ci
    # 退路也要驗——它掛掉的時候沒有第三條路了。
    assert "PLOTLY_CDN_FALLBACK" in ci


def test_cdn_掛掉要退回另一個來源(tmp_path):
    """CDN 掛掉、版本被撤、公司防火牆擋掉 cdnjs——三件事的症狀一模一樣，
    而且都不會有錯誤訊息。所以退路不是保險，是必要的。"""
    html = _build(tmp_path, plotly_cdn=True)
    assert "onerror=" in html, "沒有退路"
    assert "cdn.plot.ly" in html, "退路沒有指到另一個來源"
    # 退路自己也失敗的話不要無限迴圈。
    assert "this.onerror=null" in html


def test_圖表函式庫沒載入要說出來(tmp_path):
    """一塊空白看起來像「還在載入」。使用者回報的會是「圖跑不出來」，
    而那句話沒辦法告訴任何人該修哪裡。"""
    html = _build(tmp_path)
    assert "typeof Plotly === 'undefined'" in html
    seg = html[html.index("typeof Plotly === 'undefined'"):][:900]
    assert "plotly.js" in seg, "訊息裡沒有說是哪個函式庫"


def test_手機上圖例只留帶數值的那幾項(tmp_path):
    """375px 寬的螢幕上，七項圖例會折成四行、87px 高，而上留白只有 72px——
    它會壓在 K 線圖最上面那一段上。

    縮字沒有用（實測還是 87px，plotly 每一列有最小高度），要減的是**項數**。
    留下帶數值的三個，其餘四個在圖上本來就看得出是什麼。
    """
    html = _build(tmp_path)
    fn = html[html.index("function tuneForNarrow"):]
    fn = fn[: fn.index("\n}")]
    assert "showlegend = false" in fn, "沒有減少圖例項數"
    assert "20MA|60MA|建議停損" in fn, "留下的不是帶數值的那三個"
    # 上留白要大於量出來的圖例底端（60px）。
    assert "t: 68" in fn
