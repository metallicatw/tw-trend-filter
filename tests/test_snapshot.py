"""同一組門檻、同一份快照，Python 和瀏覽器必須篩出同一份名單。

## 為什麼會有兩份實作

四部曲的每一個可調門檻，最後都只是拿一個數字去比大小。所以「換一組門檻會篩出
哪幾檔」不需要重算任何指標——只要每一檔在最後一根 K 棒上的那十幾個數字。那份
快照嵌在報告網頁裡，判定就在瀏覽器裡跑完：零等待、不必登入、不用叫任何 workflow。

代價是判定寫了兩次：`pipeline.passes()` 是 Python 那一份，報告網頁上的 `tfPass()`
是它的逐行翻譯。瀏覽器裡跑不了 Python，而唯一的替代方案是把判定搬回伺服器——
那就回到「按一下等三十分鐘」。所以重複是刻意的，而代價由這一支扛著。

## 這一支真的在跑那份 JS

不是拿 regex 去比對兩邊長得像不像——那種檢查會在「兩邊都寫了 `<=`，但其中一邊
比的是另一個欄位」的時候綠燈。這裡是把報告裡那一段 `<script>` 原封不動餵給
node 執行，讓它對同一批列跑出答案，再和 Python 的答案逐檔對。

## 邊界是重點，不是附加

翻譯最容易翻錯的不是邏輯，是 `<=` 和 `<`：`close <= min_price` 和
`close < min_price` 在「剛好等於」的那一檔上是相反的答案，而隨機測資幾乎永遠
不會剛好落在那一點上。所以測資分兩批：一批刻意讓每一個門檻剛好踩在等號上，
一批是固定亂數種子的一千列。
"""

import json
import os
import random
import shutil
import subprocess
import tempfile

import pytest

from tw_trend_filter.pipeline import (
    DEFAULT_RULES,
    LIVE_FIELDS,
    SNAPSHOT_COLUMNS,
    SNAPSHOT_DAYS,
    Rules,
    _live_block,
    passes,
    snapshot_row,
)

# ── 把報告裡那段 JS 挖出來丟給 node ────────────────────────────────

# 這一段要排在挖出來的 JS **前面**：那段 JS 在最外層就會呼叫
# document.addEventListener，沒有先擺好 stub 的話，連載入都到不了 tfPass。
PRELUDE = """
// 判定本身（tfPass）是純函式，碰不到 DOM；這裡只要讓載入不爆掉。
globalThis.document = {
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  addEventListener: function () {},
};
"""

# 刻意繞過 `tfPass(row, payload.rules)` 這條捷徑，改成把數字塞進假的輸入框、
# 再讓 `tfRules()` 自己去讀。理由是**單位換算也在 tfRules 裡**：畫面上的成交
# 金額是「百萬」，判定用的是「元」。直接餵 rules 的話，那一次乘以一百萬永遠
# 不會被執行到——而這一支第一次跑就是被這件事抓到的。
DRIVER = """
const payload = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
document.getElementById = function (id) {
  const k = id.replace(/^f_/, '');
  return Object.prototype.hasOwnProperty.call(payload.rules, k)
    ? {value: String(payload.rules[k])} : null;
};
const r = tfRules();
const out = payload.rows.map(function (row) { return tfPass(row, r); });
console.log(JSON.stringify(out));
"""


def _page_js():
    """報告網頁上那一段 `<script>`（不含裝快照的那個 application/json）。"""
    html = _live_block(DEFAULT_RULES, snapshots=[_snap()])
    # 最後一個 <script> … </script> 就是判定那一段；前面那個是
    # <script type="application/json">，開頭的標籤不一樣，所以 rsplit 不會切錯。
    head, _, tail = html.rpartition('<script>')
    assert head, '報告裡找不到判定那一段 <script>'
    js, _, _ = tail.partition('</script>')
    assert 'function tfPass' in js, '挖出來的那一段裡沒有 tfPass'
    return js


