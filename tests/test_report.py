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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


def _fake_snap(code='2330', name='台積電'):
    """和 `_fake_result` 同一檔的快照。

    側欄那排卡片是前端從快照畫的，所以**沒有快照就沒有側欄**——這個 helper 存在
    是為了讓每一條報告測試都拿到一份完整的頁面，而不是一份沒有側欄的。
    """
    from tw_trend_filter.pipeline import (
        SNAPSHOT_COLUMNS, SNAPSHOT_DAYS, reward_risk)

    snap = {
        'code': code, 'name': name, 'industry': '半導體業',
        'close': 200.0, 'vol20': 5000.0, 'amt20': 9e8,
        'ma20': 195.0, 'ma60': 180.0,
        'cross_ago': 3, 'bw': [0.30] * (SNAPSHOT_DAYS - 1) + [0.05],
        'boll_up': 196.0, 'donchian': 198.0,
        'vol_ratio': 1.35, 'atr14': 4.2,
        'chg': 1.0, 'chg_pct': 0.5,
    }
    # ②④ 的判定結果，和 `screen_stock` 一樣**從價格推出來**。
    # 寫死 True 的話，改了上面那幾個價格卻忘了改這裡，快照就自相矛盾了。
    snap['trend_ok'] = snap['close'] > snap['ma60'] and snap['ma20'] > snap['ma60']
    snap['brk_boll'] = snap['close'] > snap['boll_up']
    snap['brk_don'] = snap['donchian'] is not None and snap['close'] > snap['donchian']
    # 第五關的三格。六大與目標價／下檔價來自 tw-six-metrics，報酬風險比在這邊
    # 用**同一個收盤價**算出來——所以這裡也從 `close` 推，不寫死一個數字。
    snap['six'] = 3.5
    rr, free = reward_risk(320.0, 150.0, snap['close'])
    snap['rr'] = rr if rr is None else round(rr, 6)
    snap['rr_free'] = free
    assert set(snap) == set(SNAPSHOT_COLUMNS), '快照欄位和 SNAPSHOT_COLUMNS 對不上'
    return snap


def _build(tmp_path, **kw):
    from tw_trend_filter.pipeline import DEFAULT_RULES

    kw.setdefault('rules', DEFAULT_RULES)
    kw.setdefault('snapshots', [_fake_snap()])
    path = build_interactive_html(
        [_fake_result()], '2026-09-04', str(tmp_path),
        datetime.datetime(2026, 9, 4, 15, 30), **kw)
    assert path, 'build_interactive_html 回傳 None'
    return open(path, encoding='utf-8').read()


#: 圖表區那顆是前端畫的，所以 JS 樣板一定在 HTML 裡；真正決定畫不畫的是
#: INFO 陣列裡那個 url 欄位。
def _has_badge_link(html, url):
    return f'"url": "{url}"' in html


def test_link_base_變成每一檔的個股頁連結(tmp_path):
    """側欄那顆 ↗ 和圖表區那顆，兩個都要在。

    側欄那排卡片現在由前端從快照畫（門檻可調，Python 在建站時不知道會篩出哪
    幾檔），所以 Python 端能驗的是**送給前端的那個基底網址**。整條路由
    `test_snapshot.py` 的端對端那幾條顧。
    """
    html = _build(tmp_path, link_base='https://example.org/stock')
    assert 'const TF_LINK = "https://example.org/stock"' in html
    assert _has_badge_link(html, 'https://example.org/stock/2330.html')


def test_側欄連結不會順便切換圖表(tmp_path):
    # 連結在 onclick="tfOpen(code)" 的卡片裡面，沒擋住冒泡的話點它會連帶切走
    # 圖表，讀者從新分頁回來時看的是另一檔。
    html = _build(tmp_path, link_base='https://example.org/stock')
    assert 'event.stopPropagation()' in html


def test_沒給_link_base_就完全不畫連結(tmp_path):
    html = _build(tmp_path, link_base='')
    assert 'const TF_LINK = ""' in html       # 空字串 → 前端不畫那顆
    assert _has_badge_link(html, '')
    assert 'example.org' not in html


def test_結尾斜線不會生出雙斜線(tmp_path):
    html = _build(tmp_path, link_base='https://example.org/stock/')
    assert 'const TF_LINK = "https://example.org/stock"' in html
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
    #
    # 看的地方從 `fig-0`（一整張 plotly 圖）換成 `tf-series`（那一檔的數列）：
    # 頁面現在送的是「數列 ＋ 一份共用的樣板」，圖由前端組（見 tfSeriesFig）。
    # 要守的事情一個字都沒變——指標仍然是在**完整**歷史上算完才裁。
    html = _build(tmp_path, chart_years=1)
    ser = re.search(r'<script type="application/json" id="tf-series">(.*?)</script>',
                    html, re.S)
    assert ser, '找不到數列'
    assert 'null' not in ser.group(1), '裁切後出現 null，指標是在裁切之後才算的'


