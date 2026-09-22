# Azure Windows VM 部署指南（TTB 虛擬交易）

TTB 是 Windows GUI 程式，並透過本機 `localhost:8080` / ZMQ `51141` 提供資料。因此部署型態應是**單台 Windows VM**；不要使用 Linux Container、Azure Functions 或 VMSS。

## 1. 建議規格與成本

- 區域：East Asia（若該區域無容量，可改 Japan East）
- 映像：Windows Server 2022 Datacenter, Gen2, Desktop Experience
- SKU：`Standard_B2as_v2`（2 vCPU、8 GiB RAM）
- 磁碟：64 GiB `StandardSSD_LRS`
- 公開 IP：Standard；只允許你的固定 IP 進入 RDP

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

目前 RDP NSG 規則只允許 `49.216.172.184/32`。若你的公網 IP 改變，必須先更新 NSG，否則會無法連線。管理員密碼不記錄在 repo，登入後應立即更換。

```powershell
mstsc /v:20.189.93.22
```

用上一個命令輸出的 IP 開啟 Windows Remote Desktop（`mstsc /v:<PUBLIC_IP>`），登入後在 VM 內安裝：

```powershell
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
winget install --id Python.Python.3.13 -e --accept-source-agreements --accept-package-agreements
winget install --id Tailscale.Tailscale -e --accept-source-agreements --accept-package-agreements

mkdir C:\TTB
cd C:\TTB
git clone https://github.com/ChiJiun/TaifexTradeBox.git .
py -3.13 -m pip install --upgrade pip
py -3.13 -m pip install pyzmq requests
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

開始前請人工確認：帳戶為 flat、沒有未成交單、商品代碼正確、VM 時區為 Asia/Taipei，且已經理解比賽/模擬平台的 `見價見量` 成交規則。第一次不要讓 Task Scheduler 自動送單。

```powershell
cd C:\TTB\Sample\Python
py -3.13 sim_hft_bot.py `
  --symbol <ACTIVE_SYMBOL> --mode ttb `
  --confirm-virtual-orders --confirm-flat-account
```

若 TTB、RDP 或網路異常，策略不能保證遠端平倉；恢復連線後必須先檢查 TTB 的實際持倉與委託，再重啟 bot。這是虛擬帳戶也必須遵守的操作安全界線。

## 6. 24/7 監控（建議先手動驗證）

TTB 是 GUI 程式，Task Scheduler 應設為「Only when user is logged on」，讓它在同一個互動式桌面啟動。先建立 `C:\TTB\start-bot.cmd`：

```bat
@echo off
timeout /t 60 /nobreak
cd /d C:\TTB\Sample\Python
py -3.13 sim_hft_bot.py --symbol <ACTIVE_SYMBOL> --mode shadow
```

確認 shadow 穩定後，再把 `--mode shadow` 改成 TTB 模式並加入兩個 confirmation flags；建議先連續觀察數個交易日，再設定開機/登入自動啟動：

```powershell
schtasks /Create /TN "TTB GUI" /SC ONLOGON `
  /TR "C:\TTB\TTB.exe" /RL HIGHEST /F
schtasks /Create /TN "TTB Strategy" /SC ONLOGON `
  /TR "C:\TTB\start-bot.cmd" /RL HIGHEST /F
```

RDP 斷線不要登出 Windows；登出會終止 GUI 與互動式策略。重開機後仍可能需要人工登入 TTB，不能把「自動登入」當成交易安全機制。

## 7. 省成本與告警

```powershell
# 非交易時段停止並釋放 VM（停止後仍可保留磁碟/IP）
az vm deallocate --resource-group $rg --name $vm

# 重新啟動
az vm start --resource-group $rg --name $vm
```

在 Azure Portal 建立 Cost Management Budget，至少設定 50%、75%、90% 告警。只有在策略已通過 shadow 與人工復核後，才適合把 VM 改成長時間執行；交易期間應優先確保 TTB 與 bot 的進程、磁碟空間和 VM 狀態可觀測。

## 官方參考

- [Azure for Students](https://azure.microsoft.com/en-us/free/students/)
- [Azure Bsv2/Basv2 VM sizes](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/basv2-series)
- [Azure VM cost optimization](https://learn.microsoft.com/en-us/azure/virtual-machines/cost-optimization-plan-to-manage-costs)
- [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