def _js_verdicts(rows, rules):
    """讓 node 跑那段 JS，回傳每一列的觸發訊號（沒過是 None）。"""
    node = shutil.which('node')
    if node is None:
        # CI 上一定有 node（ubuntu runner 內建），所以在 CI 缺席就是紅字，
        # 不是跳過——一條永遠跳過的守門和沒有守門一樣。
        if os.environ.get('CI'):
            pytest.fail('CI 上找不到 node，這一支守不住兩邊的一致性')
        pytest.skip('本機沒有 node；這一支會在 CI 上跑')

    payload = {
        'rows': [snapshot_row(s) for s in rows],
        # JS 那一邊拿到的是畫面上的數字：成交金額的單位是百萬。
        'rules': {k: (getattr(rules, k) / 1e6 if k == 'min_amount'
                      else getattr(rules, k))
                  for k, *_ in LIVE_FIELDS},
    }
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'check.js')
        arg = os.path.join(d, 'payload.json')
        with open(src, 'w', encoding='utf-8') as f:
            f.write(PRELUDE + '\n' + _page_js() + '\n' + DRIVER)
        with open(arg, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        r = subprocess.run([node, src, arg], capture_output=True, text=True)
    assert r.returncode == 0, f'node 跑不起來：{r.stderr}'
    return json.loads(r.stdout)


def _py_verdicts(rows, rules):
    out = []
    for s in rows:
        ok, triggers = passes(s, rules)
        out.append(triggers if ok else None)
    return out


# ── 測資 ──────────────────────────────────────────────────────────

def _snap(close=100.0, **kw):
    """一檔剛好**通過**預設門檻的快照。每一項都離門檻一步，方便往回推。

    價格那幾項（兩條均線、布林上軌、20 日高點）刻意寫成 `close` 的比例，不是
    寫死的數字。第一版寫死了，於是「股價剛好等於下限」那一列是 close=10 對上
    ma60=90——它確實沒過，但**不是因為股價**，而是因為它早在 60MA 那一關就被
    擋掉了。把 `<=` 改成 `<` 那一列照樣沒過，測試照樣綠。

    守在等號上的測資，其他每一項都必須留在安全距離內，那一格才真的在被驗。
    """
    s = {
        'code': '2330', 'name': '台積電', 'industry': '半導體',
        'close': close, 'vol20': 5000.0, 'amt20': 5e8,
        'ma20': close * 0.95, 'ma60': close * 0.90,
        'cross_ago': 3,
        'bw': [0.30] * (SNAPSHOT_DAYS - 1) + [0.05],
        'boll_up': close * 0.98, 'donchian': close * 0.99,
        'vol_ratio': 1.5, 'atr14': close * 0.02,
        # 判定用不到這兩個，側欄卡片要用。給一組固定值就好——它們進不了
        # `passes()`，但少了它們 `snapshot_row()` 會 KeyError。
        'chg': 1.0, 'chg_pct': 1.0,
    }
    s.update(kw)
    # ②④ 那三個布林值**從上面那幾個價格推出來**，和 `screen_stock` 裡那幾行
    # 一模一樣。
    #
    # 不讓呼叫端自己填一個固定值，是為了讓既有的邊界測資繼續守住它原本守的
    # 東西：「收盤對60MA」那一列設的是 `ma60=close`，它要驗的是那一關會不會
    # 擋——要是 `trend_ok` 另外給一個固定的 True，那一列會通過，而測試照樣綠。
    # 推導出來，設了 `ma60=close` 就自動得到 `trend_ok=False`。
    #
    # 真要單獨指定（例如驗「判定只看布林值、不看那幾個 float」）就用 kw 蓋掉。
    s.setdefault('trend_ok', s['close'] > s['ma60'] and s['ma20'] > s['ma60'])
    s.setdefault('brk_boll', s['close'] > s['boll_up'])
    s.setdefault('brk_don',
                 s['donchian'] is not None and s['close'] > s['donchian'])
    # 第五關的三格。預設 None／False ＝「對面今天沒有這一檔的資料」，而預設
    # 門檻是 0，所以 `cross_passes` 一律放行——四部曲的測資因此完全不受影響。
    s.setdefault('six', None)
    s.setdefault('rr', None)
    s.setdefault('rr_free', False)
    assert set(s) == set(SNAPSHOT_COLUMNS), '快照欄位和 SNAPSHOT_COLUMNS 對不上'
    return s


def _boundary_rows():
    """每一個門檻剛好踩在等號上，各來一列。

    `passes()` 在這幾項用的是 `<=`（**等於就是沒過**），所以這幾列的正確答案
    是「沒過」——而把 `<=` 翻成 `<` 的話，它們會全部變成「過」。
    """
    r = DEFAULT_RULES
    rows = [
        _snap(),                                   # 基準：過
        _snap(close=r.min_price),                  # 股價剛好等於下限 → 沒過
        _snap(vol20=r.min_vol20),                  # 均量剛好等於 → 沒過
        _snap(amt20=r.min_amount),                 # 均額剛好等於 → 沒過
        _snap(ma60=100.0),                         # 收盤剛好等於 60MA → 沒過
        _snap(ma20=90.0, ma60=90.0),               # 20MA 剛好等於 60MA → 沒過
        _snap(boll_up=100.0, donchian=None),       # 突破要 `>`，等於不算
        _snap(vol_ratio=r.vol_ratio),              # 量比剛好等於下限 → **過**（這一項是 `<`）
        # 黃金交叉的窗口：lookback=10，第 10 天算在內、第 11 天不算。
        _snap(cross_ago=10, bw=[0.30] * SNAPSHOT_DAYS),
        _snap(cross_ago=11, bw=[0.30] * SNAPSHOT_DAYS),
        _snap(cross_ago=0, bw=[0.30] * SNAPSHOT_DAYS),    # 0 代表沒交叉過
        # 布林壓縮：`<=`，剛好等於門檻算壓縮。
        _snap(cross_ago=0, bw=[0.30] * (SNAPSHOT_DAYS - 1) + [r.squeeze]),
        # 壓縮發生在窗口**外面**（第 11 天），不算。
        _snap(cross_ago=0,
              bw=[0.30] * 9 + [r.squeeze] + [0.30] * (SNAPSHOT_DAYS - 10)),
        # 20 日高點剛好等於收盤：突破要 `>`，等於不算。布林上軌同時推到收盤
        # 上面，不然這一列會被布林那一邊救起來，而那等於沒有驗到 20 日高點。
        _snap(donchian=100.0, boll_up=101.0),
        # donchian 是 None（上市未滿 20 天）：只能靠布林軌突破。
        _snap(donchian=None),                      # 過（布林軌在收盤底下）
        _snap(donchian=None, boll_up=101.0),       # 沒過（兩個突破都不成立）
        # 兩個訊號同時觸發、兩個突破同時成立——訊號的**順序**也要一樣。
        _snap(cross_ago=2, bw=[0.05] * SNAPSHOT_DAYS,
              boll_up=98.0, donchian=98.5),
        # ②④ 的判定讀的是布林值，**不是**旁邊那幾個 float。
        #
        # 這三列故意讓兩者互相矛盾：float 說「過」而布林說「沒過」。任何一邊
        # （Python 或 JS）偷看 float 的話，兩邊就會對這三列給出不同的答案，
        # 而 `test_同一份快照兩邊篩出同一份名單` 會紅。
        #
        # 矛盾是刻意造出來的，但它對應的是真實情況：快照裡的 ma60 只存到小數
        # 第二位，而判定用的是完整的 float64——收盤 100.001、ma60 100.0004 的
        # 那一檔，存進去會變成 100.0 對 100.0，float 看起來是「沒站上」，而
        # 真正的答案是「站上了」。這裡把方向反過來寫，是因為反過來才驗得到
        # 「有沒有真的讀布林值」（順著寫的話，讀錯也會得到同一個答案）。
        _snap(trend_ok=False),                     # float 說過、布林說沒過
        _snap(brk_boll=False, brk_don=False),      # 兩個突破都被布林值否掉
        _snap(boll_up=101.0, donchian=101.0, brk_boll=True),  # float 說沒突破、布林說有
    ]
    for i, s in enumerate(rows):
        s['code'] = f'{1000 + i}'
    return rows


def _random_rows(n=1000, seed=20260913):
    """固定種子的亂數列。邊界那幾列守的是等號，這一批守的是「有沒有哪一格接錯」。"""
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        close = round(rnd.uniform(5, 300), 2)
        rows.append(_snap(
            code=f'{9000 + i}',
            close=close,
            vol20=round(rnd.uniform(100, 20000), 1),
            amt20=round(rnd.uniform(1e6, 2e9), 1),
            ma20=round(close * rnd.uniform(0.85, 1.15), 2),
            ma60=round(close * rnd.uniform(0.85, 1.15), 2),
            cross_ago=rnd.choice([0, 1, 5, 9, 10, 11, 25]),
            bw=[round(rnd.uniform(0.02, 0.5), 4) for _ in range(SNAPSHOT_DAYS)],
            boll_up=round(close * rnd.uniform(0.9, 1.1), 2),
            donchian=(None if rnd.random() < 0.1
                      else round(close * rnd.uniform(0.9, 1.1), 2)),
            vol_ratio=round(rnd.uniform(0.5, 4.0), 3),
            atr14=round(rnd.uniform(0.1, 15), 3),
        ))
    return rows


# 讀者真的會打進去的幾組，加上兩組故意打壞的。
RULE_SETS = {
    '預設': DEFAULT_RULES,
    '放寬': Rules(min_price=5, min_vol20=200, min_amount=1e7,
                  lookback=20, squeeze=0.3, vol_ratio=0.8, atr_stop=2),
    '收緊': Rules(min_price=50, min_vol20=5000, min_amount=1e9,
                  lookback=3, squeeze=0.05, vol_ratio=2.5, atr_stop=4),
    '回看天數超過快照存的天數': Rules(lookback=SNAPSHOT_DAYS + 30),
    '回看天數打成負數': Rules(lookback=-5),
    '回看天數打成零': Rules(lookback=0),
}


@pytest.mark.parametrize('name', list(RULE_SETS))
def test_同一份快照兩邊篩出同一份名單(name):
    rules = RULE_SETS[name]
    rows = _boundary_rows() + _random_rows()
    py, js = _py_verdicts(rows, rules), _js_verdicts(rows, rules)

    assert len(py) == len(js)
    bad = [(rows[i]['code'], py[i], js[i])
           for i in range(len(py)) if py[i] != js[i]]
    assert not bad, (
        f'門檻〔{name}〕下有 {len(bad)} 檔兩邊答案不一樣（前五檔）：'
        + '；'.join(f'{c}：Python={p!r} JS={j!r}' for c, p, j in bad[:5])
    )


def test_這批測資真的有過有不過():
    """守門的守門。

    如果測資全部沒過，上面那一支會綠——而它什麼都沒驗到。這一支在
    `_boundary_rows` 被改壞（例如基準那一列自己就不過）的時候會紅。
    """
    v = _py_verdicts(_boundary_rows() + _random_rows(), DEFAULT_RULES)
    hit = [x for x in v if x]
    assert len(hit) >= 20, f'只有 {len(hit)} 列通過，測資偏了'
    assert len(hit) < len(v), '每一列都通過，門檻沒有在擋任何東西'


@pytest.mark.parametrize('what,at,just_over', [
    # 欄位            剛好踩在門檻上             推過去一點點
    ('股價下限',   {'close': 10.0},           {'close': 10.01}),
    ('20日均量',   {'vol20': 1000.0},         {'vol20': 1000.1}),
    ('20日均額',   {'amt20': 5e7},            {'amt20': 5e7 + 1}),
    # 60MA 推到收盤附近的時候，20MA 要跟著推上去——不然擋住它的會是下一關。
    ('收盤對60MA', {'ma60': 100.0, 'ma20': 100.5},
                   {'ma60': 99.99, 'ma20': 100.5}),
    ('20MA對60MA', {'ma20': 90.0, 'ma60': 90.0},
                   {'ma20': 90.01, 'ma60': 90.0}),
    ('突破布林上軌', {'boll_up': 100.0, 'donchian': None},
                     {'boll_up': 99.99, 'donchian': None}),
])
def test_每一列邊界測資真的卡在它要守的那一關(what, at, just_over):
    """守門的守門，第二層。

    這一支存在的理由是它抓到過一次真的：第一版的 `_snap` 把均線寫死成 90／95，
    於是「股價剛好等於下限」那一列是 close=10 對上 ma60=90——它沒過，但是被
    60MA 那一關擋掉的。把判定裡的 `<=` 改成 `<`，那一列照樣沒過，跨語言那一支
    照樣全綠。一列在錯的關卡上被擋掉的邊界測資，等於沒有那一列。
    """
    stuck, _ = passes(_snap(**at), DEFAULT_RULES)
    assert not stuck, f'〔{what}〕剛好踩在門檻上，應該沒過'
    loose, _ = passes(_snap(**just_over), DEFAULT_RULES)
    assert loose, f'〔{what}〕只推過門檻一點點就該過——它現在卡在別的關卡上'


def test_量比那一項是小於不是小於等於():
    """這一項和其他六項相反，而它最容易被「順手統一」成 `<=`。

    `passes()` 寫的是 `vol_ratio < rules.vol_ratio`：量比**剛好等於**門檻算過。
    原因是門檻預設 1.2 是「比平常多兩成」，而「剛好多兩成」本來就該算數。
    """
    ok, _ = passes(_snap(vol_ratio=DEFAULT_RULES.vol_ratio), DEFAULT_RULES)
    assert ok
    ok, _ = passes(_snap(vol_ratio=DEFAULT_RULES.vol_ratio - 0.001), DEFAULT_RULES)
    assert not ok


def test_網頁上的預設值就是這一次真的跑的那組門檻():
    """輸入框裡那幾個數字要是報告自己跑的那組，不是寫死的 DEFAULT_RULES。

    不然一份用 `--vol-ratio 1.1` 跑出來的報告，打開來輸入框寫 1.2，而清單是
    1.1 篩的——畫面和內容安靜地不一致，正是這個專案踩過的那一類錯。
    """
    r = Rules(min_price=33, min_amount=1.23e8, vol_ratio=1.05)
    html = _live_block(r, snapshots=[_snap()])
    assert 'id="f_min_price" type="number" min="0" step="1" value="33"' in html
    assert 'value="123"' in html          # 成交金額換算成百萬
    assert 'id="f_vol_ratio"' in html and 'value="1.05"' in html


def test_沒有快照就只剩那把尺():
    """沒有快照的時候只留〔預設篩選條件〕那份內容，不畫一個按了沒反應的表單。

    （`build_interactive_html` 那一層現在會直接擋掉沒有快照的呼叫——見
    `test_沒有快照的報告根本產不出來`。這一條守的是 `_live_block` 自己。）
    """
    html = _live_block(DEFAULT_RULES)
    assert 'id="tf-rules"' in html
    assert 'id="live"' not in html
    assert 'tfPass' not in html


def test_有圖沒圖的那幾檔都在側欄上而且分得出來():
    """`TF_DRAWN` 決定點一張卡片是切到它的圖，還是說「這一頁上沒有它的圖」。

    空的話每一張卡片都會變成「沒有圖」——而那正是它壞掉時的樣子，不會報錯。
    """
    html = _live_block(DEFAULT_RULES, snapshots=[_snap()],
                       drawn={'2330': 0, '2317': 1},
                       link_base='https://example.invalid/six/')
    assert '"2317": 1' in html or '"2317":1' in html
    assert 'const TF_LINK = "https://example.invalid/six"' in html
    assert 'function tfOpen' in html
    assert 'function tfNoChart' in html
    # `TF_DRAWN` 現在**只管樣式**，不管路由：點哪一張卡片都走同一條
    # `tfFetchChart(code)`，數列在頁面上就直接用，不在就去抓。所以這裡不再找
    # `showChart(at)`（那個分岔拿掉了），改成確認那條「有沒有圖」的判斷還在，
    # 而且它認得第二個來源——附了圖表資料（TF_DATA）的話，全市場每一檔都點得開。
    assert 'tfFetchChart(code)' in html, '點卡片不再走同一條路了'
    # 找的是那個**呼叫**，不是「showChart」這九個字母——原始碼的註解裡寫著
    # 「以前這裡分兩條：嵌過的叫 showChart() …」，拿字串去整頁找會把那句註解
    # 一起找到，然後這條測試永遠紅。
    assert 'showChart(at)' not in html, '又冒出第二條開圖的路'
    assert 'hasOwnProperty.call(TF_DRAWN, code) || !!TF_DATA' in html, \
        '「有沒有圖」漏掉了 TF_DATA 這個來源，放寬門檻多出來的股票會全被標成沒有圖'


# ── 快照真的到得了那一頁 ──────────────────────────────────────────
#
# 上面每一支都在驗 `_live_block()` 本身。但這一整條路上最容易斷的不是它，
# 是**呼叫端忘了把 snapshots 傳下去**——那時候頁面照樣產得出來、照樣沒有錯誤，
# 只是〔自己調門檻〕整塊不見了。這一支從 `build_interactive_html` 這一端進去。

def _fake_result(code='2330', name='台積電'):
    import numpy as np
    import pandas as pd

    from tw_trend_filter.pipeline import compute_bollinger

    idx = pd.bdate_range('2022-01-03', periods=300)
    close = pd.Series(np.linspace(100.0, 200.0, 300), index=idx)
    df = pd.DataFrame({
        'Open': close * 0.99, 'High': close * 1.02, 'Low': close * 0.98,
        'Close': close,
        'Volume': pd.Series(np.full(300, 5e6), index=idx),
    }, index=idx)
    bmid, bup, bdn, _ = compute_bollinger(close)
    return {
        'ticker': f'{code}.TW', 'code': code, 'name': name,
        'industry': '半導體業',
        'close': 200.0, 'ma20_last': 195.0, 'ma60_last': 180.0,
        'boll_up_last': 205.0, 'boll_mid_last': 195.0, 'boll_dn_last': 185.0,
        'boll_bw_pct': 10.2, 'vol_today': 6e6, 'vol20_avg': 5e6,
        'vol_ratio': 1.35, 'amt_M': 900.0, 'atr14': 4.2, 'stop_loss': 187.4,
        'golden_cross': True, 'squeeze': False, 'trigger': '黃金交叉',
        '_df': df, '_ma20': close.rolling(20).mean(),
        '_ma60': close.rolling(60).mean(),
        '_boll_up': bup, '_boll_mid': bmid, '_boll_dn': bdn,
    }


def _page(tmp_path, **kw):
    import datetime

    from tw_trend_filter.pipeline import build_interactive_html

    p = build_interactive_html(
        [_fake_result()], '2026-09-13', str(tmp_path),
        datetime.datetime(2026, 9, 13, 15, 30), rules=DEFAULT_RULES, **kw)
    assert p, 'build_interactive_html 回傳 None'
    return open(p, encoding='utf-8').read()


def test_快照傳得到報告頁上(tmp_path):
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')])
    assert 'id="live"' in html
    assert 'id="tf-snap"' in html
    assert 'function tfPass' in html
    # 〔調整篩選條件〕不收合：它是這一頁的控制器，改了就換掉左邊那排卡片。
    # 〔預設篩選條件〕移到外層網站的燈泡裡了（見 tests/test_report_links.py），
    # 這一頁只帶著它的內容。
    assert '<div id="live">' in html
    assert '<template id="tf-rules">' in html
    assert '<dialog' not in html


def test_沒有快照的報告根本產不出來(tmp_path):
    """少了快照，產出的會是一份**沒有側欄**的報告：打得開、不報錯、什麼都點不到。

    所以在 `build_interactive_html` 那一層就擋掉，而不是讓它安靜地產一份壞的。
    以前 `rules=None` 的意思是「不畫那一塊說明」；現在它們是頁面的骨架。
    """
    import pytest as _pytest

    with _pytest.raises(ValueError, match='snapshots'):
        _page(tmp_path, snapshots=None)


def test_側欄整排卡片由前端畫_python_端不再組一份(tmp_path):
    """兩份實作會在其中一邊改了樣式之後安靜地長得不一樣。

    舊版是 Python 組好 `nav_btns` 送進 HTML。它畫的是「排程當天通過預設門檻的
    那幾檔」——而現在那排卡片要畫的是「通過**你現在這組門檻**的那幾檔」，那一組
    Python 在建站時不可能知道。所以只留前端那一份。
    """
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')])
    assert '<div id="sb-list"></div>' in html, 'Python 又組了一份側欄'
    assert 'function tfCard' in html, '前端那一份不見了'
    assert 'id="btn-0"' not in html, '舊的側欄卡片還在'


def test_有圖的那幾檔在_TF_DRAWN_裡而且序號對得上(tmp_path):
    """序號錯一位的症狀是「點 A 跳到 B」，而它不會報錯。"""
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')])
    assert '"2330": 0' in html or '"2330":0' in html
    assert '6505' not in html.split('const TF_DRAWN =')[1].split(';')[0]


def test_側欄選中的那一檔和右邊畫的那一張是同一檔(tmp_path):
    """兩邊由不同的東西決定，所以會各走各的——而症狀很安靜：側欄亮著 2317，
    右邊畫的是 2330。

    真的踩到過兩次，兩次都是「有兩個地方在管同一件事」：

    1. `showChart(idx)` 自己會去標側欄第 idx 張卡片。以前那是對的（第 i 張卡片
       就是第 i 張圖），現在不是了——卡片是「通過你這組門檻的那幾檔」，圖是
       「排程當天通過預設門檻的那幾檔」，兩份名單的長度和順序都不一樣。
    2. 頁面載入時 `DOMContentLoaded` 有兩個監聽器，後跑的那個寫死 `showChart(0)`，
       把前一個開好的圖蓋掉，卻蓋不掉側欄上的亮框。

    所以這條測試守的是**唯一性**：側欄的 .act 只有 tfMark 在寫，開哪一張圖只有
    tfOpen 在決定。
    """
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')])
    body = html[html.index('<body>'):]
    # 那個「兩份名單、兩套索引」的狀況現在是**結構上**不成立的：圖表區只有一格，
    # 畫哪一檔由**代號**決定，不再有「第 idx 張圖」這種東西。所以這裡先確認
    # showChart 真的不在了——它回來就代表索引那條路也回來了。
    assert 'function showChart' not in body, '按索引開圖的那條路又回來了'
    seg = body[body.index('function tfDraw('):]
    seg = seg[: seg.index('\n}')]
    assert ".nb'" not in seg and '.nb"' not in seg, 'tfDraw 又去標側欄了'
    # 載入時不可以有第二個地方決定開哪一張圖。只看 DOMContentLoaded 的處理常式
    # 內容，不是整份 HTML——原始碼的註解裡寫著「`showChart(0)` 拿掉了」，拿字串
    # 去整頁找會把那句註解一起找到，然後這條測試永遠紅。
    for h in _dom_ready_handlers(body):
        for fn in ('showChart(', 'tfDraw(', 'tfFetchChart('):
            assert fn not in h, f'又有人在載入時寫死開某一張圖：{h[:80]}'
    assert 'function tfMark' in body and 'function tfOpen' in body


def _dom_ready_handlers(body):
    """每一個 `DOMContentLoaded` 監聽器的函式內容。

    只做到「切到下一個 `});`」這種粗淺的程度就夠了：這裡要問的是「這個處理常式
    裡有沒有人呼叫 showChart」，不是要解析 JavaScript。
    """
    out, at = [], 0
    needle = "addEventListener('DOMContentLoaded'"
    while True:
        i = body.find(needle, at)
        if i < 0:
            return out
        end = body.find('});', i)
        out.append(body[i:end if end > 0 else i + 400])
        at = i + len(needle)


# ── 沒過預設篩選的那幾檔，圖是抓回來的 ──────────────────────────────

def test_每一檔沒過篩的股票各寫一個圖表資料檔(tmp_path):
    """點了才抓，而不是全部嵌進同一頁。

    1,900 檔兩年的 K 棒全嵌進來是好幾百 MB；一檔一個檔案，點下去才抓那一檔。

    檔名就是代號，所以前端直接組得出網址，不需要一份索引——而一份索引就是第二個
    會過期的東西。

    檔案裡裝的是**數列**，不是一整張 plotly 圖（本來是）。一整張圖裡日期那六百多
    個字串被十一條 trace 各存一份、customdata 又把每條數列原封不動再存一次、然後
    同一套兩百行的樣式每一檔都帶一份——240 KB。數列只有數字，59 KB。樣式由頁面上
    那份共用的樣板提供（`tf-figtpl`），圖在瀏覽器組（`tfSeriesFig`）。
    """
    import json as _json
    import os

    from tw_trend_filter.pipeline import SERIES_ARRAYS

    d = tmp_path / 'trend-d'
    extra = {'6505': _fake_result(code='6505', name='台塑化')}
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')],
                 extra_charts=extra, data_dir=str(d), data_base='trend-d')
    assert (d / '6505.json').is_file(), '沒過篩的那一檔沒有寫出圖表資料'
    ser = _json.loads((d / '6505.json').read_text(encoding='utf-8'))
    assert ser['code'] == '6505'
    assert ser['d'], '寫出來的數列是空的'
    n = len(ser['d'])
    for key in SERIES_ARRAYS:
        assert len(ser[key]) == n, f'{key} 和日期不一樣長，plotly 會把線畫到一半就停'
    # 已經嵌在頁面上的那一檔不必再寫一份。
    assert not (d / '2330.json').exists(), '嵌過的又寫了一次，白花時間和空間'
    assert 'const TF_DATA = "trend-d"' in html
    assert 'function tfFetchChart' in html
    # 頁面上要有那份共用樣板，不然抓回來的數列沒有東西可以填。
    assert 'id="tf-figtpl"' in html, '頁面上沒有圖表樣板，抓回來的數列畫不出圖'
    assert os.path.getsize(d / '6505.json') > 1000


