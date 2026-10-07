# Changelog

High-level summary of changes from the original scripts (`10-block-lg-telemetry`, `decline-tos.sh`, `clear-app-update-gate.sh`, `install.sh`, `verify-lockout.sh`) to the current version.

## Summary

The original scripts were a one-time, manually run toolkit: only the hosts block ran at boot, consent was declined by editing files with Python, and several settings silently reverted on reboot. The current version runs entirely from `init.d` on every boot, writes settings through LG's settings service so they persist, defeats services that re-enable settings after boot, needs no Python on the TV, and adds tooling to verify the result and diagnose what overrides it.

## Fixed

- **Hosts blocklist never applied when webosbrew's blocker was enabled.** The original decided whether it had already installed by checking whether `/etc/hosts` was mounted. webosbrew mounts `/etc/hosts` first, so the check always passed and the script only refreshed a file that was never mounted. It now looks for its own marker line inside `/etc/hosts` and stacks its bind mount on top of webosbrew's, preserving webosbrew's entries.
- **Consent and settings reverted on reboot.** `com.webos.settingsservice` owns these values and overwrites the flat files under `/var/luna/preferences/`. Settings are now written through the service with `luna-send`, so they persist.
- **`voiceAllowed`, `voice2Allowed`, and `usageCare` were never set.** The original only changed keys that already existed in the `option` file, and these were absent. They are now always set.
- **Consent declining failed on TVs with a stripped Python.** The original required a Python with the `json` module and refused to run without one. The scripts are now pure POSIX shell.
- **Verifier missed blocked domains.** The hosts check required each domain to end a line, so it failed on webosbrew's multi-domain lines. It now matches any sinkhole line containing the domain.
- **Verifier reported stale consent state.** It read the flat files, which may not match what the TV is actually using. It now queries the live settings service, falling back to the files.
- **Installer's dry run required root.** Root is now only required for `--apply`.

## Added

- **Every layer now runs at boot.** Consent/settings and the app-update gate are now `init.d` hooks alongside the hosts block, instead of scripts you had to run by hand.
- **ACR runtime neutralization.** `/tmp/acr.xml`, the live ACR switch, is rewritten so `ACR_On`, `capture`, and `send_data` are all `false`. An optional `LG_ACR_SERVICE` setting restarts the ACR-owning service afterwards.
- **Delayed re-assertion.** After the boot pass, settings and `acr.xml` are re-applied at +30, +90, and +180 seconds to beat LG services (such as the one re-enabling `dbgLogUpload`) that undo changes after the hooks run.
- **`voiceLongDistance=off`** is now enforced in the `general` settings.
- **File-edit fallback.** If `luna-send` or the settings service is unavailable, settings are applied by editing the files directly with `sed`, adding any missing keys.
- **Boot log.** All hooks append to `/var/lib/webosbrew/lg-lockout.log`, so you can confirm they ran at boot.
- **Expanded verification.** New checks for every enforced `option`/`general` key, `acr.xml` state, and a final section showing the boot log.
- **`scan-lg-owners`**: on-TV diagnostic that identifies which services, binaries, and processes reference or hold a given setting or path.
- **`lg-service-scan.py`**: workstation diagnostic (Python 3.12+) that collects the TV's service files over a single SSH session and SCP pull, then maps services to features locally.
- **Test overrides.** Environment variables (`LG_LOCKOUT_ROOT`, `LG_LUNA_SEND`, `LG_LOG`, and others) let every script run against a staged copy of the filesystem.
- **`README.md`** documenting the project.

## Changed

- **Script names.** `decline-tos.sh` → `20-decline-lg-tos`, `clear-app-update-gate.sh` → `30-clear-lg-app-update`, `verify-lockout.sh` → `verify-lockout`. Hooks drop the `.sh` extension and use numeric prefixes because `run-parts` only runs dot-free, executable files, in lexical order.
- **Install layout.** All three hooks go to `/var/lib/webosbrew/init.d/`; only the manual tools (`verify-lockout`, `scan-lg-owners`) go to `/var/lib/webosbrew/lg-lockout/`.
- **ACR is verified through `acr.xml`.** The `eula_disallowed_rebooted` marker is still written, but the verifier treats it as informational, since not all firmware creates or uses it.
- **Hosts file rebuild.** The blocklist is rebuilt from the current effective `/etc/hosts` each time rather than from a pristine copy saved in `/tmp`, and both `mount --bind` and `mount -o bind` are tried.

## Removed

- **Python dependency** on the TV.
- **Hard-coded `acr2` restart.** Replaced by the opt-in `LG_ACR_SERVICE` setting, since the ACR service name varies by firmware.
- **`HOSTS_PRISTINE` copy** in `/tmp`, no longer needed with the marker-based rebuild.
- **Two commented-out optional domains** (`lgvoice.com`, `lgshopsvc.lgappstv.com`). The active blocklist of 42 domains is unchanged.
