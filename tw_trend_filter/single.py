"""單檔更新：只重算一檔，改寫已經產生好的報告裡那一列（一鍵更新用）。

全市場掃描一次要十幾分鐘；個股頁按〔一鍵更新〕的時候只想知道「這一檔今天
過不過」。這裡只抓這一檔的日線、用跟全市場掃描同一套 `indicator_snapshot()`
重算快照，然後：

1. 把報告 `tf-snap` 裡這一檔那一列換掉（沒有就加一列）——網頁上的篩選是在
   瀏覽器裡拿這張表重算的，所以換掉之後〔篩選〕的結果就是新的；
2. 圖表數列：原本嵌在 `tf-series` 裡的就換掉嵌的那一份，否則寫成
   `<data-dir>/<代號>.json`；
3. 把 `TF_VER` 換成新的時間戳，瀏覽器才不會拿快取裡的舊圖。

六大分數與報酬風險比沿用報告裡原本那一列（它們來自另一個專案，一天換一次）。

用法：
    python -m tw_trend_filter.single 2330 --report site/trend-report.html \
        [--report output/index.html] [--data-dir site/trend-d]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

SNAP_RE = re.compile(r'(<script type="application/json" id="tf-snap">)(.*?)(</script>)', re.S)
SERIES_RE = re.compile(r'(<script type="application/json" id="tf-series">)(.*?)(</script>)', re.S)
VER_RE = re.compile(r'(const TF_VER = )("[^"\n]*")(;)')


def _sf(v, d=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, d)


def download(code: str, period: str = '2y'):
    """回傳 (ticker, DataFrame)。先試上市（.TW）再試上櫃（.TWO）。"""
    import pandas as pd
    import yfinance as yf

    from .pipeline import _YF_SESSION

    last = ''
    for ticker in (f'{code}.TW', f'{code}.TWO'):
        for kw in ({'session': _YF_SESSION}, {}):
            try:
                df = yf.download(ticker, period=period, interval='1d', auto_adjust=True,
                                 progress=False, threads=False, **kw)
            except Exception as e:  # noqa: BLE001
                last = f'{type(e).__name__}: {e}'
                continue
            if df is not None and len(df):
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
                df = df.dropna(subset=['Close', 'Volume'])
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return ticker, df
    raise RuntimeError(f'Yahoo 沒有 {code} 的日線{("：" + last) if last else ""}')


def compute(code: str, df, old_row: list | None, rules=None) -> tuple[list, dict]:
    """算出 tf-snap 的一列與圖表數列。old_row 用來沿用名稱、產業、六大與報酬風險比。"""
    from .pipeline import (DEFAULT_RULES, SNAPSHOT_COLUMNS, _chart_series, compute_bollinger,
                           indicator_snapshot, snapshot_row)

    rules = rules or DEFAULT_RULES
    if len(df) < 65:
        raise RuntimeError(f'{code} 只有 {len(df)} 天資料，不夠算（至少 65 天）')
    numbers, ind = indicator_snapshot(df)
    old = dict(zip(SNAPSHOT_COLUMNS, old_row)) if old_row else {}
    snap = {'code': code, 'name': old.get('name') or code, 'industry': old.get('industry') or '',
            **numbers, 'six': old.get('six'), 'rr': old.get('rr'),
            'rr_free': old.get('rr_free', False)}
    for k in SNAPSHOT_COLUMNS:
        snap.setdefault(k, old.get(k))
    close = df['Close']
    boll_ma, boll_up, boll_dn, _ = compute_bollinger(close)
    res = {'code': code, 'name': snap['name'], '_df': df, '_ma20': ind['ma20'], '_ma60': ind['ma60'],
           '_boll_up': boll_up, '_boll_mid': boll_ma, '_boll_dn': boll_dn,
           'stop_loss': round(ind['price'] - rules.atr_stop * ind['atr_val'], 2),
           'ma20_last': round(float(ind['ma20'].iloc[-1]), 2),
           'ma60_last': round(float(ind['ma60'].iloc[-1]), 2)}
    keep_rows = int(2.0 * 252) + 130
    return snapshot_row(snap), _chart_series(res, keep_rows, _sf)


def patch_report(html: str, code: str, row: list, series: dict, ver: str) -> tuple[str, str]:
    """回傳 (新的 HTML, 數列放在哪：embedded / file / none)。"""
    from .pipeline import SNAPSHOT_COLUMNS

    m = SNAP_RE.search(html)
    if not m:
        raise RuntimeError('報告裡找不到 tf-snap（這份報告太舊或不是趨勢選股的報告）')
    rows = json.loads(m.group(2).replace('<\\/', '</'))
    ci = SNAPSHOT_COLUMNS.index('code')
    for i, r in enumerate(rows):
        if r[ci] == code:
            rows[i] = row
            break
    else:
        rows.append(row)
    body = json.dumps(rows, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    html = html[:m.start(2)] + body + html[m.end(2):]
    where = 'file'
    s = SERIES_RE.search(html)
    if s:
        try:
            ser = json.loads(s.group(2).replace('<\\/', '</'))
        except ValueError:
            ser = None
        if isinstance(ser, dict) and code in ser:
            ser[code] = series
            sb = json.dumps(ser, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
            html = html[:s.start(2)] + sb + html[s.end(2):]
            where = 'embedded'
    html = VER_RE.sub(lambda v: v.group(1) + json.dumps(ver) + v.group(3), html, count=1)
    return html, where


def old_row_of(html: str, code: str) -> list | None:
    from .pipeline import SNAPSHOT_COLUMNS

    m = SNAP_RE.search(html)
    if not m:
        return None
    ci = SNAPSHOT_COLUMNS.index('code')
    for r in json.loads(m.group(2).replace('<\\/', '</')):
        if r[ci] == code:
            return r
    return None


def run(code: str, reports: list[Path], data_dir: Path | None, *, get=download) -> dict:
    reports = [p for p in reports if p.is_file()]
    if not reports:
        raise RuntimeError('找不到趨勢選股的報告（先跑一次全市場掃描）')
    first = reports[0].read_text(encoding='utf-8')
    old = old_row_of(first, code)
    _, df = get(code)
    row, series = compute(code, df, old)
    ver = time.strftime('%Y%m%d%H%M%S')
    where = 'none'
    for path in reports:
        html, where = patch_report(path.read_text(encoding='utf-8'), code, row, series, ver)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(html, encoding='utf-8')
        tmp.replace(path)
    if where == 'file' and data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / f'{code}.json').write_text(
            json.dumps(series, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    from .pipeline import DEFAULT_RULES, SNAPSHOT_COLUMNS, passes

    snap = dict(zip(SNAPSHOT_COLUMNS, row))
    ok, why = passes(snap, DEFAULT_RULES)
    return {'code': code, 'asof': series['d'][-1] if series.get('d') else '', 'close': snap['close'],
            'pass': ok, 'trigger': '、'.join(why), 'new': old is None, 'reports': len(reports)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='python -m tw_trend_filter.single',
                                 description='只重算一檔，改寫已經產生好的趨勢選股報告')
    ap.add_argument('code')
    ap.add_argument('--report', action='append', default=[], help='要改寫的報告（可給多次）')
    ap.add_argument('--data-dir', default='', help='沒過篩那幾檔的圖表資料目錄（<代號>.json）')
    a = ap.parse_args(argv)
    if not re.fullmatch(r'\d{4,6}[A-Z]?', a.code):
        print(f'「{a.code}」不是股票代號', file=sys.stderr)
        return 2
    try:
        r = run(a.code, [Path(p) for p in a.report], Path(a.data_dir) if a.data_dir else None)
    except Exception as e:  # noqa: BLE001
        print(f'趨勢選股單檔更新失敗：{e}', file=sys.stderr)
        return 1
    state = f'今天過篩（{r["trigger"]}）' if r['pass'] else '今天沒過預設篩選'
    print(f'趨勢選股：{r["code"]} 資料到 {r["asof"]}，收盤 {r["close"]}，{state}'
          + ('（報告裡原本沒有這一檔，已加入）' if r['new'] else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
