"""這一趟到底算不算數——健康門檻。

## 為什麼這一支要單獨存在

`__main__` 裡那道門檻的下一步是 `git push -f origin report`，而 `report` 是一條
**只有一個 commit 的孤兒分支**。門檻放行一次，昨天那份好的報告就永久消失。

而這道門檻寫好之後有一段時間是**完全沒有作用的**，外表完全看不出來：

    _scanned[0] += 1        # 在呼叫端，無條件

`screen_stock` 有三種回 `None` 的方式（下載失敗／歷史不足／今天沒過篩），呼叫端
一種都分不出來，所以 `scanned` 恆等於母體大小，`ok_ratio` 恆為 100%。Yahoo 整天
不給資料 → 1,900 檔全部下載失敗 → `healthy=yes` → force push → 昨天那份被蓋成
一份「今天 0 檔通過」。

**而「0 檔通過」在台股是一個合理的日子。** 所以 job 全綠、摘要正常、Excel 附件
也在，沒有任何一個地方看起來不對。這是整個專案裡唯一一個「壞掉會弄丟東西、而且
永遠不會被發現」的地方。

## 這裡守的是什麼

不是「那個公式算得對不對」——公式很短，看一眼就知道。守的是**那幾個數字的意思
有沒有被改掉**。上一版的 bug 不在公式裡，在「`scanned` 到底數的是什麼」。所以
最重要的是 `test_每一檔都要記到帳上`：它不檢查任何門檻，只檢查
`scanned + errors + too_short == universe`。將來有人在 `screen_stock` 裡加第四種
`return None` 而忘了記帳，這一條會先紅。
"""

import json
import os

import pandas as pd

import tw_trend_filter.pipeline as pl
from tw_trend_filter.__main__ import main


def _good_frame(days=150):
    """一檔資料完整、算得動的假股票。過不過篩不重要，重要的是「掃得到」。"""
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
    """跑一趟 `pipeline.run`，母體與下載行為都由呼叫端決定。"""
    orig_universe, orig_download = pl.load_tw_stock_universe, pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            [f'{c}.TW' for c in universe],
            {f'{c}.TW': f'測試{c}' for c in universe},
            {c: '測試業' for c in universe},
        )
        pl.yf.download = download
        # `retry_rounds=()`：這幾條測試的下載函式是**故意**丟例外的，而補問
        # 對「故意壞掉」沒有意義，只會讓每一條多等 135 秒。補問本身由
        # tests/test_retry.py 守。
        return pl.run(str(tmp_path), make_excel=False, workers=2,
                      retry_rounds=(), open_when_done=False, plotly_cdn=False)
    finally:
        pl.load_tw_stock_universe = orig_universe
        pl.yf.download = orig_download


def _main_with(tmp_path, universe, download, monkeypatch):
    """同上，但走完整的 `main()`，拿到的是**結束碼**。

    直接測 `pipeline.run` 的回傳值不夠：上一版的 bug 在 run 回傳的數字裡就已經
    是錯的了，但真正造成損害的是 `main` 拿那些數字算出來的 `healthy`，而那一段
    當時一條測試都沒有。
    """
    monkeypatch.setattr(pl, 'load_tw_stock_universe', lambda *a, **k: (
        [f'{c}.TW' for c in universe],
        {f'{c}.TW': f'測試{c}' for c in universe},
        {c: '測試業' for c in universe},
    ))
    monkeypatch.setattr(pl.yf, 'download', download)
    return main(['--output-dir', str(tmp_path), '--no-excel',
                 '--workers', '2', '--offline-plotly', '--no-retry'])


# ---------------------------------------------------------------------------
# 迴歸：上一版在這一條上是綠的，而它不該是


def test_全市場下載失敗不可以判定成健康(tmp_path, monkeypatch):
    """這就是那個 bug。

    上一版跑這一條會得到 `scanned=5, universe=5, errors=0, healthy=yes`——
    也就是「五檔全部沒問到」和「五檔全部問到、只是都沒過篩」在門檻眼裡一模一樣。
    """
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
    """yfinance 偶爾漏一兩檔是常態。門檻擋的是系統性失敗，不是雜訊。

    這一條和上面那一條要一起看：只有上面那條的話，把門檻寫成「有任何一檔失敗
    就 return 2」也會過，而那種門檻每天都紅，紅到沒有人看為止。
    """
    frame = _good_frame()
    calls = {'n': 0}

    def flaky(*a, **k):
        calls['n'] += 1
        if calls['n'] == 1:          # 二十檔裡漏第一檔
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


# ---------------------------------------------------------------------------
# 新上市不是失敗


