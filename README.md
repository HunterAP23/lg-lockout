# lg-lockout

LG webOS TV Telemetry & Consent Lockout - Block LG TV spyware on the device itself

Using scripts [originally provided by Wendell of Level 1 Techs](https://forum.level1techs.com/t/lg-tv-block-mini-how-to/255178), this repo contains a set of POSIX shell scripts for **rooted** LG webOS TVs that stop the TV from phoning home: they block LG's telemetry, advertising, ACR (automatic content recognition), and OTA endpoints, withdraw every data-collection consent, and switch off the data-sharing settings LG hides across several config stores. The scripts run automatically on every boot through the Homebrew Channel's `init.d` hook, re-apply their settings after LG services try to undo them, and ship with a verifier and diagnostics so you can confirm what actually stuck.

> **Warning.** This modifies system state on a rooted TV. Every file the scripts edit is backed up first (`*.bak.<timestamp>`), and nothing is deleted outright, but use it at your own risk. A factory reset or firmware update can undo some or all of it; router-level DNS/firewall blocking is the only layer that survives those.

## Requirements

- An LG webOS TV rooted with the [webosbrew Homebrew Channel](https://github.com/webosbrew/webos-homebrew-channel), with root SSH enabled.
- Nothing else on the TV. The scripts are pure POSIX `sh` and use only tools present on stock webOS (`sed`, `awk`, `grep`, `mount`, `luna-send`, `systemctl`/`systemd-run`). **No Python is required on the TV.**
- Optional, on your workstation: Python 3.12+ plus `ssh`/`scp` for `lg-service-scan.py`.

## How it works

The lockout is layered, so that if one mechanism is bypassed the others still hold.

| Layer | Mechanism | Provided by |
|---|---|---|
| 1 | DNS / firewall blocking at the router | You (out of scope, but recommended) |
| 2 | Homebrew Channel's built-in OTA blocker | `webosbrew_block_updates` flag, set by `install.sh` |
| 3 | Comprehensive `/etc/hosts` blocklist | `10-block-lg-telemetry` |
| 4 | Withdraw consents, disable data-sharing settings, neutralize ACR | `20-decline-lg-tos` |
| 5 | Suppress the app-update nag | `30-clear-lg-app-update` |
| 6 | Verification and diagnostics | `verify-lockout`, `scan-lg-owners`, `lg-service-scan.py` |

### Boot sequence

On every boot the Homebrew Channel's `startup.sh` runs once (guarded by `flock`). If the `webosbrew_block_updates` flag exists, it first copies `/etc/hosts` to `/tmp`, appends its four core OTA domains, and bind-mounts the copy over `/etc/hosts`. Later, it runs `run-parts /var/lib/webosbrew/init.d`, which executes our hooks in numeric order:

```
startup.sh
 ├─ webosbrew OTA block (bind-mounts /etc/hosts)
 └─ run-parts /var/lib/webosbrew/init.d
     ├─ 10-block-lg-telemetry   stack our hosts blocklist on top
     ├─ 20-decline-lg-tos       consents + settings + acr.xml, then schedule re-checks
     │    └─ +30s / +90s / +180s   --reassert-once passes (systemd-run)
     └─ 30-clear-lg-app-update  wipe cached update state
```

`run-parts` only executes files that are **executable and have no dot in their name**, which is why the hooks have no `.sh` extension and `install.sh` sets them to mode `755`.

## Contents

| File | Installed to | Runs |
|---|---|---|
| `10-block-lg-telemetry` | `/var/lib/webosbrew/init.d/` | Every boot |
| `20-decline-lg-tos` | `/var/lib/webosbrew/init.d/` | Every boot, plus 3 delayed re-assert passes |
| `30-clear-lg-app-update` | `/var/lib/webosbrew/init.d/` | Every boot |
| `verify-lockout` | `/var/lib/webosbrew/lg-lockout/` | Manually |
| `scan-lg-owners` | `/var/lib/webosbrew/lg-lockout/` | Manually (diagnostic) |
| `install.sh` | — | Once, from the copied directory |
| `lg-service-scan.py` | — | On your workstation (diagnostic) |

All three hooks also append to a persistent log at `/var/lib/webosbrew/lg-lockout.log` (trimmed to the last 500 lines), so you can confirm they ran at boot.

## The scripts

### `10-block-lg-telemetry` — hosts blocklist

Sinkholes 42 LG hostnames to `0.0.0.0` and `::1`: OTA/firmware update servers, the SDX telemetry families (`nextlgsdp.com`, `lgtvcommon.com`), ad servers (`lgsmartad.com`), ThinQ/IoT, and LG's LLM/AI platform (`ngfts.lge.com`). Voice-search and Content Store backends are listed but commented out, because blocking them breaks those features.

`/etc` is a read-only overlay on webOS, so the file can't be edited in place. Instead the script builds a new hosts file in `/tmp` and bind-mounts it over `/etc/hosts`:

1. It checks for its own marker line (`# LG lockout block list`) **inside** `/etc/hosts`, rather than checking whether `/etc/hosts` is merely mounted (webosbrew has always mounted it first).
2. It builds the new file from the current effective `/etc/hosts`, stripping any previous copy of its own block, so webosbrew's entries are preserved and nothing is duplicated.
3. It bind-mounts the result on top (trying both `mount --bind` and `mount -o bind`), then confirms the marker is visible.
4. If its block is already active, it refreshes the file in place instead of mounting again.

The bind mount lives in tmpfs and is rebuilt every boot. To disable the extra blocklist without uninstalling, create `/var/luna/preferences/webosbrew_block_updates_extra_off`.

### `20-decline-lg-tos` — consents, settings, and ACR

LG keeps consent and privacy state in three stores owned by `com.webos.settingsservice`. The service's database is authoritative: editing the flat files under `/var/luna/preferences/` directly gets overwritten on the next boot. This script therefore writes through the service with `luna-send`, waiting up to ~40 seconds for it to come up.

**Consent (`eulaStatus`).** Every `*Allowed` flag (24 on the reference TV, read from the eula file with a built-in fallback list) is set to `false`. These are consent records, not network switches: `networkAllowed=false` withdraws consent for LG's network data collection but does not disconnect the TV. Because some webOS versions rebuild `eulaStatus` from the individual agreement documents at boot, and those documents have no `luna-send` setter, the script also marks every document in the eula file as `accepted=false, updated=true`, then restarts `eula-service` if it is a systemd unit.

**Settings.**

| Category | Key | Value |
|---|---|---|
| `option` | `voiceAllowed`, `voice2Allowed`, `usageCare`, `dbgLogUpload`, `faultLogUpload` | `false` |
| `option` | `watchedListCollection`, `thirdPartyCookie` | `off` |
| `general` | `aiNudge`, `screenSaverAd`, `homePromotion`, `voiceLongDistance` | `off` |
| `general` | `launchEulaByHome` | `false` |

If `luna-send` or the settings service is unavailable, the script falls back to editing the flat files with `sed`, adding any key the file doesn't already contain.

**ACR.** The live switch for ACR is the runtime config `/tmp/acr.xml`. The script rewrites every `ACR_On="true"`, `capture="true"`, and `send_data="true"` to `false`. If `LG_ACR_SERVICE` is set, it then restarts that systemd unit so the change takes effect immediately. It also writes the legacy opt-out marker `/mnt/lg/cmn_data/acr/data/eula_disallowed_rebooted`, which some firmware reads; on firmware that ignores it, the marker is harmless.

**Re-assertion.** Some LG services re-enable settings *after* the init.d hooks run (observed with `dbgLogUpload` and a regenerated `acr.xml`). To win that race, the boot pass schedules three `--reassert-once` passes at +30, +90, and +180 seconds using `systemd-run` (or a detached `sleep` loop if `systemd-run` is missing). Each pass re-applies the option/general settings, re-neutralizes `acr.xml`, and rewrites the marker. Re-assert passes never schedule further passes.

Run with `--dry-run` to see every action without changing anything.

### `30-clear-lg-app-update` — app-update nag

Moves aside the Content Store's cached update state (`updateInfo` and `updateDependencyInfo` under `/mnt/lg/cmn_data/var/palm/data/com.webos.appInstallService/`) and replaces each with an empty file, so the store sees no pending updates. The store can recreate these files after boot; running every boot, together with the hosts and DNS layers, keeps the nag suppressed. Adapted from [lgappupdateblocker](https://github.com/dr0dr1dr2dr3/lgappupdateblocker).

### `verify-lockout` — verification

A read-only checklist that exits non-zero if any check fails. It prefers the **live** settings-service state (via `luna-send`) over the flat files, since the service is what actually governs behavior.

1. **Hosts**: representative domains are sinkholed, and `/etc/hosts` is a bind mount.
2. **Processes**: reports whether `sdx`, `uploadd`, `rdxd`, and `admanager` are running (informational; they may run while failing to reach LG).
3. **Consent and settings**: `eulaStatus` flags, every `option`/`general` key above, and `acr.xml` (`ACR_On`, `capture`, `send_data`).
4. **Connections**: no established connections to LG domains.
5. **DNS**: notes that the resolver is the local connman proxy, so upstream blocking belongs on the router.
6. **App-update gate**: `updateInfo` is empty.
7. **Boot log**: the last lines of `lg-lockout.log`, confirming the hooks and re-assert passes ran.

### `scan-lg-owners` — on-TV diagnostic

Read-only. Identifies which service owns or rewrites a setting: it maps each systemd unit's `ExecStart` binaries to the unit, greps units, binaries, and webOS directories for keywords (binary-safe, so it finds strings inside ELF daemons), shows which live process holds `/tmp/acr.xml` or the ACR data directory open, and lists `acr.xml` generator candidates.

```sh
sh /var/lib/webosbrew/lg-lockout/scan-lg-owners                 # default ACR + log-upload keywords
sh /var/lib/webosbrew/lg-lockout/scan-lg-owners dbgLogUpload    # custom keywords
```

### `lg-service-scan.py` — workstation diagnostic

Does the same analysis without making the TV do the work. In a **single SSH session**, it has the TV bundle its unit files, `ExecStart` binaries, `/usr/palm/services`, and `/tmp/acr.xml` into one tarball; it pulls the tarball with **SCP**; then all parsing happens locally. You're prompted for the root password twice (once each by `ssh` and `scp`); the script never handles the password itself. Results are ranked per feature (`acr`, `debug_log_upload`, `fault_log_upload`, `voice`, `ads`).

```sh
python3 lg-service-scan.py 192.168.1.50 --json report.json
python3 lg-service-scan.py 192.168.1.50 --extra-dir /usr/lib        # also scan shared libraries
python3 lg-service-scan.py --from-archive lg-scan-<timestamp>.tgz   # re-parse without the TV
```

Other options: `--keyword`, `--feature NAME=kw1,kw2`, `--max-mb`, `--out`, `--user`, `--ssh-opt`.

## Installation

Copy the directory to the TV and run the installer as root. It defaults to a dry run.

```sh
scp -r lg-lockout-initd root@<TV-IP>:/tmp/
ssh root@<TV-IP>
cd /tmp/lg-lockout-initd
chmod +x *
sh install.sh            # preview
sh install.sh --apply    # install
```

The installer creates the `webosbrew_block_updates` flag, copies the three hooks into `init.d` with mode `755`, and copies `verify-lockout` and `scan-lg-owners` into `/var/lib/webosbrew/lg-lockout/`. It never touches `startup.sh` or `jumpstart.sh` and never runs `rm -rf`.

To apply everything immediately instead of waiting for a reboot:

```sh
for s in 10-block-lg-telemetry 20-decline-lg-tos 30-clear-lg-app-update; do
    sh /var/lib/webosbrew/init.d/$s
done
```

## Verifying

```sh
sh /var/lib/webosbrew/lg-lockout/verify-lockout
```

After a reboot, wait about three minutes so the re-assert passes can finish, then run the verifier. Section 7 should show fresh timestamps from all three hooks and the `20-decline-lg-tos/reassert` passes.

## Configuration

Every path defaults to the real TV location. The environment variables below override them, mainly for testing against a staged copy of the filesystem.

| Variable | Used by | Purpose |
|---|---|---|
| `LG_LOCKOUT_ROOT` | all | Prefix for all filesystem paths |
| `LG_LOG` | hooks, verify | Boot log location |
| `LG_LUNA_SEND` | `20-decline`, verify | `luna-send` binary |
| `LG_WAIT_TRIES` | `20-decline` | Settings-service readiness attempts (2 s apart, default 20) |
| `LG_ACR_XML` | `20-decline`, verify | ACR runtime config (default `/tmp/acr.xml`) |
| `LG_ACR_SERVICE` | `20-decline` | systemd unit to restart after rewriting `acr.xml` |
| `LG_REASSERT_DELAYS` | `20-decline` | Re-assert delays in seconds (default `"30 90 180"`) |
| `LG_NO_SCHEDULE` | `20-decline` | Set to `1` to skip scheduling re-assert passes |
| `LG_HOSTS_SRC`, `LG_HOSTS_TMP`, `LG_MOUNT_POINT`, `LG_OPTOUT_FLAG` | `10-block` | Hosts paths and opt-out flag |
| `LG_SCAN_DIRS`, `LG_GEN_DIRS`, `LG_UNIT_DIRS`, `LG_LIVE_TARGETS`, `LG_SERVICE_LIST` | `scan-lg-owners` | Scan scope |

## Uninstalling

```sh
rm /var/lib/webosbrew/init.d/10-block-lg-telemetry \
   /var/lib/webosbrew/init.d/20-decline-lg-tos \
   /var/lib/webosbrew/init.d/30-clear-lg-app-update
rm -r /var/lib/webosbrew/lg-lockout
rm /var/luna/preferences/webosbrew_block_updates   # optional: also disables webosbrew's OTA block
reboot
```

The hosts block disappears on reboot. Consent and settings changes persist in the settings service; restore them from the TV's settings menu, or from the `*.bak.<timestamp>` copies left next to each edited file.

## Limitations

- **Firmware-dependent.** The consent and ACR internals are partly reverse-engineered and vary between webOS versions. Always check `verify-lockout` after a reboot on your own firmware.
- **Re-assertion is a workaround.** It wins a one-time boot race. If a service rewrites a setting continuously, identify it with the scanners and target it directly (e.g. with `LG_ACR_SERVICE`, or a systemd drop-in).
- **The ACR owning service isn't configured by default.** Use the scanners to identify it on your TV before setting `LG_ACR_SERVICE`.
- **The local resolver can't be blocked here.** The TV resolves through connman's local proxy, so DNS-level blocking must be done on your router or upstream resolver.
- **Factory resets and firmware updates** can revert everything except router-level blocking.

## Credits

- Wendell's [LG TV block mini how-to](https://forum.level1techs.com/t/lg-tv-block-mini-how-to/255178) on the Level1Techs forum, the origin of the layered approach.
- The [webosbrew](https://github.com/webosbrew) project for rooting and the Homebrew Channel boot hooks.
- [dr0dr1dr2dr3/lgappupdateblocker](https://github.com/dr0dr1dr2dr3/lgappupdateblocker), the basis for the app-update gate.