def test_裁切真的讓檔案變小(tmp_path):
    long_html  = _build(tmp_path, chart_years=5)
    short_html = _build(tmp_path, chart_years=1)
    assert len(short_html) < len(long_html)


def test_報告頁上不再有_excel_連結(tmp_path):
    """〔⬇️ Excel 報表〕那顆連結拿掉了。

    這一頁上有的東西 Excel 裡幾乎都有，而且這一頁還多了「自己調門檻」——那是
    Excel 給不了的。Excel 照常每天產、照常發到 Releases，只是不再佔著頁首。

    `excel_url` 這個參數**留著**（呼叫端不必跟著改，而它隨時可能要回來），所以
    這條測試守的是「傳了也不會畫出來」——不然某天有人把那顆連結加回去，而沒有
    人記得當初為什麼拿掉。
    """
    html = _build(tmp_path, excel_url='https://github.com/a/b/releases')
    assert 'https://github.com/a/b/releases' not in html
    # 數的是那顆連結的樣子，不是「Excel」這四個字母——JS 的註解裡有一句「和
    # Excel 那一份同一個排法」，拿字串去找會把它一起找到，然後這條測試永遠紅。
    assert 'class="dl"' not in html
    assert 'Excel 報表' not in html


def test_連結指的是_releases_列表頁(monkeypatch=None):
    """報告是先產生、後上傳的。

    列表頁的網址在產生報告的當下就知道，而今天那個 release 要等上傳完才存在。
    也不能用 `/releases/latest`：一個 release 都還沒有的時候它會 404，而那正是
    第一次跑的時候。
    """
    import os

    from tw_trend_filter.__main__ import _env_excel_url

    keys = ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID')
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        # 不在 Actions 底下 → 沒有連結，報告上不會出現那顆按鈕
        assert _env_excel_url() == ''

        os.environ['GITHUB_REPOSITORY'] = 'a/b'
        assert _env_excel_url() == 'https://github.com/a/b/releases'
        assert 'latest' not in _env_excel_url()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_同一天重跑要覆蓋而不是堆第二個檔():
    """排程一天只跑一次，但人會手動按 Run workflow。同一個 release 底下堆兩個
    一模一樣的檔名，下載的人要自己猜哪個是新的。"""
    ci = (ROOT / '.github/workflows/daily.yml').read_text('utf-8')
    step = ci[ci.index('發布 Excel 到 Releases'):]
    step = step[: step.index('artifact 留著當退路')]
    assert '--clobber' in step, '同一天重跑會堆出第二個附件'
    assert 'gh release view' in step, '沒有先判斷 release 在不在'
    # 不用任何憑證——GITHUB_TOKEN 是 runner 自己就有的。
    assert 'github.token' in step
    assert 'GDRIVE' not in ci, 'Drive 的殘留沒清乾淨'


def test_走過的死路要寫在原始碼裡():
    """artifact 會過期、Drive 的服務帳號沒有配額——兩條都試過。

    下一個看到「Excel 存哪」這個問題的人（包括三個月後的我）會想到同樣那兩個
    答案，而失敗的原因都不是看一眼就知道的。
    """
    from tw_trend_filter import __main__ as m

    doc = m._env_excel_url.__doc__
    assert 'storageQuotaExceeded' in doc or '儲存空間' in doc
    assert 'artifact' in doc


def test_沒有任何標的時不產生檔案(tmp_path):
    assert build_interactive_html([], '2026-09-04', str(tmp_path)) is None


# ─────────────────────────────────────────────────────────────────────────
# 切換個股要快。三件事各自都不會讓任何東西壞掉，只會讓桌機上每切一檔就卡
# 兩三秒——而那種問題沒有測試守著，下次有人「順手」改回去就回來了。
#
# 共通的成本：一次 Plotly.relayout 會把五百根 K 棒整個重畫（candlestick
# 的每一根都是一條 path）。所以規則是：能不 relayout 就不 relayout。
# ─────────────────────────────────────────────────────────────────────────

