"""四部曲的門檻可以調，而且報告會說出它是用哪一組跑的。

這一支守的不是「參數傳得進去」——那種錯會當掉。守的是**參數傳進去了、報告卻
還在講預設值**：那份 Excel 第一分頁會白紙黑字寫著「當日量 ≥ 20 日均量 × 1.2」，
而實際跑的是 1.1。它不會當掉，不會變紅，只會讓一份報告安靜地和自己不一致。

同一類錯誤在 tw-six-metrics 踩過一次：`yearly_limit` 的說明寫 300、input 預設
300、排程實際用 60。唯一的症狀是「它比你以為的慢」。
"""

import subprocess
import sys

from tw_trend_filter.pipeline import DEFAULT_RULES, Rules


def test_預設值就是原本寫死在程式裡的那幾個數字():
    """改成可調的那一刻，預設跑法必須一個字都沒變。

    這幾個數字是從 `screen_stock` 的舊版原始碼抄下來的，不是從 `Rules` 抄的
    ——從 `Rules` 抄等於拿它自己證明它自己。
    """
    r = DEFAULT_RULES
    assert r.min_price == 10
    assert r.min_vol20 == 1000
    assert r.min_amount == 50_000_000
    assert r.lookback == 10
    assert r.squeeze == 0.12
    assert r.vol_ratio == 1.2
    assert r.atr_stop == 3.0


def test_預設的說明文字和改成可調之前逐字相同():
    """Excel 第一分頁那四行以前是寫死的。改成算出來的之後，預設值要印出同一份。

    逐字比對，不是「看起來差不多」：這四行是使用者判斷「這份報告是怎麼篩的」
    的唯一依據，少一個逗號或把「5,000 萬元」寫成「50,000,000 元」都是換了一份
    文件。
    """
    assert DEFAULT_RULES.describe() == (
        ('① 基礎流動性防禦',
         '股價 > 10 元 ｜ 20日均量 > 1,000 張 ｜ 日均成交金額 > 5,000 萬元'),
        ('② 趨勢多頭確認',
         '收盤站穩季線(60MA)之上，且月線(20MA) > 季線(60MA)'),
        ('③ 關鍵發動時機',
         '過去 10 日內：月季線黃金交叉 或 布林頻寬壓縮 ≤ 12%'),
        ('④ 強勢突破＋爆量',
         '收盤突破布林上軌 或 創 20 日新高，且當日量 ≥ 20 日均量 × 1.2'),
    )


def test_改了門檻說明就跟著改():
    """這是整支模組存在的理由：門檻改了而報告還在講預設值，是最貴的那一種錯。"""
    r = Rules(vol_ratio=1.1, squeeze=0.15, min_amount=30_000_000, lookback=5)
    text = dict(r.describe())
    assert '× 1.1' in text['④ 強勢突破＋爆量']
    assert '1.2' not in text['④ 強勢突破＋爆量']
    assert '≤ 15%' in text['③ 關鍵發動時機']
    assert '過去 5 日內' in text['③ 關鍵發動時機']
    assert '3,000 萬元' in text['① 基礎流動性防禦']
    # ② 完全不吃門檻——它比的是兩條均線的相對位置，沒有可調的數字。
    assert text['② 趨勢多頭確認'] == dict(DEFAULT_RULES.describe())['② 趨勢多頭確認']


def test_停損倍數也會寫進進出場策略那一段():
    """`3 × ATR(14)` 出現在兩個地方：算出來的停損價，和策略說明那一行。

    只改其中一個的話，Excel 上會出現「停損 = 收盤 − 3 × ATR」配著一個用 2 倍
    算出來的數字，而那兩個數字就印在同一張表上。
    """
    assert Rules(atr_stop=2.0).changed() == {'atr_stop': (3.0, 2.0)}


def test_照預設跑的時候_changed_是空的():
    """log 上那一行要能分出「預設」和「有人動過」，所以空字典是有意義的答案。"""
    assert DEFAULT_RULES.changed() == {}
    assert Rules().changed() == {}
    assert Rules(min_price=20).changed() == {'min_price': (10.0, 20.0)}


