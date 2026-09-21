"""哪一趟可以拿去蓋掉昨天那份報告。

## 為什麼這一組測試存在

`daily.yml` 用 `git push -f` 把報告推到一條**只有一個 commit** 的孤兒分支。
覆蓋掉就沒有第二份，沒有還原的路。而它的安全帶原本只有一個 `healthy`——
一個只回答「這一趟的資料可信嗎」的旗標，對「這一趟是誰、為了什麼跑的」
一無所知。

實測（2026-09-21）兩條會把正式報告蓋掉的路：

    按 Run workflow 填 limit=50
      → universe 50 → full_run 為假 → 八成覆蓋率那條整個關掉
      → coverage=1.000 healthy=yes → 50 檔版上線

    手動改一組門檻試跑
      → healthy=yes → 非預設門檻的報告上線，而網站上看不出它和平常不一樣

第三條更難看見：`screen_stock` 曾經在指標算完**之前**就記 `scanned`，於是
上游 cross feed 的 schema 一漂移，同一檔同時算進 scanned 和 errors——

    真的掃到 10/10 檔   ⚠️ 有 3 檔沒有掃成   snapshots 只有 7 筆
    ok_ratio 1.0   coverage 1.0   healthy=yes

報告裡少掉三成，門檻說 100%。這是「健康門檻從來沒有作用過」那個 bug 的
鏡像版，而擋住它的是一個**不變式**而不是一個門檻：
`scanned + errors + too_short` 必須等於母體。
"""
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
    # `main()` 正常結束是 `return 0`，不是 SystemExit——只有 argparse 的錯誤
    # 才會丟。所以兩種都接住。
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
    """`--limit 50`：universe 被縮成 50，八成覆蓋率那條就被關掉了。

    這一趟**可以**是 healthy（它要證明的是整條路走不走得通），但它絕對
    不可以蓋掉全市場那一份。
    """
    got = _outputs(tmp_path, monkeypatch, argv=['--limit', '50'],
                   scanned=50, universe=50)
    assert got['healthy'] == 'yes', '煙霧測試不該被判成不健康'
    assert got['limited'] == 'yes'
    assert got['publishable'] == 'no', '50 檔的煙霧測試會蓋掉正式報告'


def test_大一點的煙霧測試也不可以發布(tmp_path, monkeypatch):
    """`--limit 500`：母體 500 檔 ≥ 100，所以 `full_run` 是真的。

    上面那一條（limit 50）擋得住是因為 `full_run` 為假，不是因為看了 limit
    ——把 `limited` 那一項拿掉，它照樣是綠的（試過）。這一條才守得住
    「有 limit 就不准發布」這件事本身。
    """
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
    """`scanned + errors + too_short != universe` ＝ 有一條路沒記帳或記了兩次。

    這是不變式，不是門檻——沒有容忍度。
    """
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
    """release tag、commit 訊息、報告標題以前全印「跑的那一天」。

    週末或盤中跑的時候，資料其實是上一個交易日的。
    """
    got = _outputs(tmp_path, monkeypatch, asof='2026-09-18')
    assert got['asof'] == '2026-09-18', got
    assert got['date'] == '2026-09-21'


def test_沒有砍資料的日子asof就是當天(tmp_path, monkeypatch):
    got = _outputs(tmp_path, monkeypatch)
    assert got['asof'] == '2026-09-21', '沒砍資料卻寫了別的日期'


# ---------------------------------------------------------------------------
# workflow 那一邊


def _daily():
    return (ROOT / '.github/workflows/daily.yml').read_text(encoding='utf-8')


def test_發布的每一步都看publishable():
    """五個發布步驟（report 分支、Pages 的四步）都要換掉安全帶。

    漏掉一個的症狀是「報告分支是昨天的、Pages 是煙霧測試的」——兩邊不同步，
    而且兩個 job 都是綠的。
    """
    y = _daily()
    assert "steps.screen.outputs.healthy == 'yes'" not in y, (
        '還有步驟用 healthy 當安全帶'
    )
    assert y.count("steps.screen.outputs.publishable == 'yes'") == 5, (
        f"只有 {y.count(chr(34))} 個步驟改成 publishable"
    )


def test_release的tag用資料基準日():
    """同一天跑兩趟、一趟砍到昨天，tag 會撞名。"""
    y = _daily()
    assert 'DAY: ${{ steps.screen.outputs.asof' in y, y[:0] or 'tag 還在用 date'
