# Azure Windows VM 部署指南（TTB 虛擬交易）

TTB 是 Windows GUI 程式，並透過本機 `localhost:8080` / ZMQ `51141` 提供資料。因此部署型態應是**單台 Windows VM**；不要使用 Linux Container、Azure Functions 或 VMSS。

## 1. 建議規格與成本

- 區域：East Asia（若該區域無容量，可改 Japan East）
- 映像：Windows Server 2022 Datacenter, Gen2, Desktop Experience
- SKU：`Standard_B2as_v2`（2 vCPU、8 GiB RAM）
- 磁碟：64 GiB `StandardSSD_LRS`
- 公開 IP：Standard；優先使用 Bastion、Tailscale 或 JIT 管理 RDP

`B2as_v2` 適合先跑 GUI、TTB、Python 與記錄檔。若只做 shadow 測試，可先用較小規格；若 24/7 運作，請設定 Azure Budget。Azure for Students 的額度通常足以做短期測試，但不是永久免費，必須以 Azure Portal 中你帳戶的剩餘 credit 為準。

## 2. 建立 VM（Azure CLI）

在本機 PowerShell 執行，先安裝並登入 Azure CLI：

```powershell
az login
az account list --output table
az account set --subscription "<SUBSCRIPTION_ID>"

$rg = "rg-ttb-sim"
$loc = "eastasia"
$vm = "ttb-sim-01"
$nsg = "$vm-nsg"
$admin = "azureuser"
$myIp = "<YOUR_PUBLIC_IP>/32"   # 只允許你的目前公網 IP，例如 203.0.113.10/32

az group create --name $rg --location $loc
az network nsg create --resource-group $rg --name $nsg --location $loc
az network nsg rule create `
  --resource-group $rg --nsg-name $nsg --name Allow-RDP-From-Home `
  --priority 100 --direction Inbound --access Allow --protocol Tcp `
  --source-address-prefixes $myIp --source-port-ranges '*' `
  --destination-address-prefixes '*' --destination-port-ranges 3389

az vm create `
  --resource-group $rg --name $vm --location $loc `
  --image Win2022Datacenter:windows:server-gen2:latest `
  --size Standard_B2as_v2 --admin-username $admin `
  --admin-password '<STRONG_PASSWORD>' `
  --os-disk-size-gb 64 --storage-sku StandardSSD_LRS `
  --nsg $nsg --public-ip-sku Standard `
  --nic-delete-option delete --os-disk-delete-option delete

az vm show -d --resource-group $rg --name $vm `
  --query "publicIps" --output tsv
```

不要把 RDP 規則改成 `0.0.0.0/0`。如果家用 IP 會變動，先用 Tailscale 登入 VM，再移除公開 RDP 規則。

## 3. 連線與安裝軟體

### 已建立的 VM

本專案目前使用以下 Azure VM：

```text
資源群組：a112502533_rg_3957
名稱：ttb-sim-01
區域：East Asia
規格：Standard_B2as_v2
OS：Windows Server 2022 Gen2
固定公網 IP：20.189.93.22
```

**2026-10-13 安全更新：**已以 Azure CLI 實際將 `ttb-sim-01-nsg` 中的 `Allow-RDP-From-Home` TCP/3389 來源從 `*` 收緊為當前管理端出口 IPv4 的 `/32`，更新後透過 Azure 查詢確認，從相同管理端測試 TCP 3389 可連通。此來源可能因行動網路、VPN 或切換 Wi-Fi 而變更。

管理端 IP 變更時，請先在具 Azure CLI 登入權限的 PowerShell 執行：

```powershell
# 唯讀：預覽目前白名單與本機公網 IPv4
powershell -ExecutionPolicy Bypass -File azure\Set-TtbRdpAllowlist.ps1

# 實際更新：將 RDP 限制為當前 IPv4 /32
powershell -ExecutionPolicy Bypass -File azure\Set-TtbRdpAllowlist.ps1 -Apply
```

如無法進行 RDP，仍可透過 Azure Portal 或 `az network nsg rule update` 修改白名單，無需先登入 VM。不要為了方便連線將來源改回 `*` 或 `0.0.0.0/0`；長期建議改用 Bastion、Tailscale 或 JIT。管理員密碼不記錄在 repo。

```powershell
mstsc /v:20.189.93.22
```

### 目前部署狀態（2026-09-24）

