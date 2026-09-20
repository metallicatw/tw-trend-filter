"""〔趨勢∩六大∩報酬〕：第五道關卡。

四部曲講的是「今天技術面在動」。這一關問的是另一件事——**這家公司的體質與價位
值不值得**。兩者互不相干，所以它是另外一道關卡，而不是把四部曲改掉：
`passes()` 與 `tfPass()` 一行都沒有為它動過（`test_snapshot.py` 仍然守著那兩邊
的一致）。

## 報酬風險比為什麼在這邊算

    目標價 ＝ 歷年本益比高 × 預估EPS      ← 和今天的股價無關
    下檔價 ＝ 歷年本益比低 × 預估EPS      ← 和今天的股價無關
    報酬風險比 ＝ |(目標價/股價 − 1) ÷ (下檔價/股價 − 1)|

只有最後一步要股價，而這支程式手上就有今天的收盤。對面（tw-six-metrics）發前
兩個、這邊算最後一步，兩邊就是同一天的同一個價格。

由對面算好送過來的話，這支程式得排在它後面才拿得到當天的值——而它跑在前面
34 分鐘。而且四部曲挑的正是「今天剛漲上去」的那一批，用昨天的價格算會讓報酬
被系統性高估，偏差的方向剛好倒向「看起來更該買」。

## 三種「沒有數字」

    rr_free=True    股價已低於下檔價：**沒有下檔風險**，四段裡最好的那一種。
    rr=0.0          股價已高過目標價：沒有上檔可分。算得出來的答案，答案是不要。
    rr=None         真的算不出來（對面沒有這一檔的目標價）。

第一種和第三種在畫面上都「沒有數字」，意思卻正好相反。混在一起的話，`> 門檻`
會把最好的那一批和沒資料的一起丟掉，而丟掉的方式是「它們不在名單上」。
"""

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

# ── 報酬風險比的算術 ──────────────────────────────────────────────


def test_一般情況():
    """5439 對帳：目標價 395.6582、下檔價 155.2678、收盤 247 → 1.62。

    這三個數字抄自 tw-six-metrics 真的發出來的那一份，答案抄自它
    `data/valuations.csv` 裡同一列。兩個 repo 各自算一次要得到同一個值——
    對不起來的話，同一檔在〔台股評等清單〕和這裡會是兩個報酬風險比。
    """
    rr, free = reward_risk(395.6582, 155.2678, 247.0)
    assert not free
    assert abs(rr - 1.62) < 0.01, rr


def test_股價低於下檔價是沒有風險不是沒有資料():
    rr, free = reward_risk(300.0, 100.0, 90.0)
    assert free is True and rr is None


def test_股價剛好等於下檔價也算沒有風險():
    """邊界。`<=` 改成 `<` 的話這一檔會掉進「算不出來」那一籃。

    而它其實是零風險：下檔已經到了，分母是 0。`abs(ret / 0)` 不能算，但那不
    代表資料缺——它代表沒有下檔可賠。對面（tw-six-metrics 的 `value_with_pe`）
    用的是同一個 `<=`，兩邊要一致。

    這一條是補上來的：第一版沒有它，而把 `<=` 改成 `<` 的那次破壞**全綠**。
    """
    rr, free = reward_risk(300.0, 100.0, 100.0)
    assert free is True and rr is None, (
        f'股價剛好等於下檔價得到 {(rr, free)}——那是零風險，不是缺資料'
    )


def test_股價高過目標價就沒有上檔可分():
    """兩個負號被 abs() 約掉的那一種。對面實測有 4 檔（南俊國際 582 倍配
    −100% 的預期報酬）。"""
    rr, free = reward_risk(100.0, 90.0, 200.0)
    assert free is False
    assert rr == 0.0, f'得到 {rr}——abs() 又把兩個負號約掉了'


def test_對面沒有這一檔就是算不出來():
    assert reward_risk(None, None, 100.0) == (None, False)
    assert reward_risk(300.0, None, 100.0) == (None, False)


def test_沒有收盤價不會爆掉():
    assert reward_risk(300.0, 100.0, 0) == (None, False)
    assert reward_risk(300.0, 100.0, None) == (None, False)


# ── 第五關的判定 ──────────────────────────────────────────────────


def _cross(**kw):
    return _snap(**kw)


def test_門檻是零就等於這一關不存在():
    """〔台股趨勢選股〕那一頁的預設值。多兩個門檻不該改變它篩出什麼——

    而「對面今天有沒有把檔案發出來」更不該。
    """
    s = _cross(six=None, rr=None, rr_free=False)
    assert cross_passes(s, 0.0, 0.0)
    assert DEFAULT_RULES.min_six == 0.0 and DEFAULT_RULES.min_rr == 0.0


def test_兩個門檻都要過():
    s = _cross(six=3.5, rr=2.5, rr_free=False)
    assert cross_passes(s, 3.0, 2.0)
    assert not cross_passes(s, 3.6, 2.0), '六大沒過卻放行了'
    assert not cross_passes(s, 3.0, 2.6), '報酬風險比沒過卻放行了'


def test_門檻是大於不是大於等於():
    """使用者寫的是「大於 3 分」「大於 2」。剛好等於不算。"""
    s = _cross(six=3.0, rr=2.0, rr_free=False)
    assert not cross_passes(s, 3.0, 0.0)
    assert not cross_passes(s, 0.0, 2.0)