def test_歷史不足的新上市不算失敗(tmp_path, monkeypatch):
    """60MA 要 60 根，所以不足 65 根的直接跳過——那是「沒得掃」不是「沒掃到」。

    把它們算進分母的話，掛牌潮那幾週門檻會在一個和資料品質完全無關的理由上誤觸。
    而誤觸一次之後，就沒有人再相信這個門檻了——這比沒有門檻更糟，因為它看起來
    還在那裡。
    """
    short, full = _good_frame(days=30), _good_frame(days=150)

    def mixed(ticker, *a, **k):
        return short.copy() if str(ticker).startswith('99') else full.copy()

    # 十檔裡四檔是新上市：ok_ratio 若用母體當分母只有 60%，會誤判成不健康。
    universe = ['1101', '1102', '1103', '1104', '1105', '1106',
                '9901', '9902', '9903', '9904']
    code = _main_with(tmp_path, universe, mixed, monkeypatch)
    assert code == 0, '新上市被算成失敗了——分母要扣掉 too_short'


# ---------------------------------------------------------------------------
# 結構守門：這一條比上面任何一條都重要


def test_每一檔都要記到帳上(tmp_path):
    """`scanned + errors + too_short` 必須等於 `universe`。

    上一版的 bug 不在公式裡，在「有一條路沒有記帳」——下載失敗那條路既不加
    `scanned` 也不加 `errors`，於是它從帳上消失了，而消失的方式剛好讓門檻恆真。

    這一條不檢查任何門檻，只檢查記帳。將來有人在 `screen_stock` 裡加第四種
    `return None`（例如「這檔停牌」「這檔是 ETF」）而忘了計數，這裡會先紅——
    而那時候門檻還是綠的，因為少記的那一檔正好也不算進分母。
    """
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
    """直接釘住「`scanned` 數的是什麼」。

    這是上一版唯一錯的那件事，而它錯得很自然：`_screen_and_collect` 跑完了，
    看起來就是掃過一檔。所以這裡用一個**一定不等於母體**的場景把它釘死。
    """
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


# ---------------------------------------------------------------------------
# workflow 讀得到的那幾個值


def test_摘要要寫出_healthy_和拆解過的數字(tmp_path, monkeypatch):
    """`daily.yml` 用 `healthy != 'no'` 決定要不要 push，所以這幾行寫錯＝門檻失效。"""
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


# 備註給以後抄這一支的人：這個 repo 用 pytest（`ci.yml` 是 `pytest tests -q`），
# 所以 parametrize 可以用。隔壁的 tw-six-metrics **不行**——那邊的
# `scripts/run_tests.py` 是自製 runner，只收「零參數的 test_ 函式」，
# parametrize 的那一個會以 TypeError 收場，而那個錯誤看起來像測試寫壞了。


# ---------------------------------------------------------------------------
# 抓不到的時候，yfinance 回的是**空的 DataFrame**，不是 None
#
# 上面那幾條都用「丟例外」模擬下載失敗，而真實世界的失敗**不長那個樣子**：
# `yf.download('9999.TWO', ...)` 回的是一個型別為 DataFrame、`is None` 為 False、
# `len()` 為 0 的東西。實測確認過。
#
# 於是 `screen_stock` 的判斷順序決定了一切。曾經是先問 `df is None`（永遠為假）
# 再問 `len(df) < 65`（0 < 65 為真），所以**每一檔沒抓到的股票都被記成「新上市」**
# ——而健康門檻會把 too_short 從分母裡扣掉，比例於是永遠是 100%。
#
# 生產環境的結果：2026-09-20 的報告只有 543/891 檔上櫃（60.9%），照常發布。


def _empty_frame():
    """yfinance 抓不到的時候真正回的東西。"""
    return pd.DataFrame()


def test_空的回應要記成下載失敗不是新上市(tmp_path):
    """這一條是整個修正的重點。

    `errors` 會讓門檻變紅，`too_short` 會把分母縮小讓門檻變綠——記錯一格，
    同一份壞掉的資料從「擋下來」變成「照常發布」。
    """
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
    """記帳修好之後，門檻要真的擋得住。

    比例：五檔裡三檔空的 → scanned 2 / scannable 5 = 40%，遠低於 90%。
    """
    good = _good_frame(150)

    def mixed(ticker, *a, **k):
        return _empty_frame() if str(ticker).startswith('88') else good.copy()

    code = _main_with(tmp_path, ['1101', '1102', '8801', '8802', '8803'],
                      mixed, monkeypatch)
    assert code == 2, (
        '三分之二的股票沒抓到，門檻卻說健康。下一步就是 force push 蓋掉昨天。'
    )