- 專案最新版已部署到 `C:\TTB\Sample\Python`，Azure 端 37 個單元測試通過。
- `TTB Automation` 排程正在執行，完整啟動參數為 `TMFJ6`、`market-maker`、`shadow`；沒有傳入任何允許虛擬委託的旗標。
- TTB、Python、8080、51141 與健康心跳檢查通過，交易時段已收到即時 tick，部位為 0、沒有活動委託。
- shadow 策略已碰到模擬時段損失上限並進入 `paused`。這證明風控會阻止繼續報價，但也表示目前不能把績效解讀為正 alpha，更不能直接切換為 `ttb`。
- 遠價下單／撤單探測器已部署且通過離線測試；尚未作用於帳戶，因為執行前仍需人工確認空倉及沒有未成交單。
- 回復備份保留於 `C:\TTB\deploy-backups\20260924-101746`；完整部署證據記錄於 `.azure\deployment-plan.md`。

用上一個命令輸出的 IP 開啟 Windows Remote Desktop（`mstsc /v:<PUBLIC_IP>`），登入後在 VM 內安裝：

```powershell
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
winget install --id Python.Python.3.13 -e --accept-source-agreements --accept-package-agreements
winget install --id Tailscale.Tailscale -e --accept-source-agreements --accept-package-agreements

mkdir C:\TTB
cd C:\TTB
git clone https://github.com/ChiJiun/TaifexTradeBox.git .
py -3.13 -m pip install --upgrade pip
py -3.13 -m pip install -r Sample/Python/requirements.txt
```

私有 GitHub repo 需要 GitHub 登入或 token；不要把 token 寫入這份文件或提交到 repo。安裝 Tailscale 後完成瀏覽器登入，確認能用 Tailscale IP 連線，再把 NSG 的公開 RDP 規則刪除或改成你的管理 IP。

## 4. 啟動順序與 smoke test

1. 以互動式 Windows 工作階段啟動 `C:\TTB\TTB.exe`。
2. 登入 TTB，選擇正確的模擬交易/競賽模式，確認本機 API 與行情更新。
3. 在第二個 PowerShell 視窗執行單元測試：

```powershell
cd C:\TTB\Sample\Python
py -3.13 -m unittest discover -v
```

4. 先跑 shadow（不送單），`<ACTIVE_SYMBOL>` 必須換成當期有效商品代碼：

```powershell
py -3.13 sim_hft_bot.py --symbol <ACTIVE_SYMBOL> --mode shadow
```

確認至少完成一個交易時段的行情、quote CSV、decision CSV 與錯誤紀錄後，才進入下一步。

## 5. 切換到 TTB 虛擬下單

開始前請人工確認：帳戶為 flat、沒有未成交單、商品代碼正確、VM 時區為 Asia/Taipei，且已經理解比賽/模擬平台的 `見價見量` 成交規則。第一次不要讓 Task Scheduler 自動送單。先執行遠價委託／撤單探測器：

```powershell
cd C:\TTB\Sample\Python
py -3.13 ttb_order_probe.py `
  --symbol TMFJ6 --side buy --distance-ticks 2000 `
  --confirm-virtual-order-probe --confirm-flat-account
```

成功標準是：新單 API 接受、取得委託編號、撤單 API 接受、委託報告進入終態，而且成交數量為零。結果位於 `output\order-probes`。即使價格離市場很遠，也不能保證絕不成交；任何非零成交量都必須先人工處理。

```powershell
cd C:\TTB\Sample\Python
py -3.13 sim_hft_bot.py `
  --symbol <ACTIVE_SYMBOL> --mode ttb `
  --confirm-virtual-orders --confirm-flat-account
```

若 TTB、RDP 或網路異常，策略不能保證遠端平倉；恢復連線後必須先檢查 TTB 的實際持倉與委託，再重啟 bot。這是虛擬帳戶也必須遵守的操作安全界線。

## 6. 24/7 監控與自動執行

TTB 是 GUI 程式，Task Scheduler 必須使用互動式登入類型（PowerShell 列舉值 `Interactive`），讓它在 `azureuser` 的桌面工作階段啟動。RDP 可以關閉，但不能登出 Windows。

先複製設定檔；預設是 shadow：

