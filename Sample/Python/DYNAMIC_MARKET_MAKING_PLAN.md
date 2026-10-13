# 動態委託簿做市與蒙地卡羅研究計畫

> **研究方案而非操作手冊。** 部分賽事目標及策略變形仍屬研究假設；已實作的 `replay_market_maker.py`、`monte_carlo_market_maker.py`、`walk_forward_oos.py` 可供離線研究，但不構成經真實 TTB API／競賽帳戶驗收。唯一操作手冊見 [OPERATIONS.md](OPERATIONS.md)。此頁早期 live 示例可能欠缺目前必需的 margin 風控欄位，**不得直接複製執行**。

## 研究結論先行

期交所新一代虛擬交易所的見價見量模式，會以真實市場成交 tick 的價格與數量判斷成交；每位模擬交易人的委託則獨立與市場比對，交易人之間不互相搶排隊順位。這讓被動限價策略可能取得真實市場沒有的成交優勢，但並不代表任何掛價都會成交，也不代表同一帳戶可無限重複使用同一筆成交量。

因此研究分成兩張成績單：

1. **虛擬競賽成績**：以 TTB 實際委託回報為準，追求費後累計損益與可控回撤。
2. **可移植性成績**：用機率成交與 queue-aware 模型壓力測試，辨識績效中有多少只是模擬器機制紅利。

「極高累計報酬」只能是待驗證目標，不能先寫成回測結論。若樂觀模型賺錢、保守模型不成交或虧損，應標示為 **TTB-specific edge**，不得解讀成實盤 alpha。

## 2026 虛擬競賽專用目標

若目標完全限定在 2026 大專模擬競賽，應使用 `virtual-max` 模式與 `independent` 成交模型，不再以 queue-aware 結果作為上線門檻。不過官方排名是先取夏普比率前 30 名，再按總獲利取前 15 名，因此實務目標是：

1. 完成至少六類商品及 28 個成交日；
2. 保持競賽期總損益為正並進入夏普前 30；
3. 在前兩項成立後最大化費後總獲利。

起始資金為 200 萬元，僅能交易日盤；臺股期貨手續費每口 100 元，多數其他指定商品每口 50 元。帳戶風險指標低於 25% 會強制沖銷，因此無上限馬丁並不符合「最大化可排名報酬」的目標。

### `virtual-max` 策略差異

- 入口可直接掛最佳買價／賣價，`entry_offset_ticks=0`，優先增加獨立見價見量的成交次數。
- 固定 `target_ticks` 後退出，並繼續使用 sticky quote 避免每 tick 撤單。
- `fixed`：固定口數。
- `martingale`：虧損循環後按倍數增加，受 `max_sizing_step`、`max_order_qty` 與庫存上限約束。
- `anti_martingale`：獲利循環後增加，虧損即回到基礎口數。
- 所有比較一律使用已扣手續費、交易稅及未平倉預估退出成本的期末權益。
- 競賽模式於 08:45 開始進場、13:35 停止新入口、13:42 起強制減倉，避免誤跑夜盤並保留收盤前退出時間。

目前執行器每方向只維持一張委託，可用委託數量放大，不會用大量無成交意圖委託或異常撤掛；競賽規則明訂異常交易可能被取消資格。

### 費後總獲利參數搜尋

```powershell
cd Sample/Python
py -3.13 optimize_virtual_competition.py `
  "output/runs/quotes-*.csv" `
  --symbol TXFJ6 --objective profit --initial-capital 2000000 `
  --target-ticks 3,4,6,8 --stop-ticks 4,6,8,12 `
  --entry-offset-ticks 0,1 --quote-ttl-seconds 1,2,5 `
  --sizing-modes fixed,martingale,anti_martingale `
  --sizing-multipliers 1.5,2 --max-quantities 1,2,4,8 `
  --output output/research/virtual-max-search.json
