"""命令列進入點：``python -m tw_trend_filter``。

把旗標翻譯成 :func:`tw_trend_filter.pipeline.run` 的參數，其餘什麼都不做。所有
預設值都對準「排程要的那一種跑法」，本機想完全重現單機版就加 ``--local``。
"""

from __future__ import annotations

import argparse
import os
import sys

from .pipeline import DEFAULT_RULES, VERSION, Rules, run


def _env_excel_url() -> str:
    """Excel 報表的連結：這個 repo 的 Releases 頁。

    為什麼是 Releases，不是 artifact、不是 Google Drive：

    * **artifact** 30 天後就下載不到，而且要登入 GitHub 才拿得到。
    * **Drive** 走不通。服務帳號沒有自己的儲存空間，所以它在「我的雲端硬碟」
      底下建的檔案會算在它自己的（零）配額上——403 storageQuotaExceeded。
      官方的解法是「共用雲端硬碟」，而那是 Google Workspace 的功能，個人的
      Gmail 帳號開不出來。剩下的路是 OAuth 委派，但那要一顆會被 Google 撤銷的
      refresh token，而且排程沒有瀏覽器可以重新授權。
    * **Releases** 三件事都給：不過期、不必登入就下載得到、每天一個、歷史全留。
      而且不用任何憑證——`GITHUB_TOKEN` 是 runner 自己就有的。

    連的是**列表頁**而不是某一個 release，因為列表頁的網址在**產生報告的當下**
    就已經知道，而今天那個 release 要等上傳完才存在——報告是先產生、後上傳的。
    列表頁最新的排在最上面，往下就是前幾天的。

    （沒有用 `/releases/latest`：一個 release 都還沒有的時候它會 404，而那正是
    第一次跑的時候。列表頁永遠不會。）

    不在 Actions 底下就回空字串，報告上不會出現那顆按鈕。
    """
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com')
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    if repo:
        return f'{server}/{repo}/releases'
    return ''


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog='tw-trend-filter',
        description=f'台股順勢交易篩選系統 {VERSION}：全市場掃描，產出 Excel 與互動線圖。',
    )
    ap.add_argument('--version', action='version', version=VERSION)
    ap.add_argument('-o', '--output-dir', default='output',
                    help='產出資料夾（預設 output/）')
    ap.add_argument('--index', default='',
                    help='另外複製一份互動線圖到這個路徑（排程用 index.html）')
    ap.add_argument('--limit', type=int, default=0,
                    help='只掃前 N 檔。給煙霧測試用，0 = 全市場')
    ap.add_argument('--workers', type=int, default=8,
                    help='同時抓幾檔（預設 8）')
    ap.add_argument('--period', default='2y',
                    help='每一檔下載多長的歷史（yfinance 的 period，預設 2y）')
    ap.add_argument('--chart-years', type=float, default=2.0,
                    help='互動線圖保留最近幾年的 K 棒（預設 2）')
    ap.add_argument('--link-base', default='',
                    help='個股頁連結的前綴，例如 '
                         'https://metallicatw.github.io/tw-six-metrics/stock')
    ap.add_argument('--excel-url', default='',
                    help='報告上那顆 Excel 下載連結指向哪裡（省略時在 Actions 下自動帶入）')
    ap.add_argument('--no-excel', action='store_true',
                    help='不產生 Excel，只產生互動線圖')
    ap.add_argument('--no-excel-charts', action='store_true',
                    help='Excel 照做，但跳過第三個分頁的 K 線圖（快很多）')
    ap.add_argument('--offline-plotly', action='store_true',
                    help='把 plotly.js 內嵌進 HTML（檔案大 10 倍，但可離線開啟）')
    ap.add_argument('--local', action='store_true',
                    help='重現本機單機版：5 年歷史、內嵌 plotly、跑完自動開檔')

    # ── 四部曲的門檻 ──────────────────────────────────────────────────
    #
    # 預設值全部來自 `Rules`，不在這裡再寫一次——兩個地方各寫一份的話，改了一邊
    # 就會出現「說明寫 A、實際跑 B」，而那沒有任何症狀（同一個坑在 tw-six-metrics
    # 的 yearly_limit 上踩過：input 說 300、排程實際用 60，249 檔因此多排四個晚上）。
    #
    # 可調的是門檻，不是窗口長度（20MA／60MA／布林 20／Donchian 20／ATR 14）。
    # 理由寫在 `pipeline.Rules` 的 docstring 裡：窗口是這份報告的定義，Excel 欄名
    # 與 K 線圖圖例上到處都是它們。
    g = ap.add_argument_group(
        '四部曲門檻',
        '不給就照預設跑。改過的項目會印在 log 上，也會寫進 Excel 第一分頁的說明。',
    )
    d = DEFAULT_RULES
    g.add_argument('--min-price', type=float, default=d.min_price,
                   help=f'① 股價下限，元（預設 {d.min_price:g}）')
    g.add_argument('--min-vol20', type=float, default=d.min_vol20,
                   help=f'① 20 日均量下限，張（預設 {d.min_vol20:,.0f}）')
    g.add_argument('--min-amount-m', type=float, default=d.min_amount / 1e6,
                   help=f'① 20 日均成交金額下限，百萬元（預設 {d.min_amount / 1e6:g}）')
    g.add_argument('--lookback', type=int, default=d.lookback,
                   help=f'③ 往回看幾個交易日找黃金交叉／壓縮（預設 {d.lookback}）')
    g.add_argument('--squeeze', type=float, default=d.squeeze,
                   help=f'③ 布林頻寬壓縮的門檻，比例不是百分比（預設 {d.squeeze:g}）')
    g.add_argument('--vol-ratio', type=float, default=d.vol_ratio,
                   help=f'④ 當日量 ÷ 20 日均量的下限（預設 {d.vol_ratio:g}）')
    g.add_argument('--atr-stop', type=float, default=d.atr_stop,
                   help=f'停損 = 收盤 − 這個倍數 × ATR(14)（預設 {d.atr_stop:g}）')

    args = ap.parse_args(argv)

    rules = Rules(
        min_price=args.min_price,
        min_vol20=args.min_vol20,
        # 命令列收的是百萬元（`--min-amount-m 50`），內部存的是元。
        # 讓使用者打 50000000 是在請他數零。
        min_amount=args.min_amount_m * 1e6,
        lookback=args.lookback,
        squeeze=args.squeeze,
        vol_ratio=args.vol_ratio,
        atr_stop=args.atr_stop,
    )

    if args.local:
        # 單機版的三個特徵，一次打開。分開的旗標仍然有效，這一顆只是把它們
        # 綁在一起——「我要的是本機那種跑法」是一個念頭，不是三個。
        args.period = '5y'
        args.chart_years = 5.0
        args.offline_plotly = True

    result = run(
        args.output_dir,
        limit=args.limit,
        workers=args.workers,
        period=args.period,
        make_excel=not args.no_excel,
        excel_charts=not args.no_excel_charts,
        link_base=args.link_base,
        plotly_cdn=not args.offline_plotly,
        chart_years=args.chart_years,
        excel_url=args.excel_url or _env_excel_url(),
        index_copy=args.index,
        open_when_done=args.local,
        rules=rules,
    )

    # 這一趟到底算不算數。
    #
    # 原本是無條件 `return 0`，於是 1,800 檔下載失敗跟 0 檔失敗都是綠燈，
    # 而下一步是 `git push -f origin report`——一條只有一個 commit 的孤兒分支。
    # 綠燈加 force push 等於「壞掉的那天會把好的那天蓋掉，而且沒有人會知道」。
    #
    # 門檻放在 90%：yfinance 偶爾漏個幾檔是常態，漏掉十分之一就不是了。
    universe = result.get('universe') or result['scanned'] or 1
    ok_ratio = result['scanned'] / universe
    healthy  = ok_ratio >= 0.90 and result.get('errors', 0) <= universe * 0.10

    # 讓 workflow 的後續步驟拿得到「今天通過幾檔」，不必去 parse 上面那堆輸出。
    summary = os.environ.get('GITHUB_OUTPUT')
    if summary:
        with open(summary, 'a', encoding='utf-8') as fh:
            fh.write(f"count={result['count']}\n")
            fh.write(f"scanned={result['scanned']}\n")
            fh.write(f"universe={universe}\n")
            fh.write(f"errors={result.get('errors', 0)}\n")
            fh.write(f"healthy={'yes' if healthy else 'no'}\n")
            fh.write(f"date={result['date']}\n")
            fh.write(f"xlsx={result['xlsx']}\n")
            # 「今天只有 3 檔通過」和「今天有人把量比調到 3 倍」在摘要上不該
            # 長得一樣。改過就寫出改了什麼，沒改就寫 default。
            changed = result.get('rules_changed') or {}
            fh.write('rules=' + (
                ', '.join(f'{k} {a:g}→{b:g}' for k, (a, b) in changed.items())
                if changed else 'default') + '\n')

    if not healthy:
        print(f"::error::這一趟只跑完 {result['scanned']}/{universe} 檔"
              f"（{ok_ratio:.0%}），例外 {result.get('errors', 0)} 檔。"
              "報告仍然產出來了，但不應該拿它覆蓋昨天那份。")
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
