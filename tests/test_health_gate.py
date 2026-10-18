
import json
import os

import pandas as pd

import tw_trend_filter.pipeline as pl
from tw_trend_filter.__main__ import main


def _good_frame(days=150):
    closes = [100.0 + i * 0.05 for i in range(days)]
    idx = pd.bdate_range('2025-01-01', periods=days)
    return pd.DataFrame(
        {
            'Open': closes,
            'High': [c * 1.01 for c in closes],
            'Low': [c * 0.99 for c in closes],
            'Close': closes,
            'Volume': [600_000] * days,
        },
        index=idx,
    )


def _run_with(tmp_path, universe, download):
    orig_universe, orig_download = pl.load_tw_stock_universe, pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            [f'{c}.TW' for c in universe],
            {f'{c}.TW': f'測試{c}' for c in universe},
            {c: '測試業' for c in universe},
        )
        pl.yf.download = download
        return pl.run(str(tmp_path), workers=2,
                      retry_rounds=(), open_when_done=False, plotly_cdn=False)
    finally:
        pl.load_tw_stock_universe = orig_universe
        pl.yf.download = orig_download


def _main_with(tmp_path, universe, download, monkeypatch):
    monkeypatch.setattr(pl, 'load_tw_stock_universe', lambda *a, **k: (
        [f'{c}.TW' for c in universe],
        {f'{c}.TW': f'測試{c}' for c in universe},
        {c: '測試業' for c in universe},
    ))
    monkeypatch.setattr(pl.yf, 'download', download)
    return main(['--output-dir', str(tmp_path), '--no-excel',
                 '--workers', '2', '--offline-plotly', '--no-retry'])




def test_全市場下載失敗不可以判定成健康(tmp_path, monkeypatch):
    def dead(*a, **k):
        raise RuntimeError('Yahoo 今天不給資料')

    code = _main_with(tmp_path, ['1101', '1102', '1103', '1104', '1105'],
                      dead, monkeypatch)
    assert code == 2, (
        '全市場下載失敗卻判定成健康。下一步是 `git push -f origin report`，'
        '而那條分支只有一個 commit——昨天那份好的報告會被一份「今天 0 檔通過」'
        '永久蓋掉，而且沒有任何症狀。'
    )


def test_一成以內的零星失敗仍然算數(tmp_path, monkeypatch):
    frame = _good_frame()
    calls = {'n': 0}

    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('偶發')
        return frame.copy()

    code = _main_with(tmp_path, [str(1101 + i) for i in range(20)],
                      flaky, monkeypatch)
    assert code == 0, '漏掉一檔（5%）就判定不健康，這個門檻會紅到沒有人看'


def test_一切正常就是結束碼_0(tmp_path, monkeypatch):
    frame = _good_frame()
    code = _main_with(tmp_path, ['1101', '1102', '1103'],
                      lambda *a, **k: frame.copy(), monkeypatch)
    assert code == 0




def test_歷史不足的新上市不算失敗(tmp_path, monkeypatch):
    short, full = _good_frame(days=30), _good_frame(days=150)

    def mixed(ticker, *a, **k):
        return short.copy() if str(ticker).startswith('99') else full.copy()

    universe = ['1101', '1102', '1103', '1104', '1105', '1106',
                '9901', '9902', '9903', '9904']
    code = _main_with(tmp_path, universe, mixed, monkeypatch)
    assert code == 0, '新上市被算成失敗了——分母要扣掉 too_short'




def test_每一檔都要記到帳上(tmp_path):
    good, short = _good_frame(150), _good_frame(30)

    def mixed(ticker, *a, **k):
        t = str(ticker)
        if t.startswith('99'):
            return short.copy()
        if t.startswith('88'):
            raise RuntimeError('下載失敗')
        return good.copy()

    universe = ['1101', '1102', '9901', '9902', '8801', '8802', '8803']
    r = _run_with(tmp_path, universe, mixed)

    total = r['scanned'] + r['errors'] + r['too_short']
    assert total == r['universe'], (
        f"有 {r['universe'] - total} 檔沒有記到帳上："
        f"scanned={r['scanned']} errors={r['errors']} "
        f"too_short={r['too_short']} universe={r['universe']}。"
        '每一條 return None 的路都要進其中一格，否則門檻的分母是錯的。'
    )
    assert r['scanned'] == 2
    assert r['too_short'] == 2
    assert r['errors'] == 3
    assert r['error_kinds'].get('DownloadFailed') == 3, (
        '下載失敗要記成 DownloadFailed，不要和程式的例外混在一起——'
        '兩者的處置完全不同：一個去看 Yahoo，一個去看程式。'
    )


def test_scanned_不是母體大小(tmp_path):
    good = _good_frame(150)

    def half(ticker, *a, **k):
        if str(ticker).startswith('88'):
            raise RuntimeError('下載失敗')
        return good.copy()

    r = _run_with(tmp_path, ['1101', '1102', '8801', '8802'], half)
    assert r['universe'] == 4
    assert r['scanned'] == 2, (
        f"scanned={r['scanned']}，母體是 4——兩檔下載失敗卻照樣算進 scanned。"
        '這正是上一版：scanned 在呼叫端無條件遞增，所以它恆等於母體大小，'
        '而門檻拿 scanned/universe 當健康指標，於是那道門檻恆為 100%。'
    )