def test_命令列的預設值是從_Rules_拿的_不是另外寫一份():
    """兩個地方各寫一份預設，改了一邊就會出現「說明寫 A、實際跑 B」。

    `--help` 印出來的那幾個數字必須等於 `Rules` 的欄位值。這條測試跑的是真的
    argparse，所以它驗的是使用者真的會看到的那份文字。
    """
    import os
    import re

    # COLUMNS 釘住：argparse 照終端機寬度折行，而 CI 的寬度不等於本機的。
    # 折到一半的「預設 1,0\n00」會讓這條測試因為排版而紅，而排版不是它要守的東西。
    env = dict(os.environ, COLUMNS='100')
    out = subprocess.run(
        [sys.executable, '-m', 'tw_trend_filter', '--help'],
        capture_output=True, text=True, check=True, env=env,
    ).stdout
    # 再把所有連續空白（含換行）壓成一個空格，這樣就算哪天 argparse 換了折行
    # 規則也還是比得到。
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
    """讓使用者打 50000000 是在請他數零，而多打一個零不會有任何錯誤訊息。"""
    from tw_trend_filter.__main__ import main  # noqa: PLC0415

    assert callable(main)
    src = open(
        __import__('tw_trend_filter.__main__', fromlist=['x']).__file__,
        encoding='utf-8',
    ).read()
    assert 'args.min_amount_m * 1e6' in src, '換算不見了，門檻會小一百萬倍'


def test_排程不帶任何門檻_所以每天跑的永遠是預設():
    """這幾個輸入只在手動觸發時有東西。

    留空就不加旗標，argparse 會用 `Rules` 的預設——所以「預設值」在整條路上
    只有一個地方寫著，而那個地方是程式。如果 workflow 改成把數字寫死在 env 的
    fallback 裡，這條就該紅：那等於在第二個地方又寫了一份預設。
    """
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
        # env 那一行不可以有 `|| 某個數字`——那就是第二份預設。
        m = re.search(rf'\$\{{\{{\s*github\.event\.inputs\.{name}\s*(\|\|[^}}]*)?\}}\}}', wf)
        assert m, f'{name} 的 env 取值寫法找不到'
        assert not m.group(1), f'{name} 在 workflow 裡又寫了一份預設：{m.group(1)}'


# ── 門檻真的有在擋 ──────────────────────────────────────────────────────
#
# 上面那些測的是「說明文字跟著門檻走」。這一段測的是另一半：門檻改了，**答案**
# 也要跟著改。兩半都要，因為它們各自壞掉的樣子不一樣——說明沒跟上是報告說謊，
# 答案沒跟上是參數根本沒接到線。

def _synthetic(days=150, jump=110.0, flat=100.0, vol=600_000, vol_last=900_000,
               slope=0.0):
    """一檔剛好通過四部曲的假股票：橫盤或緩漲，最後一天帶量突破。

    用合成資料而不是抓真的股票，是因為這條測試要問的是「門檻擋不擋得住」，
    而真實資料每天都在變——今天過的那一檔明天可能不過，那時候紅的是測試，
    原因卻和程式無關。

    `slope` 決定 ③ 是由哪一邊成立的，而這一點是寫這支測試時才發現的：

    * `slope=0`（橫盤）：20MA 與 60MA 一路重疊，最後一天的跳空讓 20MA 第一次
      站上 60MA——所以它同時觸發**黃金交叉**和**布林壓縮**。拿它去測壓縮的門檻
      會得到一個永遠綠的測試，因為交叉那一邊照樣讓它通過。
    * `slope>0`（緩漲）：20MA 早就在 60MA 之上，回看視窗裡沒有交叉，③ 只剩
      壓縮這一條路——那才驗得到壓縮的門檻。
    """
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
    """最後一天要跳到哪裡：橫盤時就是 `jump`，緩漲時要在趨勢線之上再跳一段。

    寫成一個函式是因為「突破」必須相對於**前 20 天的高點**，而緩漲的前 20 天
    高點本來就在 `flat` 之上——直接用 110 的話，走了 150 天緩漲之後那根本不是
    突破，測試會因為錯的理由變紅。
    """
    return flat + slope * (days - 2) + (jump - flat)


def _run_with(rules, tmp_path, frame=None, want='count'):
    """跑一次 `run()`，母體只有一檔、資料是合成的、不產 Excel。回傳通過幾檔。"""
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
            str(tmp_path), make_excel=False, workers=1, rules=rules,
        )
    finally:
        pl.load_tw_stock_universe = orig_universe
        pl.yf.download = orig_download
    if want == 'trigger':
        return out['results'][0]['trigger'] if out['results'] else ''
    return out['count']


def test_預設門檻讓這檔合成股通過(tmp_path):
    """先證明這份合成資料本來是過的——不然下面每一條都會因為錯的理由變綠。"""
    assert _run_with(DEFAULT_RULES, tmp_path) == 1


