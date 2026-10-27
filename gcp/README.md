# GCP Windows VM 部署指南（TTB 虛擬交易）

> **本頁只處理 GCP VM 部署。唯一操作手冊為 [Sample/Python/OPERATIONS.md](../Sample/Python/OPERATIONS.md)**；異常處理見 [OPERATOR_RUNBOOK.md](../Sample/Python/OPERATOR_RUNBOOK.md)。Azure 版本見 [azure/README.md](../azure/README.md)，兩者共用 VM 端安裝腳本 [deploy/Install-TtbRuntime.ps1](../deploy/Install-TtbRuntime.ps1)。

TTB 是 .NET 10 WinForms 程式，自動登入依賴 Windows UI Automation 與 Credential Manager，因此只能跑在 Windows（Linux／Wine 不支援）。GCP 的 Windows 映像含授權費，BYOL 需要獨占節點，不划算。

## 1. 規格

| 項目 | 值 |
|---|---|
| 專案 | `ttb-competition`（帳單帳戶可用 `gcloud billing projects link` 隨時更換） |
| 區域 | `asia-east1-b`（彰化） |
| 機型 | `e2-medium`（2 共用 vCPU、4 GB）；記憶體不足時關機改 `e2-standard-2` |
| 映像 | `windows-2022`（Desktop Experience） |
| 磁碟 | 50 GB `pd-balanced`（Windows 映像最小值） |
| 網路 | 專用 VPC `ttb-net`；`ttb-allow-rdp-ssh-home` 只允許你的 /32，`ttb-allow-iap` 允許 IAP 35.235.240.0/20 |
| 執行帳號 | `ttbuser`（SSH 由 gcloud 金鑰建立；RDP 密碼由你自己重設） |

## 2. 建立 VM（已建立者略過）

```powershell
$p = 'ttb-competition'; $ip = (Invoke-RestMethod https://api.ipify.org)
gcloud compute networks create ttb-net --project $p --subnet-mode=custom
gcloud compute networks subnets create ttb-subnet --project $p --network ttb-net --region asia-east1 --range 10.10.0.0/24
gcloud compute firewall-rules create ttb-allow-rdp-ssh-home --project $p --network ttb-net --allow tcp:3389,tcp:22 --source-ranges "$ip/32"
gcloud compute firewall-rules create ttb-allow-iap --project $p --network ttb-net --allow tcp:3389,tcp:22 --source-ranges 35.235.240.0/20
gcloud compute instances create ttb-sim-gcp --project $p --zone asia-east1-b --machine-type e2-medium `
  --network ttb-net --subnet ttb-subnet --image-family windows-2022 --image-project windows-cloud `
  --boot-disk-size 50GB --boot-disk-type pd-balanced --shielded-secure-boot --shielded-vtpm `
  --metadata enable-windows-ssh=TRUE,sysprep-specialize-script-cmd="googet -noconfirm=true install google-compute-engine-ssh"
```

## 3. 部署

```powershell
.\gcp\Deploy-TtbRuntime.ps1                     # 乾跑：只打包與計算 SHA256
.\gcp\Deploy-TtbRuntime.ps1 -Bootstrap -Apply   # 首次：裝 .NET 10、VC++、Python 3.13、主機設定，再部署
.\gcp\Deploy-TtbRuntime.ps1 -Apply              # 之後更新程式
```

只部署已 commit 的版本。安裝失敗會自動還原（首次安裝除外），`automation\config.json` 與 `output\` 不會被覆蓋。

## 4. 只有你能做的步驟

1. 設定 RDP 密碼（密碼只會顯示在你的終端機）：`gcloud compute reset-windows-password ttb-sim-gcp --project ttb-competition --zone asia-east1-b --user ttbuser`
2. RDP 到外部 IP，以 `ttbuser` 登入。
3. 存 TTB 帳密：`cmdkey /generic:TaifexTradeBox/TTB /user:<TTB 帳號> /pass`（會提示輸入密碼）。
4. 開一次 TTB，選競賽，並在「報價設定」加入近月與次月合約。
5. 設定 Windows 自動登入：`powershell -File C:\TTB\azure\Set-TtbAutologon.ps1 -UserName ttbuser`。
6. 換網路時更新防火牆：`.\gcp\Set-TtbRdpAllowlist.ps1 -Apply`。

## 5. 省成本

- **盤中才開機**：`.\gcp\Set-TtbInstanceSchedule.ps1`（乾跑）→ 加 `-Apply`。平日 08:00 開機、14:00 關機（Asia/Taipei）；GCE 可能延後最多 15 分鐘執行（最晚 08:15 開機，仍早於 08:45 開盤）。開機後由 Autologon → `TTB Automation` 自動接手。取消：`-Remove -Apply`。
  - **關機等於硬停止**：Windows 會先登出 Autologon 工作階段再關機，bot、TTB、supervisor 都會被直接結束，不會執行 bot 的關機流程。真正的保護有兩層：收盤前 10 分鐘 engine 停止進場並出場；下次開機時 live gate 與 bot 會檢查整個帳戶，有部位或掛單就拒絕交易。
  - **盤中手動關機**可能留下部位或掛單：下次開機會偵測到並寫入對帳旗標，當天不再交易，部位會留到隔天自動恢復或人工處理。盤中要停機，先在 VM 上寫入 `bot-stop.requested` 讓 bot 正常停止再關機。
  - 國定假日仍會開機但不交易；要交易夜盤就不能用這個排程。
- 不用時 `gcloud compute instances stop ttb-sim-gcp --project ttb-competition --zone asia-east1-b`；停機只收磁碟費。
- 改機型需先停機：`gcloud compute instances set-machine-type ttb-sim-gcp --machine-type e2-standard-2 ...`。
- 在 GCP Console「帳單 → 預算與快訊」設定預算通知。