```

`--objective profit` 直接按費後總損益排序；`competition` 會先考慮已知資格與日夏普，再比較總獲利；`sharpe` 可單獨尋找低波動版本。每個行情 CSV 視為一個交易日。

**正式送單命令不在研究計畫提供。** 要評估假設，請使用下方離線回放及 Walk-forward 管線；真實虛擬委託仍需要 #2/#4 驗證及 [OPERATIONS.md](OPERATIONS.md) 規定的人工核對。

官方參考：

- [新一代虛擬交易所教學手冊](https://sim2.bq888.taifex.com.tw/Portal/TutorialGuide.pdf)
- [真實市場撮合原則](https://www.taifex.com.tw/cht/4/oamPrinciple)

## 已實作策略：sticky dynamic quote band

### 1. 公平價與方向分數

- `microprice`：用最佳買賣量加權的中間價。
- `order-flow imbalance`：比較相鄰快照的最佳價量變化。
- `trade flow`：以近期成交方向與成交量估計短期主動買賣壓力。
- 中心價：`microprice + score × tick_size`。

這些分數目前是啟發式訊號，不是校準後的上漲機率。

### 2. 動態寬度

半寬取下列最大值：

- 設定的最小半寬；
- 足以覆蓋手續費、交易稅與滑價的成本底線；
- 最近 30 秒中間價波動度。

強正向訊號只保留買進入口；強負向訊號只保留賣出入口；中性時保留雙邊入口。最大庫存預設一口，首筆成交後只允許減倉。

### 3. 黏著報價

遠離市場的委託若每個 tick 都重新報價，會永遠跟著行情移動而很難成交。目前報價會固定到以下任一條件成立才更新：

- 中心價移動至少 `reprice_threshold_ticks`；
- 委託存在超過 `quote_ttl_seconds`；
- 訊號方向改變，需要立即撤掉有毒的一側；
- 進入持倉、風險退出或停機狀態。

預設值為 4 ticks 與 5 秒，正式值應由 TTB 委託回報校準。

### 4. 庫存與退出

- 成交後以固定 `target_ticks` 掛減倉單。
- 達停損、持倉逾時、逆向流量或日損失／最大回撤門檻時，以可平倉方向報價退出。
- 不使用馬丁格爾，不因虧損放大口數。
- 回放結束仍有部位時只按買賣價估值，不假裝已平倉。

## 三種成交模型

| 模型 | 用途 | 成交假設 |
|---|---|---|
| `independent` | TTB 樂觀上限 | 委託之後有新的真實成交 tick 觸及限價，並有新增成交量即成交；同 tick 量在本帳戶訂單間共享 |
| `probabilistic` | 成交敏感度 | 穿價必成；同價觸及按 `touch_fill_probability` 抽樣；可加入延遲 |
| `queue` | 可移植性下限 | 新單前方先放入 `queue_fraction × 最佳檔顯示量`，同價成交量先消耗前方數量 |

`queue` 只有 Level-1 快照，因此仍不是完整真實訂單簿重建。三模型的目的不是找出唯一正確答案，而是產生可解釋的績效範圍。

## 回測與蒙地卡羅

### 單一路徑回放

```powershell
cd Sample/Python
py -3.13 replay_market_maker.py output/runs/quotes-YYYYMMDD-HHMMSS.csv `
  --symbol TXFJ6 --fill-model independent

py -3.13 replay_market_maker.py output/runs/quotes-YYYYMMDD-HHMMSS.csv `
  --symbol TXFJ6 --fill-model probabilistic `
  --touch-fill-probability 0.35 --latency-ms 50

py -3.13 replay_market_maker.py output/runs/quotes-YYYYMMDD-HHMMSS.csv `
  --symbol TXFJ6 --fill-model queue --queue-fraction 1.0 --latency-ms 50
```

### 區塊拔靴蒙地卡羅

```powershell
py -3.13 monte_carlo_market_maker.py `
  output/runs/quotes-YYYYMMDD-HHMMSS.csv `
  --symbol TXFJ6 --trials 1000 --block-size 500 --fill-model all `
  --touch-fill-probability 0.35 --queue-fraction 1.0 --latency-ms 50 `
  --initial-capital 1000000 `
  --output output/research/txfj6-monte-carlo.json
```

