# Azure Deployment Plan

> **Status:** Deployed

Generated: 2026-09-23 (Asia/Taipei)

---

## 1. Project Overview

**Goal:** Deploy the latest local TaifexTradeBox strategy, monitoring, far-limit order/cancel probe, and fail-closed automation scripts to the existing Azure Windows VM. Deployment verification remains in shadow mode; the separate probe is the only authorized one-order virtual-account test.

**Path:** Modify an existing Azure-hosted development/POC workload.

## 2. Requirements

| Attribute | Value |
|---|---|
| Classification | Development / virtual-trading POC |
| Scale | Small, single VM and single TTB desktop session |
| Budget | Cost-optimized; reuse existing VM |
| Subscription | Azure for Students (`e8e39ad5-f2df-4dad-8161-005a7c962a3c`) |
| Location | East Asia (`eastasia`) |
| Resource group | `a112502533_rg_3957` |
| VM | `ttb-sim-01` (`Standard_B2as_v2`) |
| Destination | `C:\TTB\Sample\Python` |

The subscription, region, resource group, and VM are inherited from the user's explicit request to update the existing Azure deployment and were verified with Azure CLI.

## 3. Components Detected

| Component | Type | Technology | Path |
|---|---|---|---|
| Strategy runner | Background worker | Python | `Sample/Python/sim_hft_bot.py` |
| Strategy core | Trading logic | Python | `Sample/Python/strategy_core.py` |
| TTB adapter | Local API/ZMQ integration | Python | `Sample/Python/ttb/` |
| Order probe | One-order post/cancel smoke test | Python | `Sample/Python/ttb_order_probe.py` |
| Automation | Supervisor, health check, scheduled-task installer | PowerShell | `Sample/Python/automation/` |
| Research/replay tools | Offline analysis | Python | `Sample/Python/replay_market_maker.py`, `monte_carlo_market_maker.py`, `optimize_virtual_competition.py` |
| Tests | Unit/integration tests with mocks | Python unittest | `Sample/Python/test_*.py` |
| TTB desktop | Existing interactive dependency | Windows desktop application | Existing VM installation |

### Existing Infrastructure

| Item | Status |
|---|---|
| Azure Windows VM | Existing and running |
| Public IP | Existing static IP `20.189.93.22` |
| TTB installation | Existing under `C:\TTB` |
| Azure IaC / `azure.yaml` | Not present; not required for this code-only update |
| GitHub repository | Configured but not anonymously accessible |

## 4. Recipe Selection

**Selected:** AZCLI, code-only update to an existing VM.

**Rationale:** No Azure resources need to be provisioned. The exact local working tree, including uncommitted strategy files, will be packaged locally and transferred through Azure VM Run Command. This avoids storing GitHub credentials or tokens on the VM.

## 5. Architecture

**Stack:** Existing Windows VM with an interactive TTB process and a Python monitoring worker.

| Component | Azure Service / Host | Change |
|---|---|---|
| TTB GUI and local APIs | Existing Azure Virtual Machine | No change |
| Python strategy code | Existing Azure Virtual Machine | Update files only |
| Output CSV logs | VM local disk | Preserve existing files |

No new Azure services, networking resources, identities, or public endpoints will be created.

## 6. Provisioning Limit Checklist

| Resource Type | Number to Deploy | Total After Deployment | Limit/Quota | Notes |
|---|---:|---:|---|---|
| New Azure resources | 0 | Unchanged | Not applicable | Code-only update to an existing VM; no quota consumption |

**Status:** All provisioning limits are unaffected. Azure quota lookup is not applicable because the deployment creates zero resources.

## 7. Execution Checklist

### Planning

- [x] Analyze workspace
- [x] Confirm existing subscription, region, VM, and destination
- [x] Scan codebase and identify changed/untracked strategy files
- [x] Select AZCLI code-only deployment recipe
- [x] Define safety and rollback
- [x] User approved and expanded this plan

### Preparation and Validation

- [x] Run the local Python test suite (37 tests passed)
- [x] Compile all Python modules
- [x] Parse all PowerShell automation scripts
- [x] Build a deployment ZIP from project documentation and `Sample/Python`, excluding generated output, caches, credentials, and TTB runtime binaries
- [x] Generate a SHA-256 manifest for the payload
- [x] Set status to `Ready for Validation`
- [x] Invoke the Azure validation workflow and record proof
- [x] Azure CLI installation and authentication
- [x] Existing VM power/provisioning/Guest Agent/extension pre-flight
- [x] Existing destination, Python runtime, TTB process, and monitor process
- [x] Bicep compilation, template validation, what-if, Docker, and static RBAC review marked not applicable because zero infrastructure resources are created

### Deployment

