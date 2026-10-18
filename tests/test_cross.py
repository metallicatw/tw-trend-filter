
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

import pytest
from test_snapshot import PRELUDE, _page_js, _snap

from tw_trend_filter.pipeline import (
    CROSS_DEFAULTS,
    DEFAULT_RULES,
    LIVE_FIELDS,
    SNAPSHOT_PRECISION,
    Rules,
    cross_passes,
    load_cross_feed,
    reward_risk,
    snapshot_row,
)



def test_一般情況():
    rr, free = reward_risk(395.6582, 155.2678, 247.0)
    assert not free
    assert abs(rr - 1.62) < 0.01, rr


def test_股價低於下檔價是沒有風險不是沒有資料():
    rr, free = reward_risk(300.0, 100.0, 90.0)
    assert free is True and rr is None


def test_股價剛好等於下檔價也算沒有風險():
    rr, free = reward_risk(300.0, 100.0, 100.0)
    assert free is True and rr is None, (
        f'股價剛好等於下檔價得到 {(rr, free)}——那是零風險，不是缺資料'
    )


def test_股價高過目標價就沒有上檔可分():
    rr, free = reward_risk(100.0, 90.0, 200.0)
    assert free is False
    assert rr == 0.0, f'得到 {rr}——abs() 又把兩個負號約掉了'


def test_對面沒有這一檔就是算不出來():
    assert reward_risk(None, None, 100.0) == (None, False)
    assert reward_risk(300.0, None, 100.0) == (None, False)


def test_沒有收盤價不會爆掉():
    assert reward_risk(300.0, 100.0, 0) == (None, False)
    assert reward_risk(300.0, 100.0, None) == (None, False)




def _cross(**kw):
    return _snap(**kw)


def test_門檻是零就等於這一關不存在():
    s = _cross(six=None, rr=None, rr_free=False)
    assert cross_passes(s, 0.0, 0.0)
    assert DEFAULT_RULES.min_six == 0.0 and DEFAULT_RULES.min_rr == 0.0


def test_兩個門檻都要過():
    s = _cross(six=3.5, rr=2.5, rr_free=False)
    assert cross_passes(s, 3.0, 2.0)
    assert not cross_passes(s, 3.6, 2.0), '六大沒過卻放行了'
    assert not cross_passes(s, 3.0, 2.6), '報酬風險比沒過卻放行了'


def test_門檻是大於不是大於等於():
    s = _cross(six=3.0, rr=2.0, rr_free=False)
    assert not cross_passes(s, 3.0, 0.0)
    assert not cross_passes(s, 0.0, 2.0)


def test_沒有下檔風險比任何門檻都好():
    s = _cross(six=3.5, rr=None, rr_free=True)
    assert cross_passes(s, 3.0, 2.0)
    assert cross_passes(s, 3.0, 99.0), '無風險應該過得了任何報酬風險比門檻'
    assert not cross_passes(s, 3.6, 2.0)


def test_算不出來的不算過():
    s = _cross(six=3.5, rr=None, rr_free=False)
    assert not cross_passes(s, 3.0, 2.0)
    assert cross_passes(s, 3.0, 0.0), '報酬風險比門檻是 0 的時候不該管它'


def test_沒有六大評分的不算過():
    s = _cross(six=None, rr=5.0, rr_free=False)
    assert not cross_passes(s, 3.0, 2.0)


def test_零報酬不算過():
    assert not cross_passes(_cross(six=3.5, rr=0.0, rr_free=False), 3.0, 2.0)



RRTEXT_DRIVER = """
const rows = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(rows.map(tfRrText)));
"""


def _js_rr_text(rows):
    node = shutil.which('node')
    if node is None:
        if os.environ.get('CI'):
            pytest.fail('CI 上找不到 node，這一支守不住卡片上那一行字')
        pytest.skip('本機沒有 node；這一支會在 CI 上跑')
    with tempfile.TemporaryDirectory() as d:
        src, arg = os.path.join(d, 'c.js'), os.path.join(d, 'p.json')
        with open(src, 'w', encoding='utf-8') as f:
            f.write(PRELUDE + '\n' + _page_js() + '\n' + RRTEXT_DRIVER)
        with open(arg, 'w', encoding='utf-8') as f:
            json.dump([snapshot_row(r) for r in rows], f, ensure_ascii=False)
        r = subprocess.run([node, src, arg], capture_output=True,
                           text=True, check=False)
    assert r.returncode == 0, f'node 跑不起來：{r.stderr}'
    return json.loads(r.stdout)


