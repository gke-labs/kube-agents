<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Pre-Flight Machine-Family Migration Validator (`machine_family_preflight.py`)

When migrating or replacing an existing GKE node pool across machine families (for example, moving from `n1`, `e2`, `n2`, or `n2d` to `n2`, `n2d`, `n4`, `c3`, or `c4`), always run `/opt/data/scripts/machine_family_preflight.py` **before** generating any ComputeClass or NodePool YAML:

```bash
python3 /opt/data/scripts/machine_family_preflight.py \
  --project <project> --location <location> --cluster <cluster> \
  --node-pool <pool> --target-family <target-family>
```

When the source cluster or node pool configuration is provided in the prompt or saved as JSON rather than read from a live cluster, pass it via `--cluster-json` or `--node-pool-json` (accepts a file path, `-` for stdin, or an inline JSON object) and `--target-family` (repeatable for `n2`, `n2d`, `n4`, `c3`, `c4`, or an explicit shape such as `c3-standard-8-lssd`):

```bash
python3 /opt/data/scripts/machine_family_preflight.py \
  --cluster-json /tmp/source_cluster.json --target-family n4
```

The validator checks and grades:

- **Security settings:** rejects disabled `shieldedNodes` (`shielded-nodes-disabled`), disabled vTPM (`shielded-vtpm-disabled`), disabled integrity monitoring (`shielded-integrity-monitoring-disabled`), and disabled Secure Boot (`shielded-secure-boot-disabled`) on Shielded-only targets (`n2`, `n2d`, `n4`, `c3`, `c4`); rejects `confidentialNodes` on non-confidential families (`n2`, `n4`, `c4`, `confidential-vm-unsupported-family`); and flags AMD `SEV`/`SEV_SNP` (`n2d`) versus Intel `TDX` (`c3`) transitions (`confidential-vm-technology-mismatch`).
- **Disk and Local SSD constraints:** rejects Persistent Disk types (`pd-*`) on Hyperdisk-only families (`n4`, `c4`, `disk-type-pd-on-hyperdisk-only`), rejects `pd-standard` on `c3` (`disk-type-pd-standard-unsupported`), rejects Hyperdisk boot disks on `n2`/`n2d` (`disk-type-hyperdisk-unsupported-boot`), rejects all Local SSD configs (`localSsdCount`, `ephemeralStorageLocalSsdConfig`, `localNvmeSsdBlockConfig`) on `n4` (`local-ssd-unsupported-on-n4`), and requires fixed NVMe `*-lssd` shapes without legacy `localSsdCount` on `c3` and `c4` (`local-ssd-requires-lssd-shape`, `local-ssd-legacy-count-unsupported`).
- **Version and OS image floors:** enforces minimum GKE control-plane and node versions per target family (`gke-control-plane-version-below-floor`, `gke-node-pool-version-below-floor`), Confidential TDX (`1.28.6-gke.1317000+`, `gke-version-below-confidential-tdx-floor`), and `dynamic-rwo` cross-generation PV topology (`1.35.3-gke.1290000+`, `dynamic-rwo-version-floor`), and rejects legacy Dockershim or unsupported OS images (`os-image-legacy-docker`, `os-image-windows-unsupported`, `os-image-incompatible`).

Report the validator's verdict (`VERDICT: COMPATIBLE` or `VERDICT: INCOMPATIBLE`), summary table, and every `[BLOCKER]` and `[WARNING]` rule ID and remediation **before** proposing any ComputeClass or NodePool manifest, and never copy rejected settings (`pd-*` on `n4`/`c4`, `localSsdCount` on `n4`, or `enableSecureBoot: false` / `enableVtpm: false`) into generated YAML.



