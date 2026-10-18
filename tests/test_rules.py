
import subprocess
import sys

from tw_trend_filter.pipeline import DEFAULT_RULES, Rules


def test_預設值就是原本寫死在程式裡的那幾個數字():
    r = DEFAULT_RULES
    assert r.min_price == 10
    assert r.min_vol20 == 1000
    assert r.min_amount == 50_000_000
    assert r.lookback == 10
    assert r.squeeze == 0.12
    assert r.vol_ratio == 1.2
    assert r.atr_stop == 3.0


def test_預設的說明文字和改成可調之前逐字相同():
    assert DEFAULT_RULES.describe() == (
        ('① 基礎流動性防禦',
         '股價 > 10 元 ｜ 20日均量 > 1,000 股 ｜ 日均成交金額 > 5,000 萬元'),
        ('② 趨勢多頭確認',
         '收盤站穩季線(60MA)之上，且月線(20MA) > 季線(60MA)'),
        ('③ 關鍵發動時機',
         '過去 10 日內：月季線黃金交叉 或 布林頻寬壓縮 ≤ 12%'),
        ('④ 強勢突破＋爆量',
         '收盤突破布林上軌 或 創 20 日新高，且當日量 ≥ 20 日均量 × 1.2'),
    )


def test_改了門檻說明就跟著改():
    r = Rules(vol_ratio=1.1, squeeze=0.15, min_amount=30_000_000, lookback=5)
    text = dict(r.describe())
    assert '× 1.1' in text['④ 強勢突破＋爆量']
    assert '1.2' not in text['④ 強勢突破＋爆量']
    assert '≤ 15%' in text['③ 關鍵發動時機']
    assert '過去 5 日內' in text['③ 關鍵發動時機']
    assert '3,000 萬元' in text['① 基礎流動性防禦']
    assert text['② 趨勢多頭確認'] == dict(DEFAULT_RULES.describe())['② 趨勢多頭確認']


def test_停損倍數也會寫進進出場策略那一段():
    assert Rules(atr_stop=2.0).changed() == {'atr_stop': (3.0, 2.0)}


def test_照預設跑的時候_changed_是空的():
    assert DEFAULT_RULES.changed() == {}
    assert Rules().changed() == {}
    assert Rules(min_price=20).changed() == {'min_price': (10.0, 20.0)}


def test_命令列的預設值是從_Rules_拿的_不是另外寫一份():
    import os
    import re

    env = dict(os.environ, COLUMNS='100')
    out = subprocess.run(
        [sys.executable, '-m', 'tw_trend_filter', '--help'],
        capture_output=True, text=True, check=True, env=env,
    ).stdout
    out = re.sub(r'\s+', ' ', out)
    assert '四部曲門檻' in out
    for flag, shown in (
        ('--min-price', '預設 10'),
        ('--min-vol20', '預設 1,000'),
        ('--min-amount-m', '預設 50'),
        ('--lookback', '預設 10'),
        ('--squeeze', '預設 0.12'),
        ('--vol-ratio', '預設 1.2'),
        ('--atr-stop', '預設 3'),
    ):
        assert flag in out, f'{flag} 不在 --help 裡'
        assert shown in out, f'{flag} 的預設值沒有印成「{shown}」'


def test_成交額在命令列上是百萬元_內部是元():
    from tw_trend_filter.__main__ import main  # noqa: PLC0415

    assert callable(main)
    src = open(
        __import__('tw_trend_filter.__main__', fromlist=['x']).__file__,
        encoding='utf-8',
    ).read()
    assert 'args.min_amount_m * 1e6' in src, '換算不見了，門檻會小一百萬倍'


def test_排程不帶任何門檻_所以每天跑的永遠是預設():
    import pathlib
    import re

    wf = (pathlib.Path(__file__).resolve().parents[1]
          / '.github/workflows/daily.yml').read_text('utf-8')
    for name, flag in (
        ('min_price', '--min-price'),
        ('min_amount_m', '--min-amount-m'),
        ('lookback', '--lookback'),
        ('squeeze', '--squeeze'),
        ('vol_ratio', '--vol-ratio'),
        ('atr_stop', '--atr-stop'),
    ):
        assert f'{name}:' in wf, f'{name} 沒有出現在 workflow 的 inputs 裡'
        assert flag in wf, f'{flag} 沒有被組進指令裡'
        m = re.search(rf'\$\{{\{{\s*github\.event\.inputs\.{name}\s*(\|\|[^}}]*)?\}}\}}', wf)
        assert m, f'{name} 的 env 取值寫法找不到'
        assert not m.group(1), f'{name} 在 workflow 裡又寫了一份預設：{m.group(1)}'



