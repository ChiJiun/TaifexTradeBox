# TTB 操作手冊（單一作業入口）

> **作業規範以本文件為準。** [MARKET_MAKER.md](MARKET_MAKER.md) 解釋目前內建策略，[DYNAMIC_MARKET_MAKING_PLAN.md](DYNAMIC_MARKET_MAKING_PLAN.md) 和 [strategies/](strategies/) 是研究提案；部署細節見 [Azure README](../../azure/README.md)，異常恢復細節見 [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md)。提案不是可執行或可安全使用的承諾。

## 現況與禁止事項

- **已實作**：即時行情訂閱、shadow 模擬、單商品 TTB Gateway、風控、交易日曆、交易事件 Journal、Fail-Closed、操作員停止旗標、健康監控，以及離線 Walk-forward/OOS。
- **僅可配置**：交易時段、成交成本、各種風控限額、可選的關機平倉嘗試。變更設定不能代表已經完成實際 TTB 驗證。
- **尚未驗收**：正確虛擬競賽帳戶身分、真實 TTB `QUERYMARGIN` 欄位／單位、下單與取消競態，以及 GUI 重啟後完整恢復（#2/#4/#10）。
- **禁止**：無人值守啟用 `--mode ttb`、不明來源委託的猜測式撤單、帶倉換月、手動刪除異常對帳旗標。

## 1. VM、GUI 與環境

實際 TTB GUI 應在互動式 Windows 工作階段登入。VM 實際 RDP NSG 僅限已核准的管理來源 IP，若 IP 改變，使用 [RDP allowlist 工具](../../azure/Set-TtbRdpAllowlist.ps1) 更新，不要改回公網 `*`。

```powershell
cd C:\TTB\Sample\Python
py -3.13 -m pip install -r requirements.txt
py -3.13 -m unittest discover -q
powershell -ExecutionPolicy Bypass -File automation\validate_release.ps1 -WorkDir (Get-Location).Path
```

