import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tw_trend_filter.__main__ as M

BASE = {
    'count': 3, 'scanned': 1900, 'universe': 1900, 'errors': 0, 'too_short': 0,
    'date': '2026-09-21', 'asof': None, 'xlsx': 'x.xlsx', 'html': '', 'index': '',
    'results': [], 'error_kinds': {}, 'error_samples': [], 'rules_changed': {},
}


def _outputs(tmp_path, monkeypatch, argv=(), **over):
    res = dict(BASE, **over)
    monkeypatch.setattr(M, '_run', lambda a, r: res)
    monkeypatch.setenv('GITHUB_OUTPUT', str(tmp_path / 'out.txt'))
    try:
        M.main(['--output-dir', str(tmp_path), '--no-excel', *argv])
    except SystemExit:
        pass
    got = {}
    for line in (tmp_path / 'out.txt').read_text(encoding='utf-8').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            got[k] = v
    return got


def test_正常的一趟可以發布(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch)
    assert got['healthy'] == 'yes'
    assert got['publishable'] == 'yes', got


def test_煙霧測試不可以發布(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch, argv=['--limit', '50'],
                   scanned=50, universe=50)
    assert got['healthy'] == 'yes', '煙霧測試不該被判成不健康'
    assert got['limited'] == 'yes'
    assert got['publishable'] == 'no', '50 檔的煙霧測試會蓋掉正式報告'


def test_大一點的煙霧測試也不可以發布(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch, argv=['--limit', '500'],
                   scanned=500, universe=500)
    assert got['healthy'] == 'yes'
    assert got['limited'] == 'yes'
    assert got['publishable'] == 'no', '500 檔的煙霧測試會蓋掉 1,900 檔那一份'


def test_改過門檻的那一趟不可以發布(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch, rules_changed={'vol_ratio': (1.2, 3.0)})
    assert got['healthy'] == 'yes'
    assert got['publishable'] == 'no', '非預設門檻的報告上線了，而網站上看不出來'
    assert 'vol_ratio' in got['rules']


def test_記帳對不起來就不健康(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch, scanned=1900, errors=150, universe=1900)
    assert got['balanced'] == 'no', got
    assert got['healthy'] == 'no', (
        '1,900 檔裡有 150 檔同時算進 scanned 和 errors，覆蓋率卻說 100%'
    )
    assert got['publishable'] == 'no'


def test_記帳的三個桶子加起來就是母體(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch, scanned=1700, errors=150, too_short=50)
    assert got['balanced'] == 'yes'
    assert got['healthy'] == 'yes', got


def test_資料基準日要寫出來(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch, asof='2026-09-18')
    assert got['asof'] == '2026-09-18', got
    assert got['date'] == '2026-09-21'


def test_沒有砍資料的日子asof就是當天(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch)
    assert got['asof'] == '2026-09-21', '沒砍資料卻寫了別的日期'




def _daily():
    return (ROOT / '.github/workflows/daily.yml').read_text(encoding='utf-8')


def test_發布的每一步都看publishable():
    y = _daily()
    assert "steps.screen.outputs.healthy == 'yes'" not in y, (
        '還有步驟用 healthy 當安全帶'
    )
    assert y.count("steps.screen.outputs.publishable == 'yes'") == 5, (
        f"只有 {y.count(chr(34))} 個步驟改成 publishable"
    )


def test_release的tag用資料基準日():
    y = _daily()
    assert 'DAY: ${{ steps.screen.outputs.asof' in y, y[:0] or 'tag 還在用 date'