def _synthetic(days=150, jump=110.0, flat=100.0, vol=600_000, vol_last=900_000,
               slope=0.0):
    import pandas as pd

    closes = [flat + slope * i for i in range(days - 1)] + [closes_jump(
        flat, slope, days, jump)]
    vols = [vol] * (days - 1) + [vol_last]
    idx = pd.bdate_range('2025-01-01', periods=days)
    return pd.DataFrame(
        {
            'Open': closes,
            'High': [c * 1.01 for c in closes],
            'Low': [c * 0.99 for c in closes],
            'Close': closes,
            'Volume': vols,
        },
        index=idx,
    )


def closes_jump(flat, slope, days, jump):
    return flat + slope * (days - 2) + (jump - flat)


def _run_with(rules, tmp_path, frame=None, want='count'):
    import tw_trend_filter.pipeline as pl

    df = _synthetic() if frame is None else frame
    orig_universe = pl.load_tw_stock_universe
    orig_download = pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            ['1111.TW'], {'1111': '測試股'}, {'1111': '測試業'}
        )
        pl.yf.download = lambda *a, **k: df.copy()
        out = pl.run(
            str(tmp_path), workers=1, rules=rules,
        )
    finally:
        pl.load_tw_stock_universe = orig_universe
        pl.yf.download = orig_download
    if want == 'trigger':
        return out['results'][0]['trigger'] if out['results'] else ''
    return out['count']


def test_預設門檻讓這檔合成股通過(tmp_path):
    assert _run_with(DEFAULT_RULES, tmp_path) == 1


def test_量比門檻拉高就擋下來(tmp_path):
    assert _run_with(Rules(vol_ratio=1.6), tmp_path) == 0
    assert _run_with(Rules(vol_ratio=1.4), tmp_path) == 1


def test_成交額門檻拉高就擋下來(tmp_path):
    assert _run_with(Rules(min_amount=100_000_000), tmp_path) == 0


def test_橫盤那一檔是交叉與壓縮同時成立的(tmp_path):
    flat = _run_with(DEFAULT_RULES, tmp_path / 'flat', want='trigger')
    assert '黃金交叉' in flat and '布林壓縮' in flat, flat
    ramp = _run_with(DEFAULT_RULES, tmp_path / 'ramp',
                     frame=_synthetic(slope=0.05), want='trigger')
    assert '黃金交叉' not in ramp and '布林壓縮' in ramp, ramp


def test_壓縮門檻收緊就擋下來(tmp_path):
    ramp = _synthetic(slope=0.05)
    assert _run_with(DEFAULT_RULES, tmp_path / 'ok', frame=ramp) == 1
    assert _run_with(Rules(squeeze=-0.01), tmp_path / 'no', frame=ramp) == 0


def test_停損倍數改了_停損價就跟著改(tmp_path):
    import tw_trend_filter.pipeline as pl

    df = _synthetic()
    orig_universe, orig_download = pl.load_tw_stock_universe, pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            ['1111.TW'], {'1111': '測試股'}, {'1111': '測試業'}
        )
        pl.yf.download = lambda *a, **k: df.copy()
        three = pl.run(str(tmp_path / 'a'), workers=1,
                       rules=Rules())['results'][0]
        one = pl.run(str(tmp_path / 'b'), workers=1,
                     rules=Rules(atr_stop=1.0))['results'][0]
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_universe, orig_download

    atr = three['atr14']
    assert abs(three['stop_loss'] - (three['close'] - 3 * atr)) < 0.02
    assert abs(one['stop_loss'] - (one['close'] - 1 * atr)) < 0.02
    assert one['stop_loss'] > three['stop_loss'], '倍數變小，停損該往上移'


def test_workflow_的_run_區塊裡不可以有空的表達式樣板():
    import pathlib
    import re

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1] / '.github/workflows'
    files = sorted(root.glob('*.yml'))
    assert files, '找不到任何 workflow'

    def walk(node, where):
        if isinstance(node, str):
            for m in re.finditer(r'\$\{\{(.*?)\}\}', node, re.S):
                assert m.group(1).strip(), f'{where} 有一組空的表達式樣板'
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f'{where}.{k}')
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f'{where}[{i}]')

    for path in files:
        walk(yaml.safe_load(path.read_text('utf-8')), path.name)


