---
title: "Host Tuning — TuneD (Arch)"
tags:
  - kvm
  - tuning
  - arch
  - latency
---

# Host Tuning — TuneD

> [!info] Scope
> Optional. `tuned` optimizes kernel scheduling/I/O for KVM host (`virtual-host` profile). Mutually exclusive with **TLP** power manager — pick one. This note reflects current `tuned` on Arch rolling (Aug 2026).

> [!danger] TLP conflict
> `tuned` and `TLP` both rewrite `sysctl`/`cpufreq`/`usb` autosuspend. Running both = flapping governors, conflicting `udev` rules. **If you use TLP on a laptop → skip this note.**

## Install & enable

```bash
sudo pacman -S --needed tuned
sudo systemctl enable --now tuned
tuned-adm active      # → balanced (default)
tuned-adm list | grep -E 'virtual-host|throughput'
```

## Activate `virtual-host`

```bash
tuned-adm list   # full catalogue (see callout below)
sudo tuned-adm profile virtual-host
tuned-adm active # → Current active profile: virtual-host
sudo tuned-adm verify   # → Verification succeeded
```

> [!example] Profile catalogue (reference)
> ```
> accelerator-performance, atomic-guest/host, aws, balanced[-battery], cpu-partitioning[-powersave],
> default, desktop[-powersave], enterprise-storage, hpc-compute, intel-sst,
> laptop-{ac-powersave,battery-powersave}, latency-performance, mssql, network-{latency,throughput},
> openshift[-control-plane/-node], optimize-serial-console, oracle, postgresql, powersave, realtime[-virtual-guest/-virtual-host],
> sap-{hana[-kvm-guest],netweaver}, server-powersave, spectrumscale-ece, spindown-disk,
> throughput-performance, virtual-{guest,host}
> ```
> For KVM host, `virtual-host` is tuned for I/O scheduling and dirty/writeback that benefits qcow2/`virtio`.

## Verify & revert

```bash
sudo tuned-adm verify
systemctl status tuned
# revert
sudo tuned-adm profile balanced
# or on TLP laptops:
sudo pacman -Rns tuned; sudo systemctl enable --now tlp
```

Related: `[[+ MOC KVM]]`, `[[KVM Services]]` — tuning complements modular idle savings.
