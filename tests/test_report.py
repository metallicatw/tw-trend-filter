"""互動報告的組裝測試。不碰網路，用假資料餵進 build_interactive_html。

這裡守的是三件會靜靜壞掉的事：

1. **個股頁連結**——`--link-base` 傳了卻沒出現在 HTML 裡，讀者只會覺得
   「這個網站就是沒有那個連結」，不會來回報。
2. **CDN 與內嵌二選一**——搞反了就是每天讓每位讀者多下載 3.5 MB，或者
   本機版離線打不開。兩種都不會報錯。
3. **K 線裁切**——裁在算指標之前的話，圖上前六十根的 60MA 會是斷的。
"""

import datetime
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
CDN_TAG = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"'


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


def test_沒有任何標的時不產生檔案(tmp_path):
    assert build_interactive_html([], '2026-09-04', str(tmp_path)) is None