def test_三種非數字的值都寫成字():
    got = _js_rr_text([
        _cross(six=3.5, rr=None, rr_free=True),
        _cross(six=3.5, rr=0.0, rr_free=False),
        _cross(six=3.5, rr=None, rr_free=False),
        _cross(six=3.5, rr=2.5, rr_free=False),
    ])
    assert got == ['無風險', '價格高估', '—', '2.50'], got


def test_無風險先判定_不會被價格高估或破折號蓋掉():
    assert _js_rr_text([_cross(six=3.5, rr=None, rr_free=True)]) == ['無風險']



CROSS_DRIVER = """
const payload = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
document.getElementById = function (id) {
  const k = id.replace(/^f_/, '');
  return Object.prototype.hasOwnProperty.call(payload.rules, k)
    ? {value: String(payload.rules[k])} : null;
};
const r = tfRules();
console.log(JSON.stringify(payload.rows.map(function (row) {
  return tfCross(row, r);
})));
"""


def _js_cross(rows, min_six, min_rr):
    node = shutil.which('node')
    if node is None:
        if os.environ.get('CI'):
            pytest.fail('CI 上找不到 node，這一支守不住兩邊的一致性')
        pytest.skip('本機沒有 node；這一支會在 CI 上跑')
    rules = Rules(min_six=min_six, min_rr=min_rr)
    payload = {
        'rows': [snapshot_row(s) for s in rows],
        'rules': {k: (getattr(rules, k) / 1e6 if k == 'min_amount'
                      else getattr(rules, k))
                  for k, *_ in LIVE_FIELDS},
    }
    with tempfile.TemporaryDirectory() as d:
        src, arg = os.path.join(d, 'c.js'), os.path.join(d, 'p.json')
        with open(src, 'w', encoding='utf-8') as f:
            f.write(PRELUDE + '\n' + _page_js() + '\n' + CROSS_DRIVER)
        with open(arg, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        r = subprocess.run([node, src, arg], capture_output=True,
                           text=True, check=False)
    assert r.returncode == 0, f'node 跑不起來：{r.stderr}'
    return json.loads(r.stdout)


def _cross_rows():
    return [
        _cross(six=3.5, rr=2.5, rr_free=False),
        _cross(six=3.0, rr=2.5, rr_free=False),
        _cross(six=3.5, rr=2.0, rr_free=False),
        _cross(six=3.5, rr=None, rr_free=True),
        _cross(six=3.5, rr=None, rr_free=False),
        _cross(six=None, rr=9.0, rr_free=False),
        _cross(six=3.5, rr=0.0, rr_free=False),
        _cross(six=None, rr=None, rr_free=False),
        _cross(six=3.0000001, rr=2.0000001, rr_free=False),
    ]


@pytest.mark.parametrize('min_six,min_rr', [(3.0, 2.0), (0.0, 0.0),
                                            (3.0, 0.0), (0.0, 2.0)])
def test_python_和瀏覽器篩出同一份(min_six, min_rr):
    rows = _cross_rows()
    js = _js_cross(rows, min_six, min_rr)
    py = [cross_passes(s, min_six, min_rr) for s in rows]
    assert js == py, (
        f'門檻 {min_six}/{min_rr}：\n  JS {js}\n  PY {py}\n'
        '（`tfCross` 是 `cross_passes` 的逐行翻譯，改了一邊要改另一邊）'
    )




def test_那個入口的兩個門檻預設都是零():
    assert CROSS_DEFAULTS == {'min_six': 0.0, 'min_rr': 0.0}
    keys = {k for k, *_ in LIVE_FIELDS}
    assert set(CROSS_DEFAULTS) <= keys, set(CROSS_DEFAULTS) - keys


def test_兩個門檻都在畫面上調得到():
    keys = {k for k, *_ in LIVE_FIELDS}
    assert {'min_six', 'min_rr'} <= keys, (
        '門檻進不了 LIVE_FIELDS 就不會出現在畫面上，也不會進 tfRules()'
    )


def test_這兩格也要決定存幾位():
    assert SNAPSHOT_PRECISION['six'] >= 2
    assert SNAPSHOT_PRECISION['rr'] >= 4


def test_抓不到就是空的不是例外():
    assert load_cross_feed('http://127.0.0.1:1/nope.json') == {}


def test_可以關掉不連網():
    assert load_cross_feed('-') == {}