def test_摘要要寫出_healthy_和拆解過的數字(tmp_path, monkeypatch):
    out = tmp_path / 'gh_output'
    out.write_text('', encoding='utf-8')
    monkeypatch.setenv('GITHUB_OUTPUT', str(out))

    def dead(*a, **k):
        raise RuntimeError('boom')

    code = _main_with(tmp_path, ['1101', '1102', '1103'], dead, monkeypatch)
    assert code == 2

    written = dict(
        line.split('=', 1) for line in out.read_text('utf-8').splitlines() if '=' in line
    )
    assert written['healthy'] == 'no'
    assert written['scanned'] == '0'
    assert written['universe'] == '3'
    assert written['errors'] == '3'
    assert 'too_short' in written, 'workflow 摘要看不到新上市那幾檔，會以為是漏抓'






def _empty_frame():
    return pd.DataFrame()


def test_空的回應要記成下載失敗不是新上市(tmp_path):
    good = _good_frame(150)

    def mixed(ticker, *a, **k):
        return _empty_frame() if str(ticker).startswith('88') else good.copy()

    r = _run_with(tmp_path, ['1101', '1102', '8801', '8802', '8803'], mixed)
    assert r['too_short'] == 0, (
        f"空的回應被記成「新上市」{r['too_short']} 檔。那會被從分母裡扣掉，"
        '於是掃到的比例永遠是 100%，門檻永遠綠燈。'
    )
    assert r['errors'] == 3, f"errors={r['errors']}，三檔空的沒有被記成失敗"
    assert r['error_kinds'].get('DownloadFailed') == 3
    assert r['scanned'] == 2


def test_大量空的回應會讓門檻變紅(tmp_path, monkeypatch):
    good = _good_frame(150)

    def mixed(ticker, *a, **k):
        return _empty_frame() if str(ticker).startswith('88') else good.copy()

    code = _main_with(tmp_path, ['1101', '1102', '8801', '8802', '8803'],
                      mixed, monkeypatch)
    assert code == 2, (
        '三分之二的股票沒抓到，門檻卻說健康。下一步就是 force push 蓋掉昨天。'
    )


def test_真的新上市仍然不算失敗(tmp_path):
    short, full = _good_frame(30), _good_frame(150)

    def mixed(ticker, *a, **k):
        return short.copy() if str(ticker).startswith('99') else full.copy()

    r = _run_with(tmp_path, ['1101', '1102', '9901'], mixed)
    assert r['too_short'] == 1 and r['errors'] == 0, (
        f"too_short={r['too_short']} errors={r['errors']}：新上市被算成失敗了"
    )




def test_涵蓋率底線擋得住分母被帶走(tmp_path, monkeypatch):
    short, full = _good_frame(30), _good_frame(150)
    universe = [str(1000 + i) for i in range(100)] + [str(9000 + i) for i in range(100)]

    def mixed(ticker, *a, **k):
        return short.copy() if str(ticker).startswith('9') else full.copy()

    code = _main_with(tmp_path, universe, mixed, monkeypatch)
    assert code == 2, (
        '一半的母體沒有進到報告裡，門檻卻說健康——because ok_ratio 的分母'
        '已經被那一半自己帶走了'
    )


def test_煙霧測試的小母體不受底線影響(tmp_path, monkeypatch):
    short, full = _good_frame(30), _good_frame(150)
    universe = ['1101', '1102', '1103', '1104', '1105', '1106',
                '9901', '9902', '9903', '9904']

    def mixed(ticker, *a, **k):
        return short.copy() if str(ticker).startswith('99') else full.copy()

    assert _main_with(tmp_path, universe, mixed, monkeypatch) == 0



def test_零檔過篩照樣要寫出_index_html(tmp_path):
    def fine(*a, **k):
        return _good_frame(days=150)

    out = tmp_path / "site"
    out.mkdir(parents=True, exist_ok=True)
    orig_cwd = os.getcwd()
    orig_universe, orig_download = pl.load_tw_stock_universe, pl.yf.download
    try:
        os.chdir(out)
        pl.load_tw_stock_universe = lambda *a, **k: (
            [f'{c}.TW' for c in ('1101', '1102', '1103')],
            {f'{c}.TW': f'測試{c}' for c in ('1101', '1102', '1103')},
            {c: '測試業' for c in ('1101', '1102', '1103')},
        )
        pl.yf.download = fine
        res = pl.run(str(out), workers=2, retry_rounds=(),
                     open_when_done=False, plotly_cdn=False,
                     index_copy='index.html')
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_universe, orig_download
        os.chdir(orig_cwd)

    assert len(res['results']) == 0, '這個 fixture 應該一檔都不過'
    idx = out / 'index.html'
    assert idx.exists() and idx.stat().st_size > 0, (
        '零檔過篩就沒有 index.html——排程發布那一步會紅，網站當天不更新'
    )
    html = idx.read_text(encoding='utf-8')
    snap = html.split('id="tf-snap">')[1].split('</script>')[0]
    assert len(json.loads(snap)) == 3, (
        f'零檔的那一天快照裡只有 {len(json.loads(snap))} 檔——'
        '側欄畫不出東西，調門檻也篩不出東西'
    )


def test_零檔過篩不等於不健康(tmp_path, monkeypatch):
    def fine(*a, **k):
        return _good_frame(days=150)

    code = _main_with(tmp_path, ['1101', '1102', '1103'], fine, monkeypatch)
    assert code == 0, f'零檔過篩被當成失敗了（結束碼 {code}）'





def test_預設併發是四不是八():
    import inspect

    assert pl.DEFAULT_WORKERS == 4, (
        f'預設併發變成 {pl.DEFAULT_WORKERS} 了。調高之前先量涵蓋率：'
        '失敗不會報錯，只會讓上櫃從名單上消失。'
    )
    assert inspect.signature(pl.run).parameters['workers'].default == pl.DEFAULT_WORKERS
