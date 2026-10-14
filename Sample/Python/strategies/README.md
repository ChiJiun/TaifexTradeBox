# 見價見量虛擬競賽策略索引

> **截至目前，策略 01、03、04 已有可執行、通過離線測試的模組；策略 02 有限攤平與 05 最終組合仍屬研究規格，尚未驗收。** 完成離線實作不代表完成 TTB 真實成交與保證金驗收。既有 CLI 仍支援內建 `market-maker`、`moving-average`，以及 `--strategy-module`。參考 [OPERATIONS.md](../OPERATIONS.md)。

本目錄的 Markdown 文件專門研究期交所新一代虛擬交易所及「縱橫期海」類競賽。
核心假設是：委託送出後，模擬撮合器會依後續真實市場逐筆成交價量及委買賣價量判斷成交，且不同模擬交易人的委託彼此獨立，不需要爭奪真實市場排隊順位。

這是模擬競賽特有的撮合優勢，不應解讀為真實市場可取得的報酬。所有參數仍須用 TTB 實際委託與成交回報校準。

## 文件列表

| 文件 | 核心功能 | 適合階段 |
|---|---|---|
| [01_STICKY_ONE_TICK_MARKET_MAKING.md](01_STICKY_ONE_TICK_MARKET_MAKING.md) | 黏著委託、持倉後只退出及一跳費後成本檢查 | **已實作** `strategies.sticky_maker:StickyOneTickMaker`（離線驗證） |
| [02_COST_ADJUSTED_BOUNDED_LADDER.md](02_COST_ADJUSTED_BOUNDED_LADDER.md) | 成本調整有限加碼 | **研究中，未實作**：需先驗證實際成交／保證金 |
| [03_ORDER_FLOW_GATED_MAKER.md](03_ORDER_FLOW_GATED_MAKER.md) | 根據現有 microprice、OFI、成交流分數做單邊過濾，另設波動過濾 | **已實作** `strategies.order_flow_maker:OrderFlowGatedMaker`（權重未校準） |
| [04_COMPETITION_PNL_SHAPING.md](04_COMPETITION_PNL_SHAPING.md) | 依已核實的前日權益，做保守 MTM 盈利封頂、損失及回撤停機 | **部分實作** `strategies.competition_pnl:CompetitionPnlShaping`（未實作資格統計／權益加碼） |
| [05_TSMC_VIRTUAL_MAX_COMBINED.md](05_TSMC_VIRTUAL_MAX_COMBINED.md) | 跨策略的台積電期貨組合狀態機 | **研究中，未實作** |

## 建議研究順序

1. 先驗證 `01` 的同價觸及成交率、送單生效延遲與一跳費後淨利。
2. 再實作 `03`，比較過濾前後的成交後 1、2、5、10 秒價格漂移。
3. 確認首筆成交後確實常發生短暫逆向再反彈，才加入 `02`。
4. 用按日資料評估 `04`，因官方夏普是以每日報酬計算，不能只看逐筆勝率。
5. 最後才使用 `05` 整合並做 walk-forward 與競賽模式測試。

## 共用符號

- `P`：期貨成交價格。
- `M`：契約乘數；標準台積電期貨通常為 2,000 股。
- `tau`：最小跳動單位。
- `F`：單口單邊手續費。
- `t`：單邊期交稅率。
- `Q`：目前總持倉口數。
- `p_bar`：目前持倉平均成本。
- `E`：帳戶權益數。

一口完整來回成本近似為：

```text
C(P) = 2F + 2tPM
```

一跳完整循環的費後利潤近似為：

```text
G_1tick(P) = M * tau - C(P)
```

只有在 `G_1tick(P) > 0` 時，一跳策略才具有基本成本優勢。

## 官方參考

- 新一代虛擬交易所教學手冊：https://sim2.taifex.com.tw/Portal/TutorialGuide.pdf
- 115 年縱橫期海活動與競賽規則：https://www.taifex.com.tw/eventTryTryp9/index
- 115 年最終成績：https://www.taifex.com.tw/eventTryTryp9/grade
- 股票期貨契約規格：https://www.taifex.com.tw/cht/2/sTF
- 期貨交易稅說明：https://www.taifex.com.tw/cht/9/tradersQAProducts

## 規則邊界

策略只利用官方揭示的獨立見價見量機制。不要使用大量無成交意圖委託、異常重複撤掛、同一行情事件重複製造成交或其他可能被認定為異常交易的行為。競賽規則保留取消異常交易帳號資格的權利。