def test_圖表資料的網址帶著這一份報告的版本(tmp_path):
    """`trend-d/<代號>.json` 每天整批換掉，網址卻一模一樣。

    不帶版本的話有兩種壞法，而且兩種都不報錯：

    1. **昨天的圖配今天的報告**。瀏覽器與 Pages 前面的 CDN 照 max-age 留著舊的
       那一份，於是今天的報告畫出昨天的 K 線——看起來完全正常，只是最後一根不
       見了，而那正是你在看的那一根。
    2. **被快取起來的 404**。報告先推 report 分支、Pages 隨後才發布，中間那一小
       段時間點下去是 404；而 404 可以被快取，所以檔案一分鐘後上去了，同一個
       瀏覽器還是一直重播那個 404。

    這一條守的是「網址帶得動版本」，以及「第一趟不是 force-cache」——那個設定
    的意思正是「有快取就用，不要問伺服器」，等於把上面兩件事都釘死。
    """
    html = _page(tmp_path, snapshots=[_snap(code='2330')],
                 extra_charts={'6505': _fake_result(code='6505', name='台塑化')},
                 data_dir=str(tmp_path / 'd'), data_base='trend-d')
    import re

    m = re.search(r'const TF_VER = "([^"]*)"', html)
    assert m and m.group(1), '圖表資料的網址沒有帶版本'
    assert "'?v=' + encodeURIComponent(TF_VER)" in html, '版本沒有接到網址上'
    # 只看 tfGrab 的函式內容，不是整頁——註解裡寫著當初為什麼拿掉 force-cache，
    # 拿字串去整頁找會把那句註解一起找到，然後這條測試永遠紅。
    grab = html[html.index('async function tfGrab('):]
    grab = grab[: grab.index('\n      }')]
    assert 'force-cache' not in grab, (
        "又用回 force-cache 了：那個設定的意思是「有快取就用，不要問伺服器」，"
        "會把舊資料和舊的 404 一起釘死"
    )
    assert "fetch(url, {cache: mode})" in grab, 'tfGrab 沒有讓呼叫端決定快取模式'
    # 404 要再試一次、而且那一次要繞過快取，不然發布空窗期的誤判會黏住。
    assert "tfGrab(code, 'reload')" in html, '404 之後沒有繞過快取再試一次'


