#!/usr/bin/env python3
"""
lg-service-scan.py — find which LG webOS services relate to features you want to
disable (ACR, debug-log upload, etc.).

Division of labour (by design):
  * The TV does the MINIMUM: in a SINGLE ssh session it stages the relevant
    service files (systemd unit files, their ExecStart target binaries, the
    /usr/palm/services tree, /tmp/acr.xml) into one tarball under /tmp. It does
    NOT grep or analyse anything.
  * SCP then pulls that one tarball to this machine.
  * ALL keyword parsing / service<->feature mapping happens locally, here.

You will be prompted for the TV's root password twice: once by ssh (staging)
and once by scp (pull). This script never sees or stores the password — ssh/scp
read it straight from your terminal.

Requires Python 3.12+, plus the `ssh` and `scp` clients on PATH.

Examples:
  python3 lg-service-scan.py 192.168.1.50
  python3 lg-service-scan.py 192.168.1.50 --extra-dir /usr/lib --max-mb 12
  python3 lg-service-scan.py --from-archive lg-scan-20260913.tgz   # re-parse, no TV
  python3 lg-service-scan.py 192.168.1.50 --json report.json
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Feature -> keywords. Add your own with --feature name=kw1,kw2 or --keyword kw.
# Keywords are matched as raw bytes, so they also hit string literals embedded
# in ELF daemons and shared libraries.
# ---------------------------------------------------------------------------
DEFAULT_FEATURES: dict[str, list[str]] = {
    "acr": [
        "/mnt/lg/cmn_data/acr", "acr.xml", "ACR_On", "acr_config",
        "eula_allowed", "eula_disallowed", "alphonso", "libalphonso",
    ],
    "debug_log_upload": ["dbgLogUpload"],
    "fault_log_upload": ["faultLogUpload"],
    "voice": ["voiceLongDistance", "voiceAllowed", "voice2Allowed"],
    "ads": ["lgsmartad", "customizedAd", "screenSaverAd", "watchedListCollection"],
}

STAGE_MODIFIERS = "-+!@:"  # systemd ExecStart prefixes to strip


# ---------------------------------------------------------------------------
# Remote staging script (POSIX sh / busybox-safe). Rendered with the stage path,
# extra dirs, and per-file size cap. Everything is copied under <stage>/tree/
# preserving absolute paths, so a staged file's TV path is trivially recovered.
# ---------------------------------------------------------------------------
REMOTE_SCRIPT = r"""
STAGE={stage}
MAXBYTES={maxbytes}
EXTRA_DIRS={extra}
TREE="$STAGE/tree"
mkdir -p "$TREE" 2>/dev/null

copy_small() {{                     # $1 = absolute source path
    [ -f "$1" ] || return 0
    sz=$(wc -c < "$1" 2>/dev/null || echo 0)
    if [ "$sz" -gt "$MAXBYTES" ]; then
        echo "$sz	$1" >> "$STAGE/skipped.tsv"
        return 0
    fi
    d="$TREE$(dirname "$1")"
    mkdir -p "$d" 2>/dev/null
    cp "$1" "$d/" 2>/dev/null || true
}}

echo "[tv] staging into $STAGE" >&2