def test_沒有下檔風險比任何門檻都好():
    """這是使用者的決定：股價已經跌破下檔價 ＝ 一定入選。

    它的 `rr` 是 None，和「算不出來」長得一模一樣。少了 `rr_free`，全市場最好
    的那 492 檔會被 `> 2` 一起丟掉。
    """
    s = _cross(six=3.5, rr=None, rr_free=True)
    assert cross_passes(s, 3.0, 2.0)
    assert cross_passes(s, 3.0, 99.0), '無風險應該過得了任何報酬風險比門檻'
    # 但六大那一關還是要過——無風險說的是價位，不是體質。
    assert not cross_passes(s, 3.6, 2.0)


def test_算不出來的不算過():
    s = _cross(six=3.5, rr=None, rr_free=False)
    assert not cross_passes(s, 3.0, 2.0)
    assert cross_passes(s, 3.0, 0.0), '報酬風險比門檻是 0 的時候不該管它'


def test_沒有六大評分的不算過():
    s = _cross(six=None, rr=5.0, rr_free=False)
    assert not cross_passes(s, 3.0, 2.0)


def test_零報酬不算過():
    """`rr=0.0` 是「股價已經高過目標價」。它有數字，而那個數字的意思是不要買。"""
    assert not cross_passes(_cross(six=3.5, rr=0.0, rr_free=False), 3.0, 2.0)


# ── 側欄卡片上那一行字 ────────────────────────────────────────────

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
    """∞ 要先知道它代表什麼才看得懂，而 0.00 看起來像「算出來剛好是零」。

    這三種在資料裡長得很像（`rr` 是 None 或 0），意思卻差很遠：

        無風險   股價已低於下檔價，沒有下檔風險——四種裡最好的
        空頭     預期報酬是負的（目標價低於現價），估價那一層把它夾成 0
        —        算不出來（沒有本益比區間，或預估 EPS 為負）

    隔壁〔台股評等清單〕那一欄用的是同一套字（tw-six-metrics 的 `reward()`）。
    兩邊不一樣的話，同一檔在兩個分頁上會顯示成兩件不同的事。
    """
    got = _js_rr_text([
        _cross(six=3.5, rr=None, rr_free=True),     # 無風險
        _cross(six=3.5, rr=0.0, rr_free=False),     # 空頭
        _cross(six=3.5, rr=None, rr_free=False),    # 算不出來
        _cross(six=3.5, rr=2.5, rr_free=False),     # 一般數字
    ])
    assert got == ['無風險', '空頭', '—', '2.50'], got


def test_無風險先判定_不會被空頭或破折號蓋掉():
    """`rr_free` 那一檔的 `rr` 也是 None——和「算不出來」長得一模一樣。

    判斷的順序寫反的話，全市場最好的那一批會顯示成「算不出來」，
    而那個症狀在畫面上是一個很合理的破折號。
    """
    assert _js_rr_text([_cross(six=3.5, rr=None, rr_free=True)]) == ['無風險']


# ── 兩邊要篩出同一份名單（Python 與瀏覽器） ──────────────────────

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
    """把每一種情況都排出來，而且踩在等號上。

    等號是翻譯最容易翻錯的地方：`<=` 和 `<` 在「剛好等於 3 分」的那一檔上是
    相反的答案，而隨機測資幾乎不會剛好落在那一點。
    """
    return [
        _cross(six=3.5, rr=2.5, rr_free=False),     # 都過
        _cross(six=3.0, rr=2.5, rr_free=False),     # 六大剛好等於門檻 → 不過
        _cross(six=3.5, rr=2.0, rr_free=False),     # 報酬風險比剛好等於 → 不過
        _cross(six=3.5, rr=None, rr_free=True),     # 無風險 → 過
        _cross(six=3.5, rr=None, rr_free=False),    # 算不出來 → 不過
        _cross(six=None, rr=9.0, rr_free=False),    # 沒有六大 → 不過
        _cross(six=3.5, rr=0.0, rr_free=False),     # 零報酬 → 不過
        _cross(six=None, rr=None, rr_free=False),   # 什麼都沒有
        _cross(six=3.0000001, rr=2.0000001, rr_free=False),   # 剛好超過一點點
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


# ── 入口與設定 ────────────────────────────────────────────────────


def test_交集入口的預設是三和二():
    assert CROSS_DEFAULTS == {'min_six': 3.0, 'min_rr': 2.0}


def test_兩個門檻都在畫面上調得到():
    keys = {k for k, *_ in LIVE_FIELDS}
    assert {'min_six', 'min_rr'} <= keys, (
        '門檻進不了 LIVE_FIELDS 就不會出現在畫面上，也不會進 tfRules()'
    )


def test_這兩格也要決定存幾位():
    """和門檻比大小的數字，存進快照的精度就是判定的精度。"""
    assert SNAPSHOT_PRECISION['six'] >= 2
    assert SNAPSHOT_PRECISION['rr'] >= 4


def test_抓不到就是空的不是例外():
    """對面掛掉不該讓今天整份趨勢報告不見——四部曲一個位元組都不需要它。"""
    assert load_cross_feed('http://127.0.0.1:1/nope.json') == {}


def test_可以關掉不連網():
    assert load_cross_feed('-') == {}