def test_不給_data_dir_就一個檔案都不寫(tmp_path):
    """本機自己跑一份報告，不該在旁邊生出 1,900 個檔案。"""
    extra = {'6505': _fake_result(code='6505', name='台塑化')}
    html = _page(tmp_path, snapshots=[_snap(code='2330')], extra_charts=extra)
    assert 'const TF_DATA = ""' in html
    # 空的 TF_DATA 之下，點下去要說「這一份報告沒有附圖表資料」，
    # 而不是去抓一個組不出來的網址。
    assert '這一份報告沒有附圖表資料' in html


def test_改門檻不會馬上重篩_要按篩選(tmp_path):
    """邊打邊篩那一版試過：打「1200」的過程中會先用 1、12、120 各篩一次，
    左邊那排卡片跳三次，而且每一次都可能把你正在看的那一檔換掉。

    門檻是一組值，不是一個值——要一起生效。所以輸入框只負責把數字標成「改過了」，
    真正重篩的是〔篩選〕那顆按鈕（或在任何一格按 Enter）。
    """
    html = _page(tmp_path, snapshots=[_snap(code='2330')])
    assert 'button class="go" onclick="tfApply()"' in html.replace('  ', ' ') \
        or 'class="go" onclick="tfApply()"' in html, '沒有〔篩選〕按鈕'
    assert "addEventListener('input', tfStale)" in html, '打字還在直接重篩'
    assert "e.key === 'Enter'" in html, 'Enter 送不出去'
    assert 'function tfStale' in html