- [x] Confirm VM, VM Agent, and extension state are healthy
- [x] Record whether the current Python monitor is running
- [x] Stop only the strategy Python process while replacing code; do not stop or log out TTB
- [x] Back up the current Azure `C:\TTB\Sample\Python` code to `C:\TTB\deploy-backups\<timestamp>`
- [x] Transfer and extract the validated payload through Azure VM Run Command
- [x] Preserve `output/` CSV data and VM-specific runtime files
- [x] Run the offline test suite on the VM
- [x] Verify deployed SHA-256 hashes
- [x] Restart only shadow monitoring if it was running before deployment
- [x] Verify that monitoring remains in `shadow` mode and no order is submitted

## 8. Safety and Rollback

- Deployment never enables `--mode ttb` and never passes `--confirm-virtual-orders`.
- Existing CSV output is excluded from replacement.
- The current Azure code is backed up before copying any file.
- On validation failure, restore the timestamped backup and leave the bot stopped for inspection.
- TTB remains logged in; the deployment does not log off or restart the VM.
- Secrets, passwords, Git credentials, and local output data are excluded from the package.

## 9. Validation Proof

| Check | Command Run | Result | Timestamp (Asia/Taipei) |
|---|---|---|---|
| Python unit tests | `py -3.13 -m unittest discover -v` | Pass: 37 tests | 2026-09-23 18:20 |
| Python build | `py -3.13 -m compileall -q .` | Pass | 2026-09-23 18:20 |
| PowerShell syntax | PowerShell parser over `automation/*.ps1` | Pass: 3 scripts | 2026-09-23 18:20 |
| Azure CLI | `az version` | Pass: Azure CLI 2.90.0 | 2026-09-23 18:24 |
| Azure authentication | `az account show` | Pass: Azure for Students subscription enabled | 2026-09-23 18:24 |
| VM state | `az vm show -d` | Pass: VM running, provisioning succeeded | 2026-09-23 18:24 |
| VM Agent pre-flight | `az vm get-instance-view` | Pass: Guest Agent Ready, no extensions in progress | 2026-09-23 18:24 |
| Guest prerequisites | `az vm run-command invoke` read-only probe | Pass: TTB root and Python path present, Python 3.13.7, TTB and Python processes running | 2026-09-23 18:25 |
| Deployment payload | 40-file ZIP plus SHA-256 manifest | Pass: 90,010 bytes, SHA-256 `2420181b09ec072a7d530224f899a8ee00493c6a7ccfd0faa8066a5a4617a971` | 2026-09-23 18:27 |
| Infrastructure/RBAC | Static review | Not applicable: no new resources, identity, role assignment, Bicep, Terraform, container, or public endpoint | 2026-09-23 18:26 |
| Azure-side unit tests | `py -3.13 -m unittest discover -v` during deployment | Pass: 37 tests | 2026-09-24 10:17 |
| Final VM pre-flight | VM instance view and extension state | Pass: running, provisioning succeeded, Guest Agent Ready, zero busy extensions | 2026-09-24 10:29 |
| Final health check | `automation/check_ttb_health.ps1` | Pass: TTB/Python/ports/health heartbeat all healthy; position 0 | 2026-09-24 10:30 |
| Live market data | `output/health.json` and bot log | Pass: `TMFJ6` ticks received during the day session | 2026-09-24 10:30 |
| Runtime safety | Process command line and health state | Pass: `--mode shadow`; no virtual-order confirmation flag; no active orders | 2026-09-24 10:30 |
| Final file identity | SHA-256 comparison for runner, adapter, probe, automation, and READMEs | Pass: all checked files match the local working tree | 2026-09-24 10:25 |

**Validated by:** azure-validate workflow

**Validation timestamp:** 2026-09-23 18:26 Asia/Taipei

## 10. Files Generated

| File | Purpose | Status |
|---|---|---|
| `.azure/deployment-plan.md` | Deployment source of truth | Complete for approval |
| Temporary deployment ZIP and manifest | Exact validated payload | Validated |

## 11. Deployment Results

- Code deployed to `C:\TTB\Sample\Python` on `ttb-sim-01`.
- Rollback backups are retained at `C:\TTB\deploy-backups\20260924-101601` and `C:\TTB\deploy-backups\20260924-101746`.
- The failed first staging attempt is retained at `C:\TTB\deploy-failed\20260924-101601` for inspection.
- Scheduled tasks `TTB GUI` and `TTB Automation` run as `azureuser` with the Windows Server-compatible `Interactive` logon type.
- `TTB Automation` is running in `shadow` mode with `TMFJ6`; the health heartbeat is fresh and actual market ticks are reaching the strategy.
- The strategy reached its simulated session-loss limit and entered the fail-closed `paused` state. Monitoring continues, but this result is not evidence of positive alpha and is a deliberate reason not to enable automated virtual orders yet.
- The far-limit post/cancel probe is deployed and unit-tested but has not been executed against the TTB account. Its explicit flat-account gate remains required.
- No new Azure endpoint or infrastructure resource was created by this deployment.

## 12. Next Step

Keep collecting shadow evidence and review the negative simulated P&L. Before the one-order probe, manually confirm that the TTB virtual account is flat and has no working orders; only after the probe succeeds and the strategy passes multiple-session validation should `automation/config.json` be considered for `ttb` mode.
