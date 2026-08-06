# 🎬 Zero-Friction Installer & Day-2 Control Panel PR Video Demos

This document provides visual walkthroughs and terminal animations demonstrating the new features introduced in **[PR #519](https://github.com/gke-labs/kube-agents/pull/519)**.

---

## ⚡ 0. One-Liner Installer Quickstart (`curl -fsSL ... | bash`)

![Demo 0: One-Liner Quickstart](https://raw.githubusercontent.com/fkc1e100/kube-agents/feat/zero-friction-installer/docs/images/demos/one_liner_install_demo.webp)

- **Command**:
  ```bash
  curl -fsSL https://gke-labs.github.io/kube-agents/install.sh | bash
  ```
- **Purpose**: Zero-friction setup directly from any Linux terminal or Cloud Shell without cloning the repository manually!
- **Highlights Shown**: Cloud Shell environment auto-detection (`CLOUD_SHELL=true`), GCP project auto-discovery (`gca-gke-2025`), cluster auto-discovery (`kcc-dash-dont-delete`), interactive Web UI configuration, automated GKE deployment, and pod health checkpoint validation.

---

## 🟢 1. Interactive Setup Wizard with Step-Back (`b`) Navigation (`./install.sh`)

![Demo 1: Interactive Setup Wizard](https://raw.githubusercontent.com/fkc1e100/kube-agents/feat/zero-friction-installer/docs/images/demos/interactive_wizard_demo.webp)

- **Purpose**: Zero-friction setup wizard for developer workstations and Cloud Shell.
- **Key Enhancements Shown**:
  - Environment & prerequisite auto-detection (`git`, `gcloud`, `kubectl`, `gh`, `helm`).
  - Target GCP project verification (`gca-gke-2025`) & GKE cluster auto-discovery (`kcc-dash-dont-delete`).
  - **Step-Back Navigation**: Type **`b`** or **`back`** at any menu prompt to step backward without restarting.
  - Interactive selection of Hermes Web UI (Port 9119) and permission boundary.
  - Post-installation output of exact `kubectl port-forward` commands.

---

## 🟡 2. Day-2 Control Panel (`raspi-config` Style) (`./install.sh --menu`)

![Demo 2: Day-2 Control Panel Enabling Web UI](https://raw.githubusercontent.com/fkc1e100/kube-agents/feat/zero-friction-installer/docs/images/demos/control_panel_demo.webp)

- **Purpose**: Fast Day-2 operational control panel modelled after Raspberry Pi's `raspi-config`.
- **Key Enhancements Shown**:
  - Active cluster configuration dashboard.
  - **1-Click Web UI Toggle**: Option `1` toggles Hermes Web UI between `ENABLED` and `DISABLED`.
  - **15-Second Live Re-Deployment**: Option `6` updates state in `vars.sh` and re-applies `platform-agent.yaml` directly to GKE without cluster downtime or data loss!

---

## 🔵 3. Non-Interactive Automated CI/CD Setup (`./install.sh -y`)

![Demo 3: Non-Interactive Automated CI/CD Setup](https://raw.githubusercontent.com/fkc1e100/kube-agents/feat/zero-friction-installer/docs/images/demos/non_interactive_demo.webp)

- **Purpose**: Zero-prompt automated execution for GitHub Actions CI/CD and AI agent automation.
- **Key Enhancements Shown**:
  - Automatically respects CLI flags (`--non-interactive`, `--enable-web-ui=true`, `--project-id`, `--cluster-name`).
  - Writes machine-readable execution report to `/tmp/kube-agents-install-report.json`.

---

## 🟣 4. Day-2 Control Panel: Updating Chat Integrations (`./install.sh --menu`)

![Demo 4: Day-2 Updating Chat Integrations](https://raw.githubusercontent.com/fkc1e100/kube-agents/feat/zero-friction-installer/docs/images/demos/update_chat_demo.webp)

- **Purpose**: Updating user permissions or chat settings on active clusters.
- **Key Enhancements Shown**:
  - Selects Option `2` (Manage Chat Integrations), updates allowed user list (`sre-team@google.com`), saves `vars.sh`, and updates Google Chat Pub/Sub streaming in ~15 seconds.

---

## 🔴 5. Day-2 Control Panel: Disabling Web UI for Hardening (`./install.sh --menu`)

![Demo 5: Day-2 Disabling Web UI Security Hardening](https://raw.githubusercontent.com/fkc1e100/kube-agents/feat/zero-friction-installer/docs/images/demos/disable_webui_demo.webp)

- **Purpose**: Hardening attack surface when debugging is finished.
- **Key Enhancements Shown**:
  - Selects Option `1` (Toggle Hermes Web UI) ➔ Toggles to `DISABLED` ➔ Executes `kubectl apply` to remove container `platform-agent-dashboard` and close port `9119` in 15 seconds!
