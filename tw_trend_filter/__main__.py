"""命令列進入點：``python -m tw_trend_filter``。

把旗標翻譯成 :func:`tw_trend_filter.pipeline.run` 的參數，其餘什麼都不做。所有
預設值都對準「排程要的那一種跑法」，本機想完全重現單機版就加 ``--local``。
"""

from __future__ import annotations

import argparse
import os
import sys

from .pipeline import (
    DEFAULT_RULES,
    DEFAULT_WORKERS,
    RETRY_ROUNDS,
    VERSION,
    Rules,
    UniverseIncomplete,
    run,
)


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


def _run(args, rules):
    """把旗標翻譯成 `pipeline.run` 的參數。獨立出來只是為了讓上面那個
    try/except 框住的範圍剛好是「跑這一趟」，不是整個 main。"""
    return run(
        args.output_dir,
        limit=args.limit,
        workers=args.workers,
        retry_rounds=() if args.no_retry else RETRY_ROUNDS,
        period=args.period,
        make_excel=not args.no_excel,
        excel_charts=not args.no_excel_charts,
        link_base=args.link_base,
        plotly_cdn=not args.offline_plotly,
        chart_years=args.chart_years,
        excel_url=args.excel_url or _env_excel_url(),
        index_copy=args.index,
        data_dir=args.data_dir,
        data_base=args.data_base,
        cross_url=args.cross_url,
        open_when_done=args.local,
        rules=rules,
    )


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
    # 預設與理由都在 `pipeline.DEFAULT_WORKERS`——兩個地方各寫一個數字的話，
    # 改了一邊就會出現「說明寫 A、實際跑 B」。
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                    help=f'同時抓幾檔（預設 {DEFAULT_WORKERS}；調高會被 Yahoo '
                         f'靜靜地擋掉一部分，見 pipeline.DEFAULT_WORKERS）')
    # 補問。預設與理由都在 `pipeline.RETRY_ROUNDS`。
    #
    # 關掉它的場合只有一種：你在跑煙霧測試，而且不想為了幾檔注定拿不到的代號
    # 等兩分鐘。排程**不要**關——那兩分鐘買的正是「同一天跑兩次得到同一份名單」。
    ap.add_argument('--no-retry', action='store_true',
                    help='沒問到的那幾檔不要隔一段時間補問（見 pipeline.RETRY_ROUNDS）')
    ap.add_argument('--period', default='2y',
                    help='每一檔下載多長的歷史（yfinance 的 period，預設 2y）')
    ap.add_argument('--chart-years', type=float, default=2.0,
                    help='互動線圖保留最近幾年的 K 棒（預設 2）')
    # 沒過預設篩選的那幾檔，圖表資料一檔一個 JSON 寫到這裡；網頁用 --data-base
    # 組出網址，點下去才抓。
    #
    # 為什麼要兩個參數：檔案**寫在哪**和網頁**去哪裡抓**不是同一件事。排程上前者
    # 是 runner 的 `pages/trend-d`，後者是瀏覽器看到的相對路徑 `trend-d`。
    ap.add_argument('--data-dir', default='',
                    help='把每一檔的圖表資料寫到這個目錄（不給就不寫）')
    ap.add_argument('--data-base', default='',
                    help='網頁抓圖表資料的路徑或網址（例如 trend-d）')
    # 六大財務指標評等與估值（目標價／下檔價），從 tw-six-metrics 的 Pages 抓。
    # 報酬風險比的最後一步要今天的收盤，而那個數字這支程式手上就有——所以對面
    # 發的是和股價無關的那兩個價位，這邊自己算。見 `pipeline.reward_risk`。
    #
    # 抓不到只是一行訊息：四部曲一個位元組都不需要它。
    ap.add_argument('--cross-url', default='',
                    help='六大評分與估值的來源（預設是 tw-six-metrics 的 '
                         'cross.json；填 "-" 代表不抓）')
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

    try:
        result = _run(args, rules)
    except UniverseIncomplete as exc:
        # 上游今天不給資料，不是這支程式壞了。歸到 2 是為了讓「CI 紅了」還能
        # 代表「程式壞了」——理由寫在 pipeline.UniverseIncomplete 的 docstring。
        print(f'::warning::{exc}')
        print('這一趟沒有產出，也沒有覆蓋任何東西。結束碼 2。')
        return 2
    # 這一趟到底算不算數。
    #
    # 原本是無條件 `return 0`，於是 1,800 檔下載失敗跟 0 檔失敗都是綠燈，
    # 而下一步是 `git push -f origin report`——一條只有一個 commit 的孤兒分支。
    # 綠燈加 force push 等於「壞掉的那天會把好的那天蓋掉，而且沒有人會知道」。
    #
    # 門檻放在 90%：yfinance 偶爾漏個幾檔是常態，漏掉十分之一就不是了。
    #
    # ⚠️ 這道門檻寫好之後有一段時間是**完全沒有作用的**，而外表看不出來：
    # `scanned` 當時由呼叫端無條件遞增，所以它恆等於母體大小，`ok_ratio` 恆為
    # 100%，全市場下載失敗照樣 healthy=yes 然後 force push 蓋掉昨天。修法在
    # `pipeline.screen_stock`（那裡有完整說明）。這裡留一句是因為：**這個數字
    # 的意思比這個公式重要**。要動這一段之前，先確認 `scanned` 還是「真的掃到
    # 幾檔」，不是「跑完幾檔」。
    #
    # 分母扣掉「歷史不足 65 根」的那幾檔：它們不是沒掃到，是沒得掃（新上市，
    # 算不了 60MA）。把它們算進分母，掛牌潮那幾週就會在一個和資料品質無關的
    # 理由上誤觸門檻——而誤觸一次之後，就沒有人再相信這個門檻了。
    universe  = result.get('universe') or 0
    too_short = result.get('too_short', 0)
    errors    = result.get('errors', 0)
    scannable = max(universe - too_short, 0)
    ok_ratio  = (result['scanned'] / scannable) if scannable else 0.0
    # **分母也要有底線。**
    #
    # 上面那個比例是「該掃的裡面掃到幾成」，而 `too_short` 會把分母縮小。那在
    # 它真的是「新上市」的時候是對的，但只要有任何一種失敗被誤記成 too_short，
    # 這個比例就會自己變成 100%——失敗的那幾百檔把分母一起帶走了。
    #
    # 那不是假設：`screen_stock` 曾經把每一檔沒抓到的股票都記成 too_short
    # （`yf.download` 抓不到時回的是空的 DataFrame，不是 None），於是
    # 2026-09-20 那份報告只有 543/891 檔上櫃，而門檻算出來是 100%，照常發布。
    #
    # 所以再加一條**不看分母**的底線：掃到的檔數對**整個母體**至少要有八成。
    # 新上市永遠到不了兩成，所以這一條不會誤觸；而任何「失敗偽裝成別的東西」
    # 的花樣都跨不過它——它問的是「這份報告蓋到多少個市場」，不是「我們自己
    # 認為該掃的有多少」。
    # 只在**真的在掃全市場**的時候套用。`--limit 10` 的煙霧測試母體只有十檔，
    # 裡面兩三檔太新就跌破八成——而那一趟要證明的是「整條路走不走得通」，不是
    # 「今天蓋到多少市場」。門檻誤觸一次就沒有人再相信它，這一條也不例外。
    coverage  = (result['scanned'] / universe) if universe else 0.0
    full_run  = universe >= 100
    healthy   = (scannable > 0
                 and ok_ratio >= 0.90
                 and (coverage >= 0.80 or not full_run)
                 and errors <= scannable * 0.10)

    # 讓 workflow 的後續步驟拿得到「今天通過幾檔」，不必去 parse 上面那堆輸出。
    summary = os.environ.get('GITHUB_OUTPUT')
    if summary:
        with open(summary, 'a', encoding='utf-8') as fh:
            fh.write(f"count={result['count']}\n")
            fh.write(f"scanned={result['scanned']}\n")
            fh.write(f"universe={universe}\n")
            fh.write(f"errors={errors}\n")
            fh.write(f"too_short={too_short}\n")
            fh.write(f"coverage={coverage:.3f}\n")
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
        kinds = result.get('error_kinds') or {}
        # 「沒下載到」和「算到一半炸了」是兩種完全不同的故障，處置也不同：
        # 前者去看 Yahoo／網路，後者去看程式。摘要上分開寫，不要讓人自己猜。
        dl = kinds.get('DownloadFailed', 0)
        other = {k: v for k, v in kinds.items() if k != 'DownloadFailed'}
        detail = f"下載失敗 {dl} 檔" if dl else ""
        if other:
            detail += ("，" if detail else "") + "例外：" + "、".join(
                f'{k} {v} 檔' for k, v in sorted(other.items(), key=lambda x: -x[1]))
        print(f"::error::這一趟只真的掃到 {result['scanned']}/{scannable} 檔"
              f"（{ok_ratio:.0%}；對整個母體 {universe} 檔是 {coverage:.0%}）"
              + (f"，{detail}" if detail else "")
              + (f"（另有 {too_short} 檔歷史不足，已排除）" if too_short else "")
              + "。報告仍然產出來了，但不應該拿它覆蓋昨天那份。")
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
