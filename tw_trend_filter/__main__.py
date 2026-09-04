"""命令列進入點：``python -m tw_trend_filter``。

把旗標翻譯成 :func:`tw_trend_filter.pipeline.run` 的參數，其餘什麼都不做。所有
預設值都對準「排程要的那一種跑法」，本機想完全重現單機版就加 ``--local``。
"""

from __future__ import annotations

import argparse
import os
import sys

from .pipeline import VERSION, run


def _env_excel_url() -> str:
    """在 GitHub Actions 底下自動組出這一次執行的頁面網址。

    Excel 報表存成 artifact，而 artifact 的直接下載網址是簽過章、幾分鐘就過期的，
    寫進報告等於寫進一個壞連結。執行頁面的網址則是永久的，artifact 就列在那一頁
    上（保留 30 天，過期後那一頁還在，只是下載不到）。

    不在 Actions 底下就回空字串，報告上不會出現那顆按鈕。
    """
    server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com')
    repo   = os.environ.get('GITHUB_REPOSITORY', '')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not (repo and run_id):
        return ''
    return f'{server}/{repo}/actions/runs/{run_id}'


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
    args = ap.parse_args(argv)

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
    )

    # 讓 workflow 的後續步驟拿得到「今天通過幾檔」，不必去 parse 上面那堆輸出。
    summary = os.environ.get('GITHUB_OUTPUT')
    if summary:
        with open(summary, 'a', encoding='utf-8') as fh:
            fh.write(f"count={result['count']}\n")
            fh.write(f"scanned={result['scanned']}\n")
            fh.write(f"date={result['date']}\n")
            fh.write(f"xlsx={result['xlsx']}\n")
    return 0


if __name__ == '__main__':
    sys.exit(main())