def test_量比門檻拉高就擋下來(tmp_path):
    """合成的量比是 900,000 / 615,000 ≈ 1.46。門檻 1.5 就該擋住。"""
    assert _run_with(Rules(vol_ratio=1.5), tmp_path) == 0
    assert _run_with(Rules(vol_ratio=1.4), tmp_path) == 1


def test_成交額門檻拉高就擋下來(tmp_path):
    """合成的 20 日均額約 6,195 萬。門檻一億就該擋住。"""
    assert _run_with(Rules(min_amount=100_000_000), tmp_path) == 0


def test_橫盤那一檔是交叉與壓縮同時成立的(tmp_path):
    """③ 是「黃金交叉 **或** 壓縮」，而橫盤那份合成資料兩邊都成立。

    這條寫出來是因為它一開始騙過了我：我拿橫盤那份去測壓縮的門檻，把壓縮關到
    負數，結果還是通過——原因不是門檻沒接上，是交叉那一邊照樣讓它過。一個
    「或」的兩條路，只堵一條測不出東西。
    """
    flat = _run_with(DEFAULT_RULES, tmp_path / 'flat', want='trigger')
    assert '黃金交叉' in flat and '布林壓縮' in flat, flat
    # 緩漲那一份只剩壓縮——下一條測試就是靠這個差別才驗得到壓縮的門檻。
    ramp = _run_with(DEFAULT_RULES, tmp_path / 'ramp',
                     frame=_synthetic(slope=0.05), want='trigger')
    assert '黃金交叉' not in ramp and '布林壓縮' in ramp, ramp


def test_壓縮門檻收緊就擋下來(tmp_path):
    """緩漲那一檔回看視窗裡沒有交叉，所以 ③ 只剩壓縮這一條路——堵住就不過。"""
    ramp = _synthetic(slope=0.05)
    assert _run_with(DEFAULT_RULES, tmp_path / 'ok', frame=ramp) == 1
    assert _run_with(Rules(squeeze=-0.01), tmp_path / 'no', frame=ramp) == 0


def test_停損倍數改了_停損價就跟著改(tmp_path):
    """停損不是門檻，改它不影響誰通過——但它必須真的用上去。

    原本是寫死的 `price - 3*atr`。接錯線的話這個數字不會變，而畫面上它只是
    一個看起來很合理的價格。
    """
    import tw_trend_filter.pipeline as pl

    df = _synthetic()
    orig_universe, orig_download = pl.load_tw_stock_universe, pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            ['1111.TW'], {'1111': '測試股'}, {'1111': '測試業'}
        )
        pl.yf.download = lambda *a, **k: df.copy()
        three = pl.run(str(tmp_path / 'a'), make_excel=False, workers=1,
                       rules=Rules())['results'][0]
        one = pl.run(str(tmp_path / 'b'), make_excel=False, workers=1,
                     rules=Rules(atr_stop=1.0))['results'][0]
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_universe, orig_download

    atr = three['atr14']
    assert abs(three['stop_loss'] - (three['close'] - 3 * atr)) < 0.02
    assert abs(one['stop_loss'] - (one['close'] - 1 * atr)) < 0.02
    assert one['stop_loss'] > three['stop_loss'], '倍數變小，停損該往上移'


def test_workflow_的_run_區塊裡不可以有空的表達式樣板():
    """GitHub 把整個 `run` 區塊當文字掃過去找表達式樣板，**不認得 shell 註解**。

    所以一行寫在 `#` 後面、用來解釋樣板長什麼樣的註解，會被當成真的樣板去求值。
    空的那一組讓整個 workflow 以「Invalid workflow file: An expression was
    expected」被擋掉——而錯誤指的行號是 `run:` 那一行，不是註解那一行，所以現場
    看起來像是整段 shell 有問題。

    **YAML 的註解不算**：那是 YAML 解析器在 Actions 看到之前就丟掉的，所以
    `# 這裡寫一組空樣板` 寫在 YAML 註解裡完全沒事。這個差別正是當初診斷卡住的
    地方，所以這條測試是解析過 YAML 之後才掃字串值的——用 grep 掃原始檔的話，
    它會對著一行無害的註解喊失敗，而那種假警報遲早會被關掉。
    """
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
    """「上游今天不給資料」和「程式壞了」不可以長得一樣。

    以前 `load_tw_stock_universe` 抓不到就 raise RuntimeError，一路冒出 main、
    traceback、exit 1。於是交易所心情不好的那幾天 CI 就是紅的——而一個會因為
    別人的網站而變紅的守門，遲早會被當成雜訊，那時候真的壞掉的那一次也會一起
    被忽略。

    這個 repo 本來就有一套講這件事的約定（0 算數／2 跑完了但不可信／其他是真的
    壞了），只是母體那條路繞過了它。
    """
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
    """把母體那條路歸到 2，不可以順手把別的例外也吞掉。

    吞掉的話，CI 從「會因為別人而紅」變成「永遠不紅」——後者更糟：前者是雜訊，
    後者是一個什麼都不守的守門。
    """
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


