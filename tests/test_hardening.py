import gzip
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tw_trend_filter.pipeline as pl




def test_量比的分母含當天_和桌機版一樣():
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert 'vol_ratio = float(volume.iloc[-1]) / vol20 if vol20 else 0.0' in src
    assert 'vol20_base' not in src, '又有地方用排除當天的均量算量比'




def test_Donchian看收盤_和桌機版一樣():
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert 'donchian = close.rolling(20).max().shift(1)' in src
    assert "donchian = df['High'].rolling(20).max()" not in src




def test_母體有下界():
    assert pl.UNIVERSE_FLOOR['.TW'] >= 500
    assert pl.UNIVERSE_FLOOR['.TWO'] >= 400
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert 'if len(extracted) < floor:' in src
    assert 'elif len(extracted):' in src, '下界沒有真的擋住 got.add'


def test_ISIN頁用cp950解碼_碁字不會變亂碼():
    raw = '6174  安碁'.encode('cp950') + b' ' + '6690  安碁資訊'.encode('cp950')
    assert raw.decode('cp950') == '6174  安碁 6690  安碁資訊'
    assert '\ufffd' in raw.decode('big5', 'replace'), '如果 big5 也解得出來，這條測試就沒意義了'
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert "resp.encoding = 'cp950'" in src
    assert "resp.encoding = 'big5'" not in src




def _cli(*args):
    r = subprocess.run([sys.executable, '-m', 'tw_trend_filter', *args],
                       capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode, r.stderr


def test_指令打錯的結束碼不是2():
    code, err = _cli('--min-price', 'abc', '--output-dir', '/tmp/x')
    assert code == pl_exit_usage(), (code, err[-300:])
    assert code != 2


def pl_exit_usage():
    from tw_trend_filter.__main__ import EXIT_USAGE
    return EXIT_USAGE


def test_回看天數超過上限就擋下來():
    code, err = _cli('--lookback', '30', '--output-dir', '/tmp/x')
    assert code == pl_exit_usage(), (code, err[-300:])
    assert f'最多 {pl.SNAPSHOT_DAYS}' in err, err[-300:]
    assert _cli('--lookback', str(pl.SNAPSHOT_DAYS), '--help')[0] == 0


def test_workflow分得出這兩種2():
    y = (ROOT / '.github/workflows/daily.yml').read_text('utf-8')
    assert '"$code" -eq 3' in y, 'workflow 還沒分出「指令打錯」'
    assert 'workflow_dispatch' in y.split('"$code" -eq 2')[1][:600], (
        '手動觸發的不可信那一趟還是綠燈'
    )




def test_估值太舊就當作沒有(monkeypatch, tmp_path):
    import datetime as dt
    import urllib.request

    def feed(as_of):
        payload = json.dumps({'as_of': as_of, 'quarter': '2026.2Q',
                              'rows': {'2330': [3.5, 3000, 2000]}}).encode()

        class R:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return lambda *a, **k: R()

    tz = dt.timezone(dt.timedelta(hours=8))
    monkeypatch.setattr(pl, '_taipei_now',
                        lambda: dt.datetime(2026, 9, 21, 15, 0, tzinfo=tz))

    monkeypatch.setattr(urllib.request, 'urlopen', feed('2026-09-18'))
    assert pl.load_cross_feed('http://x')['rows'], '三天前的估值被丟掉了'

    monkeypatch.setattr(urllib.request, 'urlopen', feed('2026-08-20'))
    assert pl.load_cross_feed('http://x') == {}, (
        '一個月前的估值還拿去算報酬風險比'
    )


def test_估值的日期要看得到():
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert src.count('TF_CROSS_AS_OF') >= 2, (
        '那個常數還是定義完就沒有人讀，估值的新舊完全看不出來'
    )




def test_煞車有總預算():
    slept = []
    b = pl.RateLimitBreaker(streak=1, cooldown=60, max_cooldown=60,
                            max_total_sleep=180, sleep=slept.append,
                            clock=lambda: 0.0)
    for _ in range(20):
        b.note_failure('Too Many Requests')
        b.wait_if_cooling()
    assert sum(slept) <= 180 + 60, f"停了 {sum(slept)} 秒，預算是 180"
    assert pl.RATE_LIMIT_TOTAL_BUDGET > 0




def test_全市場快照要存下來(tmp_path, monkeypatch):
    from test_session import CODES, _run, _series

    base = _series()
    res = _run(tmp_path, monkeypatch, lambda *a, **k: base, probes=())
    path = res['snapshot_file']
    assert path and Path(path).exists(), '沒有寫出全市場快照'

    with gzip.open(path, 'rt', encoding='utf-8') as fh:
        snap = json.load(fh)
    assert len(snap['rows']) == len(CODES), (
        f"快照只有 {len(snap['rows'])} 檔，母體是 {len(CODES)} 檔"
        "——它要裝的是**全市場**，不是通過篩選的那幾檔"
    )
    assert snap['columns'] == list(pl.SNAPSHOT_COLUMNS)
    assert snap['rules']['vol_ratio'] == pl.DEFAULT_RULES.vol_ratio, (
        '快照沒有記下那一天是用哪一組門檻跑的'
    )
    assert Path(path).name.startswith('snapshot_')


def test_release會帶上快照():
    y = (ROOT / '.github/workflows/daily.yml').read_text('utf-8')
    assert 'output/snapshot_*.json.gz' in y, 'release 沒有帶上全市場快照'




def test_圖表資料每次重寫():
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    i = src.index('os.makedirs(data_dir, exist_ok=True)')
    assert 'rmtree(data_dir' in src[i - 600:i], '寫圖表資料之前沒有先清空'