def test_真的新上市仍然不算失敗(tmp_path):
    """不要修過頭：1~64 根是新上市，0 根才是沒問到。

    兩者都「不足 65 根」，但一個是市場的事實，一個是我們的失敗。
    """
    short, full = _good_frame(30), _good_frame(150)

    def mixed(ticker, *a, **k):
        return short.copy() if str(ticker).startswith('99') else full.copy()

    r = _run_with(tmp_path, ['1101', '1102', '9901'], mixed)
    assert r['too_short'] == 1 and r['errors'] == 0, (
        f"too_short={r['too_short']} errors={r['errors']}：新上市被算成失敗了"
    )


# ---------------------------------------------------------------------------
# 分母的底線


def test_涵蓋率底線擋得住分母被帶走(tmp_path, monkeypatch):
    """就算有人又把某一種失敗記進 too_short，也不能整份發布出去。

    `ok_ratio` 是「該掃的裡面掃到幾成」，而 too_short 會縮小分母——只要失敗
    被誤記成 too_short，這個比例就會自己變成 100%。所以再加一條不看分母的
    底線：掃到的對**整個母體**至少要有八成。

    這裡用 200 檔（真的在掃全市場的規模），其中 100 檔只有 30 根。
    `ok_ratio` 是 100/100 ＝ 100%，但 coverage 只有 50%。
    """
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
    """`--limit 10` 那種跑法母體只有十檔，兩三檔太新就跌破八成。

    而那一趟要證明的是「整條路走不走得通」，不是「今天蓋到多少市場」。
    門檻誤觸一次就沒有人再相信它。
    """
    short, full = _good_frame(30), _good_frame(150)
    universe = ['1101', '1102', '1103', '1104', '1105', '1106',
                '9901', '9902', '9903', '9904']

    def mixed(ticker, *a, **k):
        return short.copy() if str(ticker).startswith('99') else full.copy()

    assert _main_with(tmp_path, universe, mixed, monkeypatch) == 0


# ── 零檔過篩的那一天 ───────────────────────────────────────────────────

def test_零檔過篩照樣要寫出_index_html(tmp_path):
    """排程發布那一步就是 `test -s index.html`——沒有這個檔案，整條紅掉。

    這一條守的是那一整串的最後一環：資料抓得好好的（全部掃到、沒有失敗），
    只是今天沒有任何一檔同時過四關——那是一個**正常的交易日**，網站不該因此
    停止更新一天。

    實際發生過一次：0 檔通過、1,969/1,988 檔掃到，而發布那一步紅在
    「沒有產生 index.html」。
    """
    def fine(*a, **k):
        return _good_frame(days=150)      # 一條直線，過不了「突破」那一關

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
        res = pl.run(str(out), make_excel=False, workers=2, retry_rounds=(),
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
    # 產出來還不夠，還要**能用**。零檔那一天頁面的全部價值就在「當場調門檻
    # 重篩」，而那件事吃的是快照——快照是空的話，這一頁打得開、什麼都調不出來，
    # 而那和沒有這一頁的差別，只有排程那一步看得出來。
    html = idx.read_text(encoding='utf-8')
    snap = html.split('id="tf-snap">')[1].split('</script>')[0]
    assert len(json.loads(snap)) == 3, (
        f'零檔的那一天快照裡只有 {len(json.loads(snap))} 檔——'
        '側欄畫不出東西，調門檻也篩不出東西'
    )


def test_零檔過篩不等於不健康(tmp_path, monkeypatch):
    """全部掃到、零檔通過 → 結束碼 0。

    「今天沒有標的」和「今天沒抓到資料」是兩件完全不同的事，而健康門檻只該
    管後者。把前者也算成失敗的話，空頭走勢裡連著幾天都會是紅的，而紅到後來
    就沒有人看那個燈號了。
    """
    def fine(*a, **k):
        return _good_frame(days=150)

    code = _main_with(tmp_path, ['1101', '1102', '1103'], fine, monkeypatch)
    assert code == 0, f'零檔過篩被當成失敗了（結束碼 {code}）'



# ---------------------------------------------------------------------------
# 併發


def test_預設併發是四不是八():
    """8 workers 會被 Yahoo 靜靜地擋掉三分之一，而且**沒有比較快**。

    全市場 1,979 檔、同一台機器實測：

        8 workers   1.0～1.6 分   成功 1,276～1,607（64.5%～81.2%）
        4 workers   1.7 分        成功 1,975（99.8%）

    8 那一趟之所以看起來快，正是因為它有幾百檔根本沒問到——空的回應是**立刻**
    回來的，不用等 timeout。跑得越快、失敗越多、看起來越快。
    """
    import inspect

    assert pl.DEFAULT_WORKERS == 4, (
        f'預設併發變成 {pl.DEFAULT_WORKERS} 了。調高之前先量涵蓋率：'
        '失敗不會報錯，只會讓上櫃從名單上消失。'
    )
    assert inspect.signature(pl.run).parameters['workers'].default == pl.DEFAULT_WORKERS
