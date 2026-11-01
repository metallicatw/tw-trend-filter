import json

import numpy as np
import pandas as pd

from tw_trend_filter import single
from tw_trend_filter.pipeline import SNAPSHOT_COLUMNS, _live_block, _script_json, DEFAULT_RULES


def _df(n=300, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0.2, 1.0, n))
    idx = pd.bdate_range('2025-01-01', periods=n)
    return pd.DataFrame({'Open': close, 'High': close + 1, 'Low': close - 1, 'Close': close,
                         'Volume': rng.integers(2_000_000, 4_000_000, n).astype(float)}, index=idx)


def _report(rows, series):
    snaps = [dict(zip(SNAPSHOT_COLUMNS, r)) for r in rows]
    return ('<html><body>' + _live_block(DEFAULT_RULES, snaps, data_ver='old')
            + _script_json('tf-series', series) + '</body></html>')


def _rows(html, rx=single.SNAP_RE):
    return json.loads(rx.search(html).group(2).replace('<\\/', '</'))


def test_換掉報告裡那一列並沿用名稱與六大(tmp_path):
    old, _ = single.compute('2330', _df(seed=2), None)
    old[SNAPSHOT_COLUMNS.index('name')] = '台積電'
    old[SNAPSHOT_COLUMNS.index('six')] = 5.5
    other, _ = single.compute('1101', _df(seed=3), None)
    rep = tmp_path / 'r.html'
    rep.write_text(_report([old, other], {}), encoding='utf-8')
    data = tmp_path / 'd'
    r = single.run('2330', [rep], data, get=lambda c: (c + '.TW', _df(seed=9)))
    html = rep.read_text(encoding='utf-8')
    rows = _rows(html)
    assert len(rows) == 2 and rows[1] == other
    new = dict(zip(SNAPSHOT_COLUMNS, rows[0]))
    assert new['name'] == '台積電' and new['six'] == 5.5
    assert new['close'] != dict(zip(SNAPSHOT_COLUMNS, old))['close']
    assert (data / '2330.json').exists()
    assert 'const TF_VER = "old"' not in html
    assert r['new'] is False


def test_有嵌入數列的換掉嵌入那一份_沒有的那一檔加一列(tmp_path):
    old, ser = single.compute('2330', _df(seed=2), None)
    rep = tmp_path / 'r.html'
    rep.write_text(_report([old], {'2330': ser}), encoding='utf-8')
    single.run('2330', [rep], tmp_path / 'd', get=lambda c: (c, _df(seed=5)))
    html = rep.read_text(encoding='utf-8')
    emb = json.loads(single.SERIES_RE.search(html).group(2).replace('<\\/', '</'))
    assert emb['2330'] != ser and not (tmp_path / 'd' / '2330.json').exists()
    r = single.run('6666', [rep], None, get=lambda c: (c, _df(seed=6)))
    assert r['new'] is True and len(_rows(rep.read_text(encoding='utf-8'))) == 2
