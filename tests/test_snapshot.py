
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


PRELUDE = """
// 判定本身（tfPass）是純函式，碰不到 DOM；這裡只要讓載入不爆掉。
globalThis.document = {
  getElementById: function () { return null; },
  querySelector: function () { return null; },
  addEventListener: function () {},
};
"""

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
    html = _live_block(DEFAULT_RULES, snapshots=[_snap()])
    head, _, tail = html.rpartition('<script>')
    assert head, '報告裡找不到判定那一段 <script>'
    js, _, _ = tail.partition('</script>')
    assert 'function tfPass' in js, '挖出來的那一段裡沒有 tfPass'
    return js


def _js_verdicts(rows, rules):
    node = shutil.which('node')
    if node is None:
        if os.environ.get('CI'):
            pytest.fail('CI 上找不到 node，這一支守不住兩邊的一致性')
        pytest.skip('本機沒有 node；這一支會在 CI 上跑')

    payload = {
        'rows': [snapshot_row(s) for s in rows],
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



def _snap(close=100.0, **kw):
    s = {
        'code': '2330', 'name': '台積電', 'industry': '半導體',
        'close': close, 'vol20': 5000.0, 'amt20': 5e8,
        'ma20': close * 0.95, 'ma60': close * 0.90,
        'cross_ago': 3,
        'bw': [0.30] * (SNAPSHOT_DAYS - 1) + [0.05],
        'boll_up': close * 0.98, 'donchian': close * 0.99,
        'vol_ratio': 1.5, 'atr14': close * 0.02,
        'chg': 1.0, 'chg_pct': 1.0,
    }
    s.update(kw)
    s.setdefault('trend_ok', s['close'] > s['ma60'] and s['ma20'] > s['ma60'])
    s.setdefault('brk_boll', s['close'] > s['boll_up'])
    s.setdefault('brk_don',
                 s['donchian'] is not None and s['close'] > s['donchian'])
    s.setdefault('six', None)
    s.setdefault('rr', None)
    s.setdefault('rr_free', False)
    assert set(s) == set(SNAPSHOT_COLUMNS), '快照欄位和 SNAPSHOT_COLUMNS 對不上'
    return s


def _boundary_rows():
    r = DEFAULT_RULES
    rows = [
        _snap(),
        _snap(close=r.min_price),
        _snap(vol20=r.min_vol20),
        _snap(amt20=r.min_amount),
        _snap(ma60=100.0),
        _snap(ma20=90.0, ma60=90.0),
        _snap(boll_up=100.0, donchian=None),
        _snap(vol_ratio=r.vol_ratio),
        _snap(cross_ago=10, bw=[0.30] * SNAPSHOT_DAYS),
        _snap(cross_ago=11, bw=[0.30] * SNAPSHOT_DAYS),
        _snap(cross_ago=0, bw=[0.30] * SNAPSHOT_DAYS),
        _snap(cross_ago=0, bw=[0.30] * (SNAPSHOT_DAYS - 1) + [r.squeeze]),
        _snap(cross_ago=0,
              bw=[0.30] * 9 + [r.squeeze] + [0.30] * (SNAPSHOT_DAYS - 10)),
        _snap(donchian=100.0, boll_up=101.0),
        _snap(donchian=None),
        _snap(donchian=None, boll_up=101.0),
        _snap(cross_ago=2, bw=[0.05] * SNAPSHOT_DAYS,
              boll_up=98.0, donchian=98.5),
        _snap(trend_ok=False),
        _snap(brk_boll=False, brk_don=False),
        _snap(boll_up=101.0, donchian=101.0, brk_boll=True),
    ]
    for i, s in enumerate(rows):
        s['code'] = f'{1000 + i}'
    return rows


def _random_rows(n=1000, seed=20260913):
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
    v = _py_verdicts(_boundary_rows() + _random_rows(), DEFAULT_RULES)
    hit = [x for x in v if x]
    assert len(hit) >= 20, f'只有 {len(hit)} 列通過，測資偏了'
    assert len(hit) < len(v), '每一列都通過，門檻沒有在擋任何東西'


@pytest.mark.parametrize('what,at,just_over', [
    ('股價下限',   {'close': 10.0},           {'close': 10.01}),
    ('20日均量',   {'vol20': 1000.0},         {'vol20': 1000.1}),
    ('20日均額',   {'amt20': 5e7},            {'amt20': 5e7 + 1}),
    ('收盤對60MA', {'ma60': 100.0, 'ma20': 100.5},
                   {'ma60': 99.99, 'ma20': 100.5}),
    ('20MA對60MA', {'ma20': 90.0, 'ma60': 90.0},
                   {'ma20': 90.01, 'ma60': 90.0}),
    ('突破布林上軌', {'boll_up': 100.0, 'donchian': None},
                     {'boll_up': 99.99, 'donchian': None}),
])
def test_每一列邊界測資真的卡在它要守的那一關(what, at, just_over):
    stuck, _ = passes(_snap(**at), DEFAULT_RULES)
    assert not stuck, f'〔{what}〕剛好踩在門檻上，應該沒過'
    loose, _ = passes(_snap(**just_over), DEFAULT_RULES)
    assert loose, f'〔{what}〕只推過門檻一點點就該過——它現在卡在別的關卡上'


def test_量比那一項是小於不是小於等於():
    ok, _ = passes(_snap(vol_ratio=DEFAULT_RULES.vol_ratio), DEFAULT_RULES)
    assert ok
    ok, _ = passes(_snap(vol_ratio=DEFAULT_RULES.vol_ratio - 0.001), DEFAULT_RULES)
    assert not ok


def test_網頁上的預設值就是這一次真的跑的那組門檻():
    r = Rules(min_price=33, min_amount=1.23e8, vol_ratio=1.05)
    html = _live_block(r, snapshots=[_snap()])
    assert 'id="f_min_price" type="number" min="0" step="1" value="33"' in html
    assert 'value="123"' in html
    assert 'id="f_vol_ratio"' in html and 'value="1.05"' in html


def test_沒有快照就只剩那把尺():
    html = _live_block(DEFAULT_RULES)
    assert 'id="tf-rules"' in html
    assert 'id="live"' not in html
    assert 'tfPass' not in html


def test_有圖沒圖的那幾檔都在側欄上而且分得出來():
    html = _live_block(DEFAULT_RULES, snapshots=[_snap()],
                       drawn={'2330': 0, '2317': 1},
                       link_base='https://example.invalid/six/')
    assert '"2317": 1' in html or '"2317":1' in html
    assert 'const TF_LINK = "https://example.invalid/six"' in html
    assert 'function tfOpen' in html
    assert 'function tfNoChart' in html
    assert 'tfFetchChart(code)' in html, '點卡片不再走同一條路了'
    assert 'showChart(at)' not in html, '又冒出第二條開圖的路'
    assert 'hasOwnProperty.call(TF_DRAWN, code) || !!TF_DATA' in html, \
        '「有沒有圖」漏掉了 TF_DATA 這個來源，放寬門檻多出來的股票會全被標成沒有圖'



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
    assert '<div id="live">' in html
    assert '<template id="tf-rules">' in html
    assert '<dialog' not in html


def test_沒有快照的報告根本產不出來(tmp_path):
    import pytest as _pytest

    with _pytest.raises(ValueError, match='snapshots'):
        _page(tmp_path, snapshots=None)


def test_側欄整排卡片由前端畫_python_端不再組一份(tmp_path):
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')])
    assert '<div id="sb-list"></div>' in html, 'Python 又組了一份側欄'
    assert 'function tfCard' in html, '前端那一份不見了'
    assert 'id="btn-0"' not in html, '舊的側欄卡片還在'


def test_有圖的那幾檔在_TF_DRAWN_裡而且序號對得上(tmp_path):
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')])
    assert '"2330": 0' in html or '"2330":0' in html
    assert '6505' not in html.split('const TF_DRAWN =')[1].split(';')[0]


def test_側欄選中的那一檔和右邊畫的那一張是同一檔(tmp_path):
    html = _page(tmp_path, snapshots=[_snap(code='2330'), _snap(code='6505')])
    body = html[html.index('<body>'):]
    assert 'function showChart' not in body, '按索引開圖的那條路又回來了'
    seg = body[body.index('function tfDraw('):]
    seg = seg[: seg.index('\n}')]
    assert ".nb'" not in seg and '.nb"' not in seg, 'tfDraw 又去標側欄了'
    for h in _dom_ready_handlers(body):
        for fn in ('showChart(', 'tfDraw(', 'tfFetchChart('):
            assert fn not in h, f'又有人在載入時寫死開某一張圖：{h[:80]}'
    assert 'function tfMark' in body and 'function tfOpen' in body


def _dom_ready_handlers(body):
    out, at = [], 0
    needle = "addEventListener('DOMContentLoaded'"
    while True:
        i = body.find(needle, at)
        if i < 0:
            return out
        end = body.find('});', i)
        out.append(body[i:end if end > 0 else i + 400])
        at = i + len(needle)



def test_每一檔沒過篩的股票各寫一個圖表資料檔(tmp_path):
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
    assert not (d / '2330.json').exists(), '嵌過的又寫了一次，白花時間和空間'
    assert 'const TF_DATA = "trend-d"' in html
    assert 'function tfFetchChart' in html
    assert 'id="tf-figtpl"' in html, '頁面上沒有圖表樣板，抓回來的數列畫不出圖'
    assert os.path.getsize(d / '6505.json') > 1000


def test_圖表資料的網址帶著這一份報告的版本(tmp_path):
    html = _page(tmp_path, snapshots=[_snap(code='2330')],
                 extra_charts={'6505': _fake_result(code='6505', name='台塑化')},
                 data_dir=str(tmp_path / 'd'), data_base='trend-d')
    import re

    m = re.search(r'const TF_VER = "([^"]*)"', html)
    assert m and m.group(1), '圖表資料的網址沒有帶版本'
    assert "'?v=' + encodeURIComponent(TF_VER)" in html, '版本沒有接到網址上'
    grab = html[html.index('async function tfGrab('):]
    grab = grab[: grab.index('\n      }')]
    assert 'force-cache' not in grab, (
        "又用回 force-cache 了：那個設定的意思是「有快取就用，不要問伺服器」，"
        "會把舊資料和舊的 404 一起釘死"
    )
    assert "fetch(url, {cache: mode})" in grab, 'tfGrab 沒有讓呼叫端決定快取模式'
    assert "tfGrab(code, 'reload')" in html, '404 之後沒有繞過快取再試一次'


def test_不給_data_dir_就一個檔案都不寫(tmp_path):
    extra = {'6505': _fake_result(code='6505', name='台塑化')}
    html = _page(tmp_path, snapshots=[_snap(code='2330')], extra_charts=extra)
    assert 'const TF_DATA = ""' in html
    assert '這一份報告沒有附圖表資料' in html


def test_改門檻不會馬上重篩_要按篩選(tmp_path):
    html = _page(tmp_path, snapshots=[_snap(code='2330')])
    assert 'button class="go" onclick="tfApply()"' in html.replace('  ', ' ') \
        or 'class="go" onclick="tfApply()"' in html, '沒有〔篩選〕按鈕'
    assert "addEventListener('input', tfStale)" in html, '打字還在直接重篩'
    assert "e.key === 'Enter'" in html, 'Enter 送不出去'
    assert 'function tfStale' in html
