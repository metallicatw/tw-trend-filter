"""體檢找到的那幾道門。

每一條對應一個「程式沒有 crash、結束碼 0、而產出的東西是錯的」的路。
"""
import gzip
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tw_trend_filter.pipeline as pl


# ---------------------------------------------------------------------------
# 量比的分母


def test_量比的分母含當天_和桌機版一樣():
    """2026-09-23 改回桌機版 V3.1 的定義：今天的量 ÷ 含今天的 20 日均量。

    曾經改成「排除當天」（比較貼近教科書），結果是和桌機版每天對不起來——剛好
    落在 1.2 附近的那幾檔一邊進、一邊出（貿聯-KY 桌機 1.2145／雲端 1.1965）。
    規格是桌機上那支程式。逐檔比對見 `tests/test_desktop_parity.py`。
    """
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert 'vol_ratio = float(volume.iloc[-1]) / vol20 if vol20 else 0.0' in src
    assert 'vol20_base' not in src, '又有地方用排除當天的均量算量比'


# ---------------------------------------------------------------------------
# Donchian


def test_Donchian看收盤_和桌機版一樣():
    """2026-09-23 改回桌機版 V3.1 的定義：前 20 日**最高收盤價**。

    曾經改成最高價（README 當時這樣寫），結果 9/23 那天少了禾榮科、群聯、萊德
    光電、貿聯四檔——收盤站上前 20 日最高收盤、但還在盤中高點之下。README 已
    改成和程式一致。逐檔比對見 `tests/test_desktop_parity.py`。
    """
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert 'donchian = close.rolling(20).max().shift(1)' in src
    assert "donchian = df['High'].rolling(20).max()" not in src


# ---------------------------------------------------------------------------
# 母體的下界


def test_母體有下界():
    """原本只問「有沒有解出東西」。

    那擋得住「整個交易所掛掉」，擋不住「只回了一部分」——ISIN 回的是一整張
    HTML 表格，截斷成前 100 列一樣解得出來。而母體從 1,988 縮成 100 之後，
    健康門檻的分母跟著縮，`coverage = scanned / universe` 永遠是 100%。
    """
    assert pl.UNIVERSE_FLOOR['.TW'] >= 500
    assert pl.UNIVERSE_FLOOR['.TWO'] >= 400
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert 'if len(extracted) < floor:' in src
    assert 'elif len(extracted):' in src, '下界沒有真的擋住 got.add'


# ---------------------------------------------------------------------------
# 指令打錯不要和「跑完了但不可信」共用結束碼


def _cli(*args):
    r = subprocess.run([sys.executable, '-m', 'tw_trend_filter', *args],
                       capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode, r.stderr


def test_指令打錯的結束碼不是2():
    """2 的意思是「跑完了但不可信」，而 `daily.yml` 看到 2 會印

        ::warning::母體覆蓋率不足，這一趟不發布報告

    然後 exit 0。手動觸發時把一個門檻打錯的結果因此是：程式根本沒跑，
    摘要上卻寫「母體覆蓋率不足」，job 綠燈。
    """
    code, err = _cli('--min-price', 'abc', '--output-dir', '/tmp/x')
    assert code == pl_exit_usage(), (code, err[-300:])
    assert code != 2


def pl_exit_usage():
    from tw_trend_filter.__main__ import EXIT_USAGE
    return EXIT_USAGE


def test_回看天數超過上限就擋下來():
    """`passes()` 會把它**安靜地夾住**到 20，而 Excel 照使用者填的數字印：

        --lookback 30
        Excel：「過去 30 日內…」　摘要：「lookback 10→30」　引擎：只看 20 日

    三份文件一致地說謊。夾住改成擋下來：一個報告說 A、實際跑 B 的結果，
    比一個跑不起來的指令糟。
    """
    code, err = _cli('--lookback', '30', '--output-dir', '/tmp/x')
    assert code == pl_exit_usage(), (code, err[-300:])
    assert f'最多 {pl.SNAPSHOT_DAYS}' in err, err[-300:]
    # 上限之內照樣收。
    assert _cli('--lookback', str(pl.SNAPSHOT_DAYS), '--help')[0] == 0


def test_workflow分得出這兩種2():
    y = (ROOT / '.github/workflows/daily.yml').read_text('utf-8')
    assert '"$code" -eq 3' in y, 'workflow 還沒分出「指令打錯」'
    assert 'workflow_dispatch' in y.split('"$code" -eq 2')[1][:600], (
        '手動觸發的不可信那一趟還是綠燈'
    )


# ---------------------------------------------------------------------------
# 六大與估值：太舊就不要用


def test_估值太舊就當作沒有(monkeypatch, tmp_path):
    """這一支 07:07 UTC 跑，對面 07:41／08:47 才建站——抓到的必然是昨天那份。

    那是可接受的。但「昨天」和「上個月」之間原本沒有任何一道門：對面連續幾天
    沒發布，這邊照抓照用，沒有一個字會變紅。而 `reward_risk()` 的整段說明在
    論證「報酬風險比要用今天的股價算」。
    """
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
    """`TF_CROSS_AS_OF` 以前定義完就沒有人讀——整份原始碼裡只出現一次。"""
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    assert src.count('TF_CROSS_AS_OF') >= 2, (
        '那個常數還是定義完就沒有人讀，估值的新舊完全看不出來'
    )


# ---------------------------------------------------------------------------
# 煞車的總預算


def test_煞車有總預算():
    """單次等待有封頂（240 秒），踩煞車的次數以前沒有。

    1,900 檔在持續限流的日子最多可以踩約 380 次，而 job 的 timeout 是 90 分鐘。
    「跑不完」和「被擋掉」的結果一樣——而煞車的設計目的正是避免後者。
    """
    slept = []
    b = pl.RateLimitBreaker(streak=1, cooldown=60, max_cooldown=60,
                            max_total_sleep=180, sleep=slept.append,
                            clock=lambda: 0.0)
    for _ in range(20):
        b.note_failure('Too Many Requests')
        b.wait_if_cooling()
    assert sum(slept) <= 180 + 60, f"停了 {sum(slept)} 秒，預算是 180"
    assert pl.RATE_LIMIT_TOTAL_BUDGET > 0


# ---------------------------------------------------------------------------
# 全市場快照存檔


def test_全市場快照要存下來(tmp_path, monkeypatch):
    """報告推到孤兒分支、圖表資料覆蓋式發布、Excel 只裝通過的那幾檔。

    於是 1,900 檔的指標只活在當天那份 index.html 裡，隔天被蓋掉——
    「9/15 那天 2454 的量比是多少、卡在哪一關」隔天就永遠答不出來。
    """
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


# ---------------------------------------------------------------------------
# 圖表資料不留舊檔


def test_圖表資料每次重寫():
    """前端是「用代號直接組網址」，所以舊檔留著就是一顆定時炸彈：

    下市股票的 JSON 會一直在，點下去照樣拿得到一份幾個月前的圖。
    """
    src = (ROOT / 'tw_trend_filter/pipeline.py').read_text('utf-8')
    i = src.index('os.makedirs(data_dir, exist_ok=True)')
    assert 'rmtree(data_dir' in src[i - 600:i], '寫圖表資料之前沒有先清空'