```powershell
cd C:\TTB\Sample\Python
Copy-Item automation\config.example.json automation\config.json
powershell -ExecutionPolicy Bypass -File automation\install_azure_tasks.ps1 -RunAsUser azureuser
Start-ScheduledTask -TaskName 'TTB Automation'
```

先跑 preflight，再檢查健康狀態：

```powershell
powershell -ExecutionPolicy Bypass -File automation\preflight_ttb.ps1
powershell -ExecutionPolicy Bypass -File automation\check_ttb_health.ps1
Invoke-RestMethod http://127.0.0.1:8765/status
Get-Content C:\TTB\Sample\Python\output\health.json
Get-Content C:\TTB\Sample\Python\output\supervisor\supervisor.log -Tail 50
```

`install_azure_tasks.ps1` 也會建立 `TTB Health Watchdog`，每 5 分鐘執行一次。若需要外部告警，可只在 VM 使用者環境設定 `TTB_ALERT_WEBHOOK_URL`；repo 不保存 webhook secret。watchdog 只在故障轉態、每 60 分鐘持續故障提醒與恢復時送出通知。

健康狀態的主要值：

| 狀態 | 意義 |
|---|---|
| `healthy` | 正常收到行情並執行決策 |
| `waiting_market` | 休市待命，屬於正常狀態 |
| `stale_market_data` | 交易時段行情停止，已撤銷已知掛單並暫停入口 |
| `paused` | 時段或風控條件暫停 |
| `error` | 策略／委託流程發生致命錯誤 |
| `manual_reconciliation_required` | 有未知部位或異常退出，必須人工核對 |

完成 shadow 與委託探測後，才能將 `automation\config.json` 的 `mode` 改為 `ttb`，並同時將 `confirmVirtualOrders`、`confirmFlatAccount` 設為 `true`。此外，live 入口單必須設定經驗證的 `--max-notional`、`--available-margin-field`、`--min-available-margin`；目前尚無真實 TTB margin response fixture，預設拒絕新增部位。相關設定與計數持久化請參考 [Python 風控說明](../Sample/Python/README.md)。live 虛擬交易發生非正常退出時，supervisor 不會自動重啟，而會留下 `manual-reconciliation-required.flag`。**禁止直接刪除此檔**；在 TTB GUI 確認正確帳戶、空倉且沒有未成交單後，以 `py -3.13 reconcile_ttb.py` 唯讀查詢，再依 [Operator Runbook](../Sample/Python/OPERATOR_RUNBOOK.md) 的四項人工確認指令清除旗標。此工具只處理本機旗標，不會下單或自動平倉。

### 無人值守限制

- Bot 可以跨休市時段常駐，但只有交易時段會產生入口委託。2026 年官方開休市日期由 `Sample/Python/automation/taifex_calendar_2026.json` 管理；未提供新年度驗證日曆不得在 2027 年繼續 live 交易。到期日/換月只允許人工確認，不能帶倉自動換月。
- TTB 登入、競賽選擇與持倉核對仍依賴 GUI；Windows 重開機後可能需要人工處理。
- 程式會用委託報告與 `QUERYRESTOREFILLREPORT()` 交叉對帳 broker position；但異常退出後仍不會自動重啟 live 模式，因為 GUI 競賽選擇、登入狀態與未知外部委託仍需要人工確認。
- 登出 Windows 會終止 GUI 與互動式排程；只關閉 RDP 視窗不會登出。

## 7. 省成本與告警

```powershell
# 非交易時段停止並釋放 VM（停止後仍可保留磁碟/IP）
az vm deallocate --resource-group $rg --name $vm

# 重新啟動
az vm start --resource-group $rg --name $vm
```

在 Azure Portal 建立 Cost Management Budget，至少設定 50%、75%、90% 告警。只有在策略已通過 shadow 與人工復核後，才適合把 VM 改成長時間執行；交易期間應優先確保 TTB 與 bot 的進程、磁碟空間和 VM 狀態可觀測。quote/decision CSV 會每日輪替，`cleanup_runtime.ps1` 預設清除 14 天前的 runtime CSV／order-probe 檔，supervisor log 超過 10 MB 會輪替。

## 官方參考

- [Azure for Students](https://azure.microsoft.com/en-us/free/students/)
- [Azure Bsv2/Basv2 VM sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/basv2-series)
- [Azure VM cost optimization](https://learn.microsoft.com/en-us/azure/virtual-machines/cost-optimization-plan-to-manage-costs)
- [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