def test_預設視窗和它的_y_軸範圍一開始就是對的(tmp_path):
    """畫出來就要是最終樣子，不能畫完再 relayout 三次調成最終樣子。

    原本這三個範圍是 Python 寫進圖 JSON 裡的，所以這條測試去 `fig-0` 讀。現在
    圖是前端用一份共用樣板組的，樣板裡那三個範圍**故意是空的**（見
    `chart_template`：留著的話會是那兩根假 K 棒算出來的值），改由
    `tfSeriesFig()` 在填數列的時候算。

    要守的事情沒變：`Plotly.newPlot` 拿到的那份 layout 就已經帶著最終範圍，
    不是畫完再調三次（每一次 relayout 都要把六百根 K 棒重畫一遍）。

    這裡驗 Python 這一側算出來的那組範圍對不對；「前端算出來的和這裡一模一樣」
    由 tests/test_chart_js.py 直接跑那段 JS 比對。
    """
    import json

    from tw_trend_filter.pipeline import _chart_figure, chart_template

    html = _build(tmp_path)
    ser = json.loads(
        re.search(r'id="tf-series">(.*?)</script>', html, re.S)
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

    # 而且那個範圍要真的是近三個月，不是整段歷史。
    lo, hi = layout["xaxis2"]["range"]
    assert (
        __import__("datetime").date.fromisoformat(hi)
        - __import__("datetime").date.fromisoformat(lo)
    ).days < 130

    # 樣板自己不帶範圍：帶著的話，前端漏算其中一個軸也看不出來——那個軸會靜靜地
    # 停在造樣板用的那兩根假 K 棒上。
    tpl = chart_template()["layout"]
    for axis in ("xaxis2", "yaxis", "yaxis2"):
        assert "range" not in tpl.get(axis, {}), f"樣板的 {axis} 還留著範圍"

    # 前端三個軸都要設。漏一個不會報錯，只會讓那張圖的某一軸自己 autorange。
    js = html[html.index("function tfSeriesFig"):]
    js = js[: js.index("\nfunction tfDraw")]
    for axis in ("xaxis2", "yaxis", "yaxis2"):
        assert f"fig.layout.{axis}.range" in js, f"tfSeriesFig 沒有設 {axis} 的範圍"


def test_換一檔就回到那一檔自己的預設視窗(tmp_path):
    """這條的意思**反過來了**，理由寫在下面。

    原本是「切回已經畫過的圖不要重新套用預設範圍」——那時候每一檔各有自己的一格
    圖，切回去看到的就是離開時的樣子，而重套一次範圍既洗掉讀者捲過的位置、又多
    付一次 relayout（一次 relayout 要把六百根 K 棒整個重畫）。

    現在圖表區只有**一格**，每一檔都畫在同一塊畫布上。在這個前提下「保留上一檔
    的範圍」是錯的：那個範圍是屬於上一檔的。近三個月那個預設視窗是**逐檔算**
    的——它的上下界取自那一檔自己最後 63 根 K 棒的高低點，而且把那一檔的停損線
    也算進去（見 `_chart_window`）。把 2330 拉到的範圍套到 1101 上，畫面上會是
    一張半空的圖，而且沒有任何東西說明為什麼。

    所以換一檔就回到那一檔自己的預設視窗，時間範圍那排按鈕也跟著回到〔3月〕
    ——不然按鈕說 1 年、圖上是 3 個月，兩者互相矛盾。

    至於 relayout 的成本：`tfDraw` 走的是 `Plotly.react`，範圍是跟著資料一起交
    出去的，不是畫完再調一次。
    """
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


# ---------------------------------------------------------------------------
# 缺值要畫成缺口，不是畫成零
# ---------------------------------------------------------------------------

def test_missing_bars_become_null_not_zero():
    """`sf()` 把轉不出來的值變成 None，不是 0.0。

    差別在畫面上很大：plotly 遇到 None 會斷線（那一根 K 棒不畫、均線留一個
    缺口），遇到 0.0 會畫一根價格為零的 K 棒——也就是憑空發明一次跌到底的
    崩盤，而且圖上看起來煞有其事。

    這一版之前是回 0.0。而同一個函式產生的陣列，下游算 y 軸範圍的地方
    (`win_hi` / `win_lo`) 本來就寫著 `if v is not None`——也就是那段程式一直
    在等一個永遠不會出現的 None。
    """
    import json
    import math as _math
    import re

    src = (Path(__file__).resolve().parents[1]
           / "tw_trend_filter" / "pipeline.py").read_text(encoding="utf-8")
    body = re.search(r"    def sf\(v, d=2\):.*?\n        return round\(f, d\)",
                     src, re.S)
    assert body, "找不到 sf()，它被改名或改寫了"
    ns = {"_math": _math}
    exec("def _w():\n" + body.group(0) + "\n    return sf\nsf = _w()", ns)  # noqa: S102
    sf = ns["sf"]

    assert sf(12.345) == 12.35
    for bad in (float("nan"), float("inf"), float("-inf"), None, "", "abc"):
        assert sf(bad) is None, f"{bad!r} 應該是 None，拿到 {sf(bad)!r}"
    # 而且要序列化得出來（plotly 的 figure 是 JSON）
    assert json.dumps([sf(1.0), sf(float("nan")), sf(2.0)]) == "[1.0, null, 2.0]"