# ── Excel 那條路 ────────────────────────────────────────────────────────
#
# 上面每一條 `_run_with` 都傳 `make_excel=False`——為了快。代價是**整個 Excel
# 路徑一次都沒被跑過**，而 `describe()` 存在的唯一理由就是它要印進 Excel 的第一
# 分頁。十四條測試守著一個從來沒有被執行的承諾。
#
# 漏掉的東西當場就咬人了：`run()` 裡面有一個區域變數也叫 `rules`（進出場策略
# 那張表），它把參數蓋掉，於是函式結尾的 `rules.changed()` 炸成「'list' object
# has no attribute 'changed'」。**整趟跑完、Excel 都存好之後才炸**，log 上一路
# 綠到最後一行，只有結束碼是 1。CI 紅了兩次才找到。

def _xlsx_strategy_rows(path):
    """Excel 第一分頁上那四行「四部曲篩選機制說明」：``[(標籤, 說明), ...]``。"""
    from openpyxl import load_workbook

    ws = load_workbook(path).worksheets[0]
    return [(ws.cell(i, 2).value, ws.cell(i, 3).value) for i in range(6, 10)]


def _run_making_excel(rules, tmp_path):
    """跑一次**有產 Excel** 的完整流程，回傳 ``(result, xlsx 路徑)``。

    資料故意用一檔怎麼樣都過不了篩的（股價 5 元，卡在 ① 流動性）：這樣不會去畫
    個股 K 線圖，Excel 幾秒就好，而第一分頁那四行說明照樣會寫出來——它跟通過
    幾檔無關。
    """
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
        out = pl.run(str(tmp_path), workers=1, rules=rules)
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_u, orig_d
    return out, out['xlsx']


def test_跑完整趟_含_Excel_不會在最後一刻炸掉(tmp_path):
    """`run()` 要回得了那個 dict。

    這條看起來什麼都沒驗，但它是這一整段存在的理由：區域變數蓋掉參數的那個 bug
    ，log 上一路正常到最後一行，只有結束碼是 1。而所有用 `make_excel=False` 的
    測試都碰不到它——Excel 那一段裡才有那個同名的區域變數。
    """
    out, xlsx = _run_making_excel(DEFAULT_RULES, tmp_path)
    assert xlsx and out['count'] == 0
    assert out['rules'] is DEFAULT_RULES, 'rules 被路上某個同名變數蓋掉了'
    assert out['rules_changed'] == {}


def test_Excel_第一分頁印的是這一趟真的用的門檻(tmp_path):
    """`describe()` 存在的唯一理由，就是這四行要跟著門檻走。

    前面那條 `test_改了門檻說明就跟著改` 驗的是 `describe()` 自己；這一條驗的是
    它**真的被寫進檔案裡**。中間那一段（誰呼叫它、寫到哪一格）以前沒有任何東西
    在守。
    """
    _, xlsx = _run_making_excel(DEFAULT_RULES, tmp_path / 'a')
    assert _xlsx_strategy_rows(xlsx) == list(DEFAULT_RULES.describe())

    custom = Rules(vol_ratio=1.1, squeeze=0.15, atr_stop=2.0)
    _, xlsx2 = _run_making_excel(custom, tmp_path / 'b')
    rows = dict(_xlsx_strategy_rows(xlsx2))
    assert '× 1.1' in rows['④ 強勢突破＋爆量']
    assert '≤ 15%' in rows['③ 關鍵發動時機']


def test_停損倍數也真的寫進_Excel_的進出場策略(tmp_path):
    """那一行和算出來的停損價是同一個倍數，不是兩份寫死的文字。"""
    from openpyxl import load_workbook

    _, xlsx = _run_making_excel(Rules(atr_stop=2.0), tmp_path)
    ws = load_workbook(xlsx).worksheets[0]
    entry = {ws.cell(i, 2).value: ws.cell(i, 3).value for i in range(12, 16)}
    assert '2 × ATR(14)' in entry['初始停損'], entry['初始停損']
    assert '3 × ATR' not in entry['初始停損']