區塊拔靴會連續抽取行情區段，再重建單調時間、累計量與銜接後價格；比逐筆隨機洗牌更能保留短期波動、價差、量與 order flow 的群聚。`block-size` 應以時間換算，優先測試約 30、60、120 秒三種尺度。

沒有真實行情時可以用下列命令檢查程式是否能跑，但結果不得放入策略結論：

```powershell
py -3.13 monte_carlo_market_maker.py --symbol TXFJ6 `
  --synthetic-quotes 5000 --trials 100 --fill-model all
```

## 正式研究流程

### 階段 A：成交模型校準

至少收集 5–10 個完整交易日，且同時保留：

- 原始 quote CSV；
- 每次送單、改價、撤單與收到回報的本機時間；
- TTB 成交價格、成交量、委託號與累計成交量；
- 斷線、重播、開收盤與異常事件。

用資料估計：同價觸及成交率、送單到生效延遲、取消後成交率、部分成交分布，以及成交後 1／2／5／10 秒價格漂移。若成交後漂移長期為負，做市價差只是補償 adverse selection。

### 階段 B：按日 walk-forward

- 不可把相鄰 tick 隨機切成訓練與測試。
- 以日期切分，例如 60% 設參數、20% 驗證、20% 完全留存測試。
- 每次只用過去資料選參數，再推進到下一個未見交易日。
- 同時報告日盤、夜盤、趨勢日、盤整日與高波動日。

### 階段 C：參數敏感度

優先掃描少量、可解釋參數：

- `target_ticks`: 4、6、8、10；
- `half_width_ticks`: 2、3、4、6；
- `quote_ttl_seconds`: 1、2、5、10；
- `reprice_threshold_ticks`: 2、4、6；
- `touch_fill_probability`: 0.15、0.35、0.60；
- `queue_fraction`: 0.5、1.0、2.0；
- `latency_ms`: 0、50、150、500。

不要從數百組合挑最高單點。優先選擇鄰近參數也維持正報酬、回撤相近的平臺區域。

### 階段 D：驗收門檻

虛擬競賽版至少要求：

- TTB 實際成交與 `independent` 預測的成交率差異可解釋；
- 留存測試至少 100 個完整循環，且費後總損益為正；
- 單日損失與最大回撤不超過事先設定額度；
- 不是由單一交易日、單一時段或少數極端交易貢獻大部分利潤；
- 蒙地卡羅中 `probabilistic` 模型的虧損機率與 p05 回撤仍在可承受範圍。

若要宣稱具實盤可移植性，還必須要求 queue-aware 樣本外中位數為正，並加入真實手續費、交易稅、滑價、排隊、改撤單延遲與斷線風險。

## 其他候選策略

### 1. 被動循環／固定 bracket（最適合目前 TTB 機制）

在公平價兩側放置具黏著性的入口，成交後只做固定目標退出。它最直接利用獨立見價見量，不需要預測很遠的方向，也是目前已實作版本。缺點是趨勢行情容易接到下跌中的買單或上漲中的賣單。

### 2. order-flow gated maker（建議下一個實驗）

仍以被動委託進場，但只在 microprice、OFI 與主動成交流一致時保留一側，並用成交後漂移自動調整門檻。它可能降低 adverse selection，但成交數會下降，需要更多交易日才能驗證。

### 3. 波動狀態切換

以短／長波動比與價差判斷盤整或趨勢：盤整才開雙邊，趨勢只做順勢一側，高波動暫停。這比單純加大停損更適合控制尾部回撤。

### 4. TX／MTX／TMF 相對價值

用同標的不同契約乘數或近遠月價差做相對價值，比單腿方向風險低；但需要同步多商品行情、雙腿成交與未成交腿處理。見價見量不保證兩腿同時成交，在完成單商品校準前不建議先做。

不建議用大量無意成交委託、重複撤掛或假量誘導等方式。即使是模擬競賽，也應遵守活動規則，並避免把不可移植的機制行為包裝成市場獲利能力。
