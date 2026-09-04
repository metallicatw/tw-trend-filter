# tw-trend-filter — 台股順勢交易篩選系統 V3.1

每個交易日收盤後掃過全台股上市＋上櫃約 1,900 檔，用同一組規則篩出「趨勢已經
成立、而且今天有量能配合」的標的，產出兩份東西：

* **互動技術線圖**（`index.html`）—— 每一檔一張可縮放、可平移的 K 線圖，含
  20/60 均線、布林通道、成交量與建議停損。這一份會被
  [tw-six-metrics](https://github.com/metallicatw/tw-six-metrics) 的建站流程取走，
  變成那個網站上的〔趨勢選股〕分頁。
* **Excel 報表**（三個分頁：篩選總覽、指標明細、個股線圖）—— 存在每一次
  Actions 執行的 Artifacts 區，保留 30 天。

原始版本是一支在 Windows 上雙擊執行的單檔程式；這個 repo 是它的移植版，篩選
邏輯與報告外觀完全相同，改掉的只有「假設自己跑在誰的桌面上」的那些部分。

---

## 篩選規則：嚴格四部曲

四關依序，全部通過才會進名單。順序是有意義的——先排除掉買不進也賣不掉的，
才輪得到談趨勢。

**第 0 關．流動性**（不算在四部曲裡，但先擋）

| 條件 | 門檻 |
| --- | --- |
| 股價 | > 10 元 |
| 20 日均量 | > 1,000 股 |
| 20 日均額 | > 5,000 萬元 |

**第 1 關．趨勢方向**　收盤價 > 60MA，且 20MA > 60MA。
短均在長均之上、價格又在兩者之上——這是「上升趨勢」最不需要解釋的定義。

**第 2 關．動能啟動**　最近 10 個交易日內，發生下列任一：

* **黃金交叉**：20MA 由下向上穿越 60MA
* **布林壓縮**：布林頻寬 `(上軌−下軌)/中軌` ≤ 12%

前者是趨勢剛轉向，後者是波動收斂到極致——兩種都是「接下來要有事發生」。

**第 3 關．突破確認**　收盤價突破布林上軌，或突破前 20 日最高價（Donchian）。
第 2 關說「要有事發生」，這一關要求那件事**已經發生**。

**第 4 關．量能背書**　當日成交量 ÷ 20 日均量 ≥ 1.2。
沒有量的突破是假突破，這一關把它們刷掉。

**建議停損**：`收盤價 − 3 × ATR(14)`。用波動度而不是固定百分比——一檔每天
震 5% 的股票和一檔每天震 1% 的，同樣的 3% 停損意義完全不同。

> ⚠️ 這是一套公開資料的量化篩選，輸出的是「符合這組技術條件的清單」，
> 不是投資建議。任何一檔都可能在通過篩選的隔天跌破停損。

---

## 排程

| Workflow | 何時 | 做什麼 |
| --- | --- | --- |
| `daily.yml` | 週一～五 台北 15:10 | 全市場掃描 → commit `index.html` → Excel 上傳 artifact |
| `ci.yml` | 每次 push / PR | 單元測試 + 40 檔煙霧測試 |

台股 13:30 收盤，Yahoo 的日線大約 14:30 之後才穩定，15:10 留了足夠的緩衝。
週末不跑：沒有新的收盤價，跑出來的是禮拜五那一份的複製品。

`daily.yml` 可以手動觸發（Actions → 每日篩選 → Run workflow），並且可以填
`limit` 只掃前 N 檔，用來快速確認整條路還通。

---

## 本機執行

```bash
pip install -r requirements.txt

# 排程用的那種跑法（2 年歷史、plotly 走 CDN、跑完不開檔）
python -m tw_trend_filter --output-dir output

# 重現本機單機版：5 年歷史、plotly 內嵌可離線開、跑完自動開檔
python -m tw_trend_filter --local

# 只掃前 50 檔，快速確認能不能跑
python -m tw_trend_filter --limit 50
```

常用旗標：

| 旗標 | 預設 | 說明 |
| --- | --- | --- |
| `--limit N` | 0（全市場） | 只掃前 N 檔 |
| `--workers N` | 8 | 同時抓幾檔 |
| `--period` | `2y` | 每檔下載多長歷史 |
| `--chart-years` | 2 | 線圖保留最近幾年 K 棒 |
| `--link-base URL` | 無 | 個股頁連結前綴 |
| `--offline-plotly` | 關 | 內嵌 plotly.js（檔案大 10 倍，可離線開） |
| `--no-excel-charts` | 關 | Excel 照做但跳過 K 線圖，快很多 |

### 為什麼預設是 2 年而不是 5 年

所有指標裡窗口最長的是 60MA 加上 Donchian 的 20 日位移，八十幾個交易日。
兩年和五年算出來的**最後一列一模一樣**，但要下載的資料少六成——乘上一千九百
檔，那是排程跑不跑得完的差別。要完整重現單機版的下載行為就加 `--period 5y`。

同理，線圖預設只保留 2 年 K 棒。指標是在**完整**歷史上算完之後才裁的，所以線
的形狀和數值都沒變，變的只有你能往左捲多遠；換來的是報告從 12 MB 降到 1 MB
出頭，手機打得開。

---

## 和 tw-six-metrics 的關係

兩個 repo 各自獨立跑，靠一個檔名接起來：

```
tw-trend-filter/index.html          ← 這裡每天產生
        │
        │  tw-six-metrics 建站時 git clone --depth 1 取走
        ▼
tw-six-metrics/site/trend-report.html
        │
        │  用 iframe 嵌進〔趨勢選股〕那一頁（頁首、導覽、搜尋框都在）
        ▼
https://metallicatw.github.io/tw-six-metrics/trend.html
```

反過來，這份報告裡每一檔都有一個連結指回
`https://metallicatw.github.io/tw-six-metrics/stock/<代號>.html`——技術面挑出來的
標的，下一個問題一定是「這家公司體質怎麼樣」，而那個答案在隔壁。

取不到的時候（這個 repo 掛了、clone 失敗）tw-six-metrics 那邊少一個導覽項，
不是多一個 404。

### 關於 repo 大小

`index.html` 每個交易日換一次，一份約 2～3 MB。壓縮後大約每年 150～200 MB
進 git 歷史——GitHub 建議單一 repo 在 1 GB 以內，所以這樣跑個四、五年才需要
處理，而下游是 `git clone --depth 1`，歷史多長都不影響它。

真的長太大的時候，最省事的做法是把 `index.html` 改推到一個 orphan 分支上、
每天 force-push 覆蓋（歷史永遠只有一個 commit），下游改成
`git clone --depth 1 --branch report`。在那之前不值得為它增加一層機制。

---

## 專案結構

```
tw_trend_filter/
  pipeline.py     篩選引擎、Excel 版面、互動線圖（從單機版移植）
  __main__.py     命令列進入點
tests/
  test_indicators.py   ATR、布林帶、產業對照
  test_report.py       報告組裝、個股頁連結、CDN/內嵌、K 線裁切
.github/workflows/
  daily.yml       每日排程
  ci.yml          測試 + 煙霧測試
```

## 資料來源

* **股票池與產業別**：TWSE ISIN（`isin.twse.com.tw`，上市 + 上櫃），
  取不到時退回 TWSE OpenAPI；兩者都沒給的用內建靜態對照表補。
* **價量**：Yahoo Finance（透過 `yfinance`）。Yahoo 會依 TLS 指紋擋掉非瀏覽器
  的連線並回 401，所以走 `curl_cffi` 模擬 Chrome。

## 授權

MIT