def test_母體抓不到是結束碼_2_不是_traceback():
    import tw_trend_filter.pipeline as pl
    from tw_trend_filter.__main__ import main

    assert issubclass(pl.UniverseIncomplete, RuntimeError), (
        '要是 RuntimeError 的子類——舊的 except 還接得住它'
    )
    orig = pl.load_tw_stock_universe
    try:
        def boom(*a, **k):
            raise pl.UniverseIncomplete('兩個來源都沒拿到')
        pl.load_tw_stock_universe = boom
        assert main(['--output-dir', '/tmp/never', '--no-excel']) == 2
    finally:
        pl.load_tw_stock_universe = orig


def test_真的壞掉還是要壞掉():
    import pytest

    import tw_trend_filter.pipeline as pl
    from tw_trend_filter.__main__ import main

    orig = pl.load_tw_stock_universe
    try:
        def boom(*a, **k):
            raise ValueError('這是程式壞了')
        pl.load_tw_stock_universe = boom
        with pytest.raises(ValueError):
            main(['--output-dir', '/tmp/never', '--no-excel'])
    finally:
        pl.load_tw_stock_universe = orig




def _run_full(rules, tmp_path):
    import pandas as pd

    import tw_trend_filter.pipeline as pl

    idx = pd.bdate_range('2025-01-01', periods=150)
    df = pd.DataFrame(
        {'Open': [5.0] * 150, 'High': [5.05] * 150, 'Low': [4.95] * 150,
         'Close': [5.0] * 150, 'Volume': [600_000] * 150},
        index=idx,
    )
    orig_u, orig_d = pl.load_tw_stock_universe, pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            ['1111.TW'], {'1111': '測試股'}, {'1111': '測試業'}
        )
        pl.yf.download = lambda *a, **k: df.copy()
        return pl.run(str(tmp_path), workers=1, rules=rules)
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_u, orig_d


def test_跑完整趟_rules_還是傳進來那一個_而且不產_Excel(tmp_path):
    out = _run_full(DEFAULT_RULES, tmp_path)
    assert out['count'] == 0
    assert out['rules'] is DEFAULT_RULES, 'rules 被路上某個同名變數蓋掉了'
    assert out['rules_changed'] == {}
    assert 'xlsx' not in out
    assert not list(tmp_path.glob('*.xlsx')), 'Excel 已經拿掉了，不該再產生'


def test_改過的門檻整趟跑完照樣記得(tmp_path):
    custom = Rules(vol_ratio=1.5, atr_stop=2.0)
    out = _run_full(custom, tmp_path)
    assert out['rules'] is custom
    assert set(out['rules_changed']) == {'vol_ratio', 'atr_stop'}


def _report_html(rules, tmp_path):
    import tw_trend_filter.pipeline as pl

    df = _synthetic()
    orig_u, orig_d = pl.load_tw_stock_universe, pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            ['1111.TW'], {'1111': '測試股'}, {'1111': '測試業'}
        )
        pl.yf.download = lambda *a, **k: df.copy()
        out = pl.run(str(tmp_path), workers=1, rules=rules)
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_u, orig_d
    assert out['html'], '這一趟應該有標的通過，才畫得出報告'
    return open(out['html'], encoding='utf-8').read()


def test_報告網頁上看得到篩選條件(tmp_path):
    text = _report_html(DEFAULT_RULES, tmp_path)
    assert 'id="tf-rules"' in text
    for label, _ in DEFAULT_RULES.describe():
        assert label in text, f'少了 {label}'
    assert '布林頻寬壓縮 ≤ 12%' in text
    assert '20 日均量 × 1.2' in text
    assert '3 × ATR(14)' in text


def test_網頁上的門檻跟著這一趟走(tmp_path):
    text = _report_html(Rules(vol_ratio=1.1, atr_stop=2.0), tmp_path)
    assert '× 1.1' in text and '2 × ATR(14)' in text
    assert '× 1.2' not in text


def test_沒給門檻就整塊不出現():
    import tw_trend_filter.pipeline as pl

    assert pl._live_block(None) == ''
    assert 'id="tf-rules"' in pl._live_block(DEFAULT_RULES)