正式部署前另外執行 `py -3.13 -m automation.check_binary_integrity`，核對 `TTB.exe` 與 `TTBHelp.pyc` 的來源快照 SHA256／Python magic。詳見 [Azure 的二進位供應鏈章節](../../azure/README.md#4-ttb-二進位版本來源控管15)。checksum 通過**不代表官方來源已驗證**。

確認 TTB GUI 已登入**正確的模擬競賽帳戶**、有效契約和當年度經人工核准的交易日曆。檢查服務 API 和 quote stream；程式無法憑當前已驗證 API 自動確認 GUI 競賽身分。

## 2. Shadow 模式：先蒐集行情／決策

```powershell
py -3.13 sim_hft_bot.py --symbol <CURRENT_VERIFIED_SYMBOL> --mode shadow
```

預設監控日盤 08:45–13:45、夜盤 15:00–隔日 05:00。**實際時間須經期交所當日休市表及契約到期日校正**；到期日正常日盤 13:30 截止，沒有該合約下午夜盤。系統不會自動換到下一個月份。Shadow 仍需 GUI 行情連線，但**不送出真實模擬帳戶委託**。

### 從程式解析的 CLI 預設值

以下區塊由 `automation/check_docs_consistency.py` 與 `sim_hft_bot.parse_args()` 做一致性檢查，CLI 改版後應同步修改。

<!-- CLI DEFAULTS BEGIN -->
| CLI 參數 | 預設 |
|---|---|
| `--mode` | `shadow` |
| `--strategy` | `market-maker` |
| `--regular-open` | `08:45` |
| `--regular-close` | `13:45` |
| `--after-hours-open` | `15:00` |
| `--after-hours-close` | `05:00` |
| `--quote-stale-seconds` | `5.0` |
| `--stale-fatal-seconds` | `120.0` |
| `--resume-fresh-ticks` | `3` |
| `--flatten-on-shutdown` | `False` |
| `--daily-loss-limit` | `10000.0` |
| `--max-drawdown` | `40000.0` |
| `--point-value` | `None` |
<!-- CLI DEFAULTS END -->

`--point-value=None` 的意思不是 0：預設依商品自動解析 TXF=200、MTX=50、TMF=10 元／點；其他商品必須明確提供經核對的乘數。手續費、稅率與滑價必須另行依真實競賽商品設定，CLI 預設不可視為官方規格。

## 3. 停機與資料中斷

- 交易時段行情距上次有效報價超過預設 **5 秒**，進入 `stale_market_data`：停止新的入口並處理已知掛單。第一次報價另有啟動寬限時間，不能把 5 秒理解為任何狀態下都立即退出。
- 自收到過行情起，持續超過預設 **120 秒** 的中斷會 Fail-Closed 結束（可用 `--stale-fatal-seconds` 調整）。收到連續預設 **3 筆**新鮮行情後才可能恢復正常報價。
- 程式在同一風控交易日保存 `risk-state.json`，並將成交等事件寫入 Journal。**不能將持久化風控等同於自動辨識或回復帳戶未知持倉。** 回覆矛盾、磁碟故障、Journal 損毀都應停止並人工對帳。
- Ctrl+C／停止旗標會啟動既有關機對帳；**預設不會保證主動平倉**。`--flatten-on-shutdown`（Supervisor `flattenOnShutdown`）只有在允許條件下**嘗試**平倉，可能失敗或部分成交，並不是可靠的斷線平倉保證。
- `/healthz` 表示程序存活，`/readyz` **只有 `healthy` 才 ready**，`/status` 提供完整 health。休市 `waiting_market` 不表示故障，但也不是交易就緒。

## 4. 異常與人工恢復

若 `output/supervisor/manual-reconciliation-required.flag` 存在或 bot 非正常退出，**保持鎖定**。先在 TTB GUI 檢查完整帳戶、所有商品持倉及工作中委託，然後執行唯讀：

```powershell
py -3.13 reconcile_ttb.py
py -3.13 ttb_readonly_capture.py
```

只有完整核對且符合 [異常對帳手冊](OPERATOR_RUNBOOK.md) 四項明確確認條件時，才能執行受控的本機旗標清除；工具不會自動下單、撤單或平倉。

## 5. TTB 虛擬下單：目前仍有人工驗收阻塞

只有在完成 #2/#4 真實 API fixture、受控的一口委託/撤銷測試、帳戶全空、保證金欄位及單位和費稅校準、風控門檻確認後，才可由人工操作員評估開啟。除了 `--confirm-virtual-orders`、`--confirm-flat-account`，仍需要正確 `--max-notional`、`--min-available-margin` 和**實測** `--available-margin-field`。缺任何一項將拒絕新增曝險。**目前不提供可直接複製執行的 live 命令，以免誤開單。**

一口 smoke test 會真的送出虛擬委託，僅能依 [README 的嚴格人工流程](README.md#真實-ttb-api-驗證準備2) 執行。即使遠價也可能成交，絕不從相同價格／口數推定委託歸屬。

## 6. 離線研究與驗收

`walk_forward_oos.py` 會分交易日隔離 Holdout、跑三種成交模型與 Monte Carlo，輸出 JSON/Markdown，且合成或無來源驗證資料不能通過研究閘門。研究成功也**不等於**實際 TTB 虛擬下單已通過 #2/#4。完整範例見 [README](README.md#walk-forwardout-of-sample-研究驗收8)。

## 7. 操作前檢查

- [ ] Windows、TTB GUI 與登入帳戶由人確認，行情沒有回放或時間異常
- [ ] 期交所休市表、合約月份／到期日與契約乘數核對
- [ ] 最近 GitHub CI 與本機離線驗證通過
- [ ] 本機風控狀態、Journal、rate counters 沒有損毀或遺失
- [ ] 所有未知委託、工作中委託與非零持倉均已由 GUI 人工解決
- [ ] `mode=shadow` 作為預設；live 的人工驗收阻塞未解除不得切換

此頁為**操作單一權威來源**。策略研究文件僅用於設計與比較，不應凌駕本操作流程。
