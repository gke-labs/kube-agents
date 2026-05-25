---
name: review-security-k8s-storage
description: Reviews Kubernetes storage configurations, PVs, and VolumeMounts for data leakage and privilege escalation risks.
---
# Task
Review storage configurations (`StorageClass`, `PersistentVolume`, `PVC`), Volumes, and `VolumeMounts` to prevent data leaks and privilege escalation.

# Checks
## 1. Volume Mount Security
- **Read-Only**: Flag `VolumeMounts` missing `readOnly: true` for writable types (`PVCs`, `hostPath`, `emptyDir`) unless write is required.
- **subPath Abuse**: Flag `subPath` on volumes writable by untrusted users (symlink breakout risk).
- **hostPath**: Flag `hostPath` usage. Recommend `local` PVs.
- **fsGroup**: Require `fsGroup` in `securityContext` to avoid running containers as root for storage access.

## 2. StorageClass & PV Security
- **Access Modes**: Flag `ReadWriteMany` (RWX). Require `ReadWriteOnce` (RWO) or `ReadOnlyMany` (ROX) to reduce blast radius.
- **Encryption**: Require `StorageClasses` to enforce encryption at rest (e.g., CMEK, `encrypted: "true"`).
- **Reclaim Policies**: Flag deprecated `Recycle`. Flag `Retain` on sensitive volumes without automated wipe processes.
- **Volume Expansion**: Flag `allowVolumeExpansion: true` without strict namespace `ResourceQuotas` (DoS risk).

## 3. CSI Drivers
- **CSI Secrets**: For CSI Secrets Store, require strict limits on mountable secrets and cloud identity access.
