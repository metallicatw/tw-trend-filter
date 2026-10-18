
from __future__ import annotations

import argparse
import os
import sys

from .pipeline import (
    DEFAULT_RULES,
    DEFAULT_WORKERS,
    RETRY_ROUNDS,
    SNAPSHOT_DAYS,
    VERSION,
    Rules,
    UniverseIncomplete,
    run,
)


def _run(args, rules):
    return run(
        args.output_dir,
        limit=args.limit,
        workers=args.workers,
        retry_rounds=() if args.no_retry else RETRY_ROUNDS,
        period=args.period,
        link_base=args.link_base,
        plotly_cdn=not args.offline_plotly,
        chart_years=args.chart_years,
        index_copy=args.index,
        data_dir=args.data_dir,
        data_base=args.data_base,
        cross_url=args.cross_url,
        official_url=args.official_url,
        open_when_done=args.local,
        rules=rules,
    )


EXIT_USAGE = 3


class _Parser(argparse.ArgumentParser):

    def exit(self, status=0, message=None):          # noqa: D102
        if message:
            self._print_message(message, sys.stderr)
        raise SystemExit(EXIT_USAGE if status == 2 else status)


def main(argv: list[str] | None = None) -> int:
    ap = _Parser(
        prog='tw-trend-filter',
        description=f'台股順勢交易篩選系統 {VERSION}：全市場掃描，產出互動線圖網頁。',
    )
    ap.add_argument('--version', action='version', version=VERSION)
    ap.add_argument('-o', '--output-dir', default='output',
                    help='產出資料夾（預設 output/）')
    ap.add_argument('--index', default='',
                    help='另外複製一份互動線圖到這個路徑（排程用 index.html）')
    ap.add_argument('--limit', type=int, default=0,
                    help='只掃前 N 檔。給煙霧測試用，0 = 全市場')
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                    help=f'同時抓幾檔（預設 {DEFAULT_WORKERS}；調高會被 Yahoo '
                         f'靜靜地擋掉一部分，見 pipeline.DEFAULT_WORKERS）')
    ap.add_argument('--no-retry', action='store_true',
                    help='沒問到的那幾檔不要隔一段時間補問（見 pipeline.RETRY_ROUNDS）')
    ap.add_argument('--period', default='2y',
                    help='每一檔下載多長的歷史（yfinance 的 period，預設 2y）')
    ap.add_argument('--chart-years', type=float, default=2.0,
                    help='互動線圖保留最近幾年的 K 棒（預設 2）')
    ap.add_argument('--data-dir', default='',
                    help='把每一檔的圖表資料寫到這個目錄（不給就不寫）')
    ap.add_argument('--data-base', default='',
                    help='網頁抓圖表資料的路徑或網址（例如 trend-d）')
    ap.add_argument('--cross-url', default='',
                    help='六大評分與估值的來源（預設是 tw-six-metrics 的 '
                         'cross.json；填 "-" 代表不抓）')
    ap.add_argument('--official-url', default='',
                    help='官方逐日行情的網址樣板（{date} 換成 YYYY-MM-DD；預設是 tw-six-metrics 的 '
                         'data/market/daily/prices；填 "-" 代表不補）')
    ap.add_argument('--link-base', default='',
                    help='個股頁連結的前綴，例如 '
                         'https://metallicatw.github.io/tw-six-metrics/stock')
    ap.add_argument('--excel-url', default='', help=argparse.SUPPRESS)
    ap.add_argument('--no-excel', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--no-excel-charts', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--offline-plotly', action='store_true',
                    help='把 plotly.js 內嵌進 HTML（檔案大 10 倍，但可離線開啟）')
    ap.add_argument('--local', action='store_true',
                    help='重現本機單機版：5 年歷史、內嵌 plotly、跑完自動開檔')

    g = ap.add_argument_group(
        '四部曲門檻',
        '不給就照預設跑。改過的項目會印在 log 上，也會寫進報告網頁上的篩選說明。',
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

    if args.lookback > SNAPSHOT_DAYS:
        ap.error(f'--lookback 最多 {SNAPSHOT_DAYS}（快照只存這麼多天，'
                 f'超過的部分引擎看不到，而報告會照你填的數字印）')

    rules = Rules(
        min_price=args.min_price,
        min_vol20=args.min_vol20,
        min_amount=args.min_amount_m * 1e6,
        lookback=args.lookback,
        squeeze=args.squeeze,
        vol_ratio=args.vol_ratio,
        atr_stop=args.atr_stop,
    )

    if args.local:
        args.period = '5y'
        args.chart_years = 5.0
        args.offline_plotly = True

    try:
        result = _run(args, rules)
    except UniverseIncomplete as exc:
        print(f'::warning::{exc}')
        print('這一趟沒有產出，也沒有覆蓋任何東西。結束碼 2。')
        return 2
    universe  = result.get('universe') or 0
    too_short = result.get('too_short', 0)
    errors    = result.get('errors', 0)
    scannable = max(universe - too_short, 0)
    ok_ratio  = (result['scanned'] / scannable) if scannable else 0.0
    coverage  = (result['scanned'] / universe) if universe else 0.0
    full_run  = universe >= 100
    booked    = result['scanned'] + errors + too_short
    balanced  = (booked == universe)
    healthy   = (scannable > 0
                 and balanced
                 and ok_ratio >= 0.90
                 and (coverage >= 0.80 or not full_run)
                 and errors <= scannable * 0.10)
    limited     = bool(getattr(args, 'limit', 0))
    default_rules = not (result.get('rules_changed') or {})
    publishable = healthy and full_run and not limited and default_rules

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
            fh.write(f"balanced={'yes' if balanced else 'no'}\n")
            fh.write(f"publishable={'yes' if publishable else 'no'}\n")
            fh.write(f"limited={'yes' if limited else 'no'}\n")
            fh.write(f"date={result['date']}\n")
            fh.write(f"asof={result.get('asof') or result['date']}\n")
            fh.write(f"snapshot={result.get('snapshot_file') or ''}\n")
            changed = result.get('rules_changed') or {}
            fh.write('rules=' + (
                ', '.join(f'{k} {a:g}→{b:g}' for k, (a, b) in changed.items())
                if changed else 'default') + '\n')

    if not healthy:
        kinds = result.get('error_kinds') or {}
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