# 1. unit -> ExecStart manifest (all Exec* lines), via systemctl
: > "$STAGE/unit_exec.tsv"
if command -v systemctl >/dev/null 2>&1; then
    for u in $(systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{{print $1}}'); do
        systemctl cat "$u" 2>/dev/null | sed -n 's/^Exec[A-Za-z]*=//p' | while IFS= read -r ln; do
            printf '%s\t%s\n' "$u" "$ln" >> "$STAGE/unit_exec.tsv"
        done
    done
fi
echo "[tv] unit_exec manifest: $(wc -l < "$STAGE/unit_exec.tsv" 2>/dev/null || echo 0) lines" >&2

# 2. copy unit files themselves (whole unit dirs, small text)
for d in /etc/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
    [ -d "$d" ] || continue
    for f in $(find "$d" -type f 2>/dev/null); do copy_small "$f"; done
done
echo "[tv] unit files staged" >&2

# 3. copy ExecStart argv0 target binaries/scripts (deduped)
cut -f2 "$STAGE/unit_exec.tsv" 2>/dev/null | while IFS= read -r cmd; do
    cmd=${{cmd#[-+!@:]}}; cmd=${{cmd#[-+!@:]}}; cmd=${{cmd#[-+!@:]}}
    set -- $cmd; b=${{1:-}}
    case "$b" in /*) echo "$b" ;; esac
done | sort -u > "$STAGE/execlist.txt"
while IFS= read -r b; do copy_small "$b"; done < "$STAGE/execlist.txt"
echo "[tv] ExecStart targets staged: $(wc -l < "$STAGE/execlist.txt" 2>/dev/null || echo 0) listed" >&2

# 4. palm service definitions (JS/config), excluding node_modules
if [ -d /usr/palm/services ]; then
    for f in $(find /usr/palm/services -type f ! -path '*/node_modules/*' 2>/dev/null); do copy_small "$f"; done
    echo "[tv] palm services staged" >&2
fi

# 5. any extra dirs the user asked for
for d in $EXTRA_DIRS; do
    [ -e "$d" ] || continue
    for f in $(find "$d" -type f ! -path '*/node_modules/*' 2>/dev/null); do copy_small "$f"; done
    echo "[tv] extra dir staged: $d" >&2
done

# 6. runtime ACR artefacts (state, not analysed on the TV)
[ -f /tmp/acr.xml ] && copy_small /tmp/acr.xml
[ -d /mnt/lg/cmn_data/acr ] && ls -laR /mnt/lg/cmn_data/acr > "$STAGE/acr_dir_listing.txt" 2>/dev/null || true

# 7. one tarball, drop the staging dir (leave only the .tgz for scp)
tar -C "$(dirname "$STAGE")" -czf "$STAGE.tgz" "$(basename "$STAGE")" 2>/dev/null
rm -rf "$STAGE" 2>/dev/null
echo "[tv] wrote $STAGE.tgz ($(wc -c < "$STAGE.tgz" 2>/dev/null || echo 0) bytes)" >&2
"""


def build_remote_script(stage: str, extra_dirs: list[str], max_mb: int) -> str:
    extra = " ".join(shlex.quote(d) for d in extra_dirs) if extra_dirs else ""
    return REMOTE_SCRIPT.format(
        stage=shlex.quote(stage),
        maxbytes=max_mb * 1024 * 1024,
        extra=shlex.quote(extra) if extra else "''",
    )


def run(cmd: list[str], *, stdin=None) -> int:
    print(f"\n$ {' '.join(shlex.quote(c) for c in cmd)}", file=sys.stderr)
    return subprocess.run(cmd, stdin=stdin).returncode


def ssh_stage(user: str, ip: str, script_text: str, ssh_opts: list[str]) -> None:
    """Single ssh session: feed the staging script on stdin to a remote `sh`.
    ssh reads the password from the terminal, not from this stdin, so the prompt
    still works."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script_text)
        script_path = fh.name
    try:
        with open(script_path, "rb") as stdin_fh:
            rc = run(["ssh", *ssh_opts, f"{user}@{ip}", "sh", "-s"], stdin=stdin_fh)
        if rc != 0:
            sys.exit(f"ssh staging failed (exit {rc}).")
    finally:
        Path(script_path).unlink(missing_ok=True)


def scp_pull(user: str, ip: str, remote_tgz: str, dest: Path, scp_opts: list[str]) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    local_tgz = dest / Path(remote_tgz).name
    rc = run(["scp", *scp_opts, f"{user}@{ip}:{remote_tgz}", str(local_tgz)])
    if rc != 0:
        sys.exit(f"scp pull failed (exit {rc}).")
    return local_tgz


# ---------------------------------------------------------------------------
# Local parsing
# ---------------------------------------------------------------------------
@dataclass
class Hit:
    tv_path: str
    owners: list[str]
    keywords: list[str] = field(default_factory=list)
    snippets: dict[str, str] = field(default_factory=dict)


def extract_archive(tgz: Path, workdir: Path) -> Path:
    with tarfile.open(tgz, "r:gz") as tf:
        # Python 3.12+: use the data filter to avoid path-traversal surprises
        tf.extractall(workdir, filter="data")
    # the archive contains a single top dir (the stage basename)
    tops = [p for p in workdir.iterdir() if p.is_dir()]
    if len(tops) == 1:
        return tops[0]
    return workdir


def parse_unit_exec(stage_dir: Path) -> dict[str, list[str]]:
    """Return {tv_binary_path: [units...]} from unit_exec.tsv."""
    owner: dict[str, list[str]] = defaultdict(list)
    tsv = stage_dir / "unit_exec.tsv"
    if not tsv.exists():
        return owner
    for line in tsv.read_text(errors="replace").splitlines():
        if "\t" not in line:
            continue
        unit, execval = line.split("\t", 1)
        cmd = execval.strip()
        # strip leading systemd modifiers (- + ! @ :). Guard against empty: an
        # empty ExecStart (e.g. a transient run-*.service) would otherwise loop
        # forever because "" in "-+!@:" is True in Python.
        while cmd and cmd[0] in STAGE_MODIFIERS:
            cmd = cmd[1:]
        parts = cmd.split()
        argv0 = parts[0] if parts else ""
        if argv0.startswith("/"):
            if unit not in owner[argv0]:
                owner[argv0].append(unit)
    return owner


def printable_snippet(data: bytes, idx: int, width: int = 120) -> str:
    """Return the printable-ASCII run around offset idx (strings-like context)."""
    lo = max(0, idx - 4 * width)          # bound the walk so a multi-MB printable
    hi = min(len(data), idx + 4 * width)  # run can't make this crawl
    start = idx
    while start > lo and 32 <= data[start - 1] < 127:
        start -= 1
    end = idx
    while end < hi and 32 <= data[end] < 127:
        end += 1
    run_bytes = data[start:end]
    text = run_bytes.decode("ascii", errors="replace")
    if len(text) > width:
        rel = idx - start
        lo = max(0, rel - width // 2)
        text = text[lo:lo + width]
    return text.strip()


def owner_for(tv_path: str, exec_owner: dict[str, list[str]]) -> list[str]:
    if tv_path in exec_owner:
        return exec_owner[tv_path]
    if "/usr/palm/services/" in tv_path:
        name = tv_path.split("/usr/palm/services/", 1)[1].split("/", 1)[0]
        return [f"(palm service) {name}"]
    if tv_path.endswith(".service"):
        return [f"(unit file) {Path(tv_path).name}"]
    return []


def scan_tree(stage_dir: Path, features: dict[str, list[str]]) -> dict[str, list[Hit]]:
    tree = stage_dir / "tree"
    if not tree.exists():
        sys.exit(f"no tree/ in archive ({stage_dir}); nothing to parse.")
    exec_owner = parse_unit_exec(stage_dir)

    kw_to_features: dict[str, list[str]] = defaultdict(list)
    for feat, kws in features.items():
        for kw in kws:
            kw_to_features[kw].append(feat)
    kw_bytes = {kw: kw.encode() for kw in kw_to_features}

    per_feature: dict[str, list[Hit]] = defaultdict(list)
    files = [p for p in tree.rglob("*") if p.is_file()]
    print(f"[local] scanning {len(files)} staged files for "
          f"{len(kw_bytes)} keywords ...", file=sys.stderr)

    for n, p in enumerate(files, 1):
        if n % 500 == 0:
            print(f"[local]   ... {n}/{len(files)} files scanned", file=sys.stderr)
        try:
            data = p.read_bytes()
        except OSError:
            continue
        tv_path = "/" + str(p.relative_to(tree))
        matched: dict[str, str] = {}
        for kw, kwb in kw_bytes.items():
            idx = data.find(kwb)
            if idx != -1:
                matched[kw] = printable_snippet(data, idx)
        if not matched:
            continue
        owners = owner_for(tv_path, exec_owner)
        # attach this file's hit to every feature any matched keyword belongs to
        feats = {f for kw in matched for f in kw_to_features[kw]}
        for feat in feats:
            fkws = [kw for kw in matched if feat in kw_to_features[kw]]
            per_feature[feat].append(
                Hit(tv_path=tv_path, owners=owners,
                    keywords=sorted(fkws),
                    snippets={kw: matched[kw] for kw in fkws})
            )
    return per_feature


def report(per_feature: dict[str, list[Hit]], stage_dir: Path) -> dict:
    out: dict = {"features": {}}
    print("\n" + "=" * 70)
    print("SERVICE  <->  FEATURE  MAPPING")
    print("=" * 70)

    for feat in sorted(per_feature):
        hits = per_feature[feat]
        # rank services by how many distinct keywords they cover, then file count
        svc_kw: dict[str, set[str]] = defaultdict(set)
        svc_files: Counter = Counter()
        for h in hits:
            for o in (h.owners or ["(no service owner — library/config)"]):
                svc_kw[o].update(h.keywords)
                svc_files[o] += 1
        ranked = sorted(svc_kw, key=lambda s: (len(svc_kw[s]), svc_files[s]), reverse=True)

        print(f"\n### FEATURE: {feat}")
        if not hits:
            print("   (no matches)")
            out["features"][feat] = {"candidates": [], "files": []}
            continue
        print("   candidate owners (ranked):")
        cand_json = []
        for s in ranked:
            kws = ", ".join(sorted(svc_kw[s]))
            print(f"     - {s}")
            print(f"         matched: {kws}   ({svc_files[s]} file(s))")
            cand_json.append({"owner": s, "keywords": sorted(svc_kw[s]),
                              "file_count": svc_files[s]})
        print("   files:")
        files_json = []
        for h in sorted(hits, key=lambda x: x.tv_path):
            own = ", ".join(h.owners) if h.owners else "(no service owner)"
            print(f"     {h.tv_path}   <- {own}")
            for kw in h.keywords:
                print(f"         {kw}: {h.snippets.get(kw,'')!r}")
            files_json.append({"path": h.tv_path, "owners": h.owners,
                               "keywords": h.keywords, "snippets": h.snippets})
        out["features"][feat] = {"candidates": cand_json, "files": files_json}

    # bonus: surface runtime ACR state that was staged
    acr_xml = stage_dir / "tree" / "tmp" / "acr.xml"
    if acr_xml.exists():
        txt = acr_xml.read_text(errors="replace")
        on = "true" in txt.split('ACR_On="', 1)[-1][:6] if 'ACR_On="' in txt else None
        print("\n" + "-" * 70)
        print(f"runtime /tmp/acr.xml present: ACR_On="
              f"{'true' if on else 'false' if on is not None else '?'}")
        out["acr_xml_present"] = True
        out["acr_on"] = on
    listing = stage_dir / "acr_dir_listing.txt"
    if listing.exists():
        print(f"acr data dir listing captured: {listing}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Locate LG webOS services tied to features you want to disable.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("ip", nargs="?", help="TV IP address (omit only with --from-archive)")
    ap.add_argument("--user", default="root", help="ssh user (default: root)")
    ap.add_argument("--max-mb", type=int, default=8,
                    help="skip staging any single file larger than this (default 8)")
    ap.add_argument("--extra-dir", action="append", default=[], metavar="DIR",
                    help="additional TV dir to stage & scan (repeatable), e.g. /usr/lib")
    ap.add_argument("--out", type=Path, default=None,
                    help="local dir for the pulled archive + extraction "
                         "(default: ./lg-scan-<timestamp>)")
    ap.add_argument("--from-archive", type=Path, default=None,
                    help="skip ssh/scp; parse an already-pulled .tgz")
    ap.add_argument("--json", type=Path, default=None, help="also write JSON report here")
    ap.add_argument("--keyword", action="append", default=[], metavar="KW",
                    help="extra keyword to hunt (added to feature 'custom')")
    ap.add_argument("--feature", action="append", default=[], metavar="NAME=kw1,kw2",
                    help="define/override a feature's keyword list")
    ap.add_argument("--ssh-opt", action="append", default=[], metavar="OPT",
                    help="pass-through option for ssh/scp, e.g. --ssh-opt=-oConnectTimeout=10")
    args = ap.parse_args()

    features = {k: list(v) for k, v in DEFAULT_FEATURES.items()}
    if args.keyword:
        features.setdefault("custom", []).extend(args.keyword)
    for spec in args.feature:
        if "=" not in spec:
            ap.error(f"--feature must be NAME=kw1,kw2 (got {spec!r})")
        name, kws = spec.split("=", 1)
        features[name.strip()] = [k.strip() for k in kws.split(",") if k.strip()]

    # ---- obtain the archive ----
    if args.from_archive:
        local_tgz = args.from_archive
        if not local_tgz.exists():
            sys.exit(f"archive not found: {local_tgz}")
        out_dir = args.out or local_tgz.parent / (local_tgz.stem + "-extracted")
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        if not args.ip:
            ap.error("ip is required unless --from-archive is used")
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_dir = args.out or Path(f"./lg-scan-{ts}")
        stage = f"/tmp/lg-scan-{ts}"
        remote_tgz = f"{stage}.tgz"
        print(f"[local] TV={args.ip} user={args.user}  stage={stage}", file=sys.stderr)
        print("[local] === ssh session (you will be asked for the root password) ===",
              file=sys.stderr)
        ssh_stage(args.user, args.ip,
                  build_remote_script(stage, args.extra_dir, args.max_mb),
                  args.ssh_opt)
        print("\n[local] === scp pull (password again) ===", file=sys.stderr)
        local_tgz = scp_pull(args.user, args.ip, remote_tgz, out_dir, args.ssh_opt)
        print(f"[local] pulled {local_tgz}", file=sys.stderr)

    # ---- parse locally ----
    with tempfile.TemporaryDirectory() as td:
        stage_dir = extract_archive(local_tgz, Path(td))
        # copy the manifests out so the user can inspect them next to the archive
        for aux in ("unit_exec.tsv", "execlist.txt", "skipped.tsv", "acr_dir_listing.txt"):
            src = stage_dir / aux
            if src.exists():
                (out_dir / aux).write_bytes(src.read_bytes())
        per_feature = scan_tree(stage_dir, features)
        result = report(per_feature, stage_dir)

    if args.json:
        args.json.write_text(__import__("json").dumps(result, indent=2))
        print(f"\n[local] JSON report written to {args.json}", file=sys.stderr)
    print(f"\n[local] archive + manifests kept in: {out_dir}", file=sys.stderr)
    print("[local] done. Nothing on the TV was modified.", file=sys.stderr)


if __name__ == "__main__":
    main()
