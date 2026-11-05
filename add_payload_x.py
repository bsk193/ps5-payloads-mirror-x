#!/usr/bin/env python3
"""Fork-aware helper to add a new version row to an existing catalog slot.

Why this exists instead of add_payload.py / update_payloads.py:
  - catalog.save_catalog(payloads) writes v1 and v2 from a single list,
    which collapses v2 (history-aware) to v1's shape (one-per-slot). This
    script writes the two files independently so v2 keeps history while v1
    stays one-per-slot/stable-tip.
  - URLs are bsk193/ps5-payloads-mirror-x, not upstream itsPLK.
  - Carries all v2 extended fields (id, author, details_url, release_notes,
    channel, min_fw, max_fw).
  - Non-interactive; takes CLI args so it is scriptable from CI or a
    workflow_dispatch.

Usage:
  add_payload_x.py bump   --slot <id> --source-repo <owner/repo> --tag <tag> ...
  add_payload_x.py new    --slot <id> --source-repo <owner/repo> --tag <tag> \
                          --name <display> --description <text> \
                          --category <cat> --author <author> ...

Supported asset extensions (picked in this priority order when --asset is
omitted): .elf, .bin, .zip, .exfat, .ffpkg, .ffpfs, .ffpfsc. The non-.elf/.bin
formats are "copy to /data/homebrew" installs (Jobee's Lair pattern). A hard
25 GiB size cap keeps game dumps out of the catalog.

Writes:
  - payloads_v2.json:  append the new row (keep existing history)
  - payloads.json:     replace the slot's single row IFF channel == stable
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
V2_PATH = REPO / "payloads_v2.json"
V1_PATH = REPO / "payloads.json"
MIRROR_REPO = "bsk193/ps5-payloads-mirror-x"
MIRROR_RELEASE_TAG = "payloads-mirror"
MIRROR_BASE_URL = f"https://github.com/{MIRROR_REPO}/releases/download/{MIRROR_RELEASE_TAG}"

# Asset extensions pick_asset recognizes, in priority order. An .elf wins over
# a .zip if both exist. The homebrew formats (.zip through .ffpfsc) install
# by copying into /data/homebrew, matching Jobee's Lair.
ALLOWED_EXTS = (".elf", ".bin", ".zip", ".exfat", ".ffpkg", ".ffpfs", ".ffpfsc")

# Hard size cap. Homebrew sits well under 25 GiB; anything larger is almost
# certainly a game dump sneaking into the catalog, which this mirror does not
# host. Enforced in cmd_bump and cmd_new before download.
MAX_PAYLOAD_SIZE = 25 * 1024 * 1024 * 1024  # 25 GiB

# Fields copied from the newest existing row in the slot when they aren't
# version-specific. A bump reuses these so the author doesn't have to retype
# them; they can still be overridden on a per-call basis by editing the row
# afterwards.
SLOT_LEVEL_FIELDS = ("name", "author", "category", "details_url", "source", "description", "icon_key", "min_fw", "max_fw")


def atomic_write_json(path: Path, value) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def strip_version_prefix(tag: str) -> str:
    if tag.startswith("x-v"):
        return tag[3:]
    if tag.startswith("v"):
        return tag[1:]
    return tag


def gh_release_view(source_repo: str, tag: str) -> dict:
    cmd = ["gh", "release", "view", tag, "-R", source_repo,
           "--json", "name,tagName,publishedAt,isPrerelease,body,assets"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ERROR: gh release view failed for {source_repo} {tag}:\n{r.stderr}")
    return json.loads(r.stdout)


def gh_mirror_upload(local_path: Path, mirror_filename: str) -> None:
    # gh upload uses the local file's basename. Stage under the mirror name.
    staged_dir = Path(tempfile.mkdtemp(prefix="pldmgrx-mirror-"))
    staged = staged_dir / mirror_filename
    staged.write_bytes(local_path.read_bytes())
    cmd = ["gh", "release", "upload", MIRROR_RELEASE_TAG, str(staged),
           "-R", MIRROR_REPO, "--clobber"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    staged.unlink(missing_ok=True)
    staged_dir.rmdir()
    if r.returncode != 0:
        sys.exit(f"ERROR: gh release upload failed:\n{r.stderr}")


def download_asset(url: str, out_path: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "add_payload_x.py"})
    with urllib.request.urlopen(req) as r, open(out_path, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_asset(release: dict, want_name: str | None) -> dict:
    assets = release.get("assets", [])
    if want_name:
        for a in assets:
            if a["name"] == want_name:
                return a
        sys.exit(f"ERROR: asset {want_name!r} not found in release; available: {[a['name'] for a in assets]}")
    # Default: first match in ALLOWED_EXTS priority order (case-insensitive).
    for suffix in ALLOWED_EXTS:
        for a in assets:
            if a["name"].lower().endswith(suffix):
                return a
    sys.exit(f"ERROR: no asset with a supported extension ({', '.join(ALLOWED_EXTS)}); available: {[a['name'] for a in assets]}")


def first_paragraph(body: str | None) -> str:
    if not body:
        return ""
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    for block in body.strip().split("\n\n"):
        block = block.strip()
        if block:
            return block
    return ""


def load_slot_template(v2: dict, slot_id: str) -> dict:
    """Pick the newest existing row in the slot as the template."""
    rows = [p for p in v2["payloads"] if p.get("id") == slot_id]
    if not rows:
        sys.exit(f"ERROR: slot {slot_id!r} not found in v2 catalog. Add new slots via admin.html first.")
    rows.sort(key=lambda e: e.get("last_update", ""), reverse=True)
    return rows[0]


def build_row(args, release: dict, asset: dict, template: dict, checksum: str) -> dict:
    version = args.version or strip_version_prefix(release["tagName"])
    mirror_filename = args.mirror_filename or asset["name"]
    row = {
        "id": args.slot,
        "name": template.get("name", args.slot),
        "filename": mirror_filename,
        "url": f"{MIRROR_BASE_URL}/{mirror_filename}",
        "source": template.get("source", f"https://github.com/{args.source_repo}/releases"),
        "source_direct": asset["url"],
        "author": template.get("author", ""),
        "details_url": template.get("details_url", f"https://github.com/{args.source_repo}"),
        "release_notes": args.release_notes if args.release_notes is not None else first_paragraph(release.get("body")),
        "description": template.get("description", ""),
        "last_update": release["publishedAt"][:10],
        "version": version,
        "category": template.get("category", "Uncategorized"),
        "channel": args.channel,
        "min_fw": template.get("min_fw", ""),
        "max_fw": template.get("max_fw", ""),
        "checksum": checksum,
        "icon_key": template.get("icon_key", ""),
    }
    return row


def sort_v2(payloads: list) -> list:
    return sorted(payloads, key=lambda e: (e["name"].lower(), "0" + str(-1 * int((e.get("last_update") or "0").replace("-", "") or 0))))


def sort_v1(payloads: list) -> list:
    return sorted(payloads, key=lambda e: e.get("last_update", ""), reverse=True)


def cmd_bump(args: argparse.Namespace) -> None:
    v2 = json.loads(V2_PATH.read_text())
    v1 = json.loads(V1_PATH.read_text())

    template = load_slot_template(v2, args.slot)
    release = gh_release_view(args.source_repo, args.tag)
    asset = pick_asset(release, args.asset)
    version = args.version or strip_version_prefix(release["tagName"])

    if asset.get("size", 0) > MAX_PAYLOAD_SIZE:
        sys.exit(f"ERROR: asset {asset['name']!r} is {asset['size']} bytes, exceeds the {MAX_PAYLOAD_SIZE}-byte cap. Refusing to mirror a game-sized artifact.")

    # Idempotency / safety: refuse to replace an existing row silently.
    existing = [p for p in v2["payloads"] if p.get("id") == args.slot and p.get("version") == version]
    if existing and not args.replace:
        sys.exit(f"ERROR: slot {args.slot!r} already has version {version!r}. Use --replace to overwrite.")

    # Fetch + sha
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tf:
        tmp_path = Path(tf.name)
    try:
        print(f"  download: {asset['url']}", file=sys.stderr)
        download_asset(asset["url"], tmp_path)
        actual_size = tmp_path.stat().st_size
        if actual_size != asset["size"]:
            sys.exit(f"ERROR: size mismatch: got {actual_size}, expected {asset['size']}")
        checksum = sha256_of(tmp_path)
        print(f"  size={actual_size} sha256={checksum[:12]}...", file=sys.stderr)

        row = build_row(args, release, asset, template, checksum)

        if args.dry_run:
            print(json.dumps(row, indent=2))
            print(f"\n(dry-run) would update v2 (append), v1 ({'replace slot row' if args.channel == 'stable' else 'no change — experimental'})", file=sys.stderr)
            return

        # Mirror upload (unless skipped).
        if not args.no_upload:
            print(f"  mirror upload: {row['filename']} -> {MIRROR_REPO} tag {MIRROR_RELEASE_TAG}", file=sys.stderr)
            gh_mirror_upload(tmp_path, row["filename"])

    finally:
        tmp_path.unlink(missing_ok=True)

    # v2 write: drop prior same-version if --replace; append; sort.
    v2["payloads"] = [p for p in v2["payloads"] if not (p.get("id") == args.slot and p.get("version") == version)]
    v2["payloads"].append(row)
    v2["payloads"] = sort_v2(v2["payloads"])
    atomic_write_json(V2_PATH, v2)

    # v1 write: only promote a stable row to the one-per-slot tip.
    if args.channel == "stable":
        v1 = [p for p in v1 if p.get("id") != args.slot]
        v1.append({k: v for k, v in row.items() if k != "channel"})
        v1 = sort_v1(v1)
        atomic_write_json(V1_PATH, v1)

    print(f"OK  slot={args.slot}  version={version}  checksum={checksum[:12]}...  channel={args.channel}", file=sys.stderr)
    print(f"    v2 rows in slot now: {len([p for p in v2['payloads'] if p.get('id') == args.slot])}", file=sys.stderr)



def cmd_new(args: argparse.Namespace) -> None:
    v2 = json.loads(V2_PATH.read_text())
    v1 = json.loads(V1_PATH.read_text())

    if any(p.get("id") == args.slot for p in v2["payloads"]):
        sys.exit(f"ERROR: slot {args.slot!r} already exists in v2. Use `bump` to add a new version.")

    release = gh_release_view(args.source_repo, args.tag)
    asset = pick_asset(release, args.asset)
    version = args.version or strip_version_prefix(release["tagName"])

    if asset.get("size", 0) > MAX_PAYLOAD_SIZE:
        sys.exit(f"ERROR: asset {asset['name']!r} is {asset['size']} bytes, exceeds the {MAX_PAYLOAD_SIZE}-byte cap.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tf:
        tmp_path = Path(tf.name)
    try:
        print(f"  download: {asset['url']}", file=sys.stderr)
        download_asset(asset["url"], tmp_path)
        actual_size = tmp_path.stat().st_size
        if actual_size != asset["size"]:
            sys.exit(f"ERROR: size mismatch: got {actual_size}, expected {asset['size']}")
        checksum = sha256_of(tmp_path)
        print(f"  size={actual_size} sha256={checksum[:12]}...", file=sys.stderr)

        mirror_filename = args.mirror_filename or asset["name"]
        row = {
            "id": args.slot,
            "name": args.name,
            "filename": mirror_filename,
            "url": f"{MIRROR_BASE_URL}/{mirror_filename}",
            "source": args.source or f"https://github.com/{args.source_repo}/releases",
            "source_direct": asset["url"],
            "author": args.author,
            "details_url": args.details_url or f"https://github.com/{args.source_repo}",
            "release_notes": args.release_notes if args.release_notes is not None else first_paragraph(release.get("body")),
            "description": args.description,
            "last_update": release["publishedAt"][:10],
            "version": version,
            "category": args.category,
            "channel": args.channel,
            "min_fw": args.min_fw or "",
            "max_fw": args.max_fw or "",
            "checksum": checksum,
            "icon_key": args.icon_key or "",
        }

        if args.dry_run:
            print(json.dumps(row, indent=2))
            print(f"\n(dry-run) would CREATE v2 slot {args.slot!r} (new) and v1 ({'add row' if args.channel == 'stable' else 'no change - experimental'})", file=sys.stderr)
            return

        if not args.no_upload:
            print(f"  mirror upload: {row['filename']} -> {MIRROR_REPO} tag {MIRROR_RELEASE_TAG}", file=sys.stderr)
            gh_mirror_upload(tmp_path, row["filename"])
    finally:
        tmp_path.unlink(missing_ok=True)

    v2["payloads"].append(row)
    v2["payloads"] = sort_v2(v2["payloads"])
    atomic_write_json(V2_PATH, v2)

    if args.channel == "stable":
        v1.append({k: v for k, v in row.items() if k != "channel"})
        v1 = sort_v1(v1)
        atomic_write_json(V1_PATH, v1)

    print(f"OK  new slot={args.slot}  version={version}  checksum={checksum[:12]}...  channel={args.channel}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(prog="add_payload_x.py", description=__doc__.strip().split('\n', 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)

    bump = sub.add_parser("bump", help="Add a new version row to an existing slot")
    bump.add_argument("--slot", required=True, help="slot id (existing in payloads_v2.json)")
    bump.add_argument("--source-repo", required=True, help="owner/repo on github.com of the upstream release")
    bump.add_argument("--tag", required=True, help="tag name of the upstream release")
    bump.add_argument("--asset", help="asset filename to pick from the release (default: first .elf/.bin)")
    bump.add_argument("--channel", choices=("stable", "experimental"), default="stable")
    bump.add_argument("--version", help="override the version string (default: tag with x-v/v prefix stripped)")
    bump.add_argument("--release-notes", help="override release_notes (default: first paragraph of release body)")
    bump.add_argument("--mirror-filename", help="override the mirror-side filename (default: upstream asset name)")
    bump.add_argument("--no-upload", action="store_true", help="skip mirror asset upload (if already there)")
    bump.add_argument("--replace", action="store_true", help="overwrite an existing row for the same (slot, version)")
    bump.add_argument("--dry-run", action="store_true", help="print the row and exit without writing")
    bump.set_defaults(func=cmd_bump)

    new = sub.add_parser("new", help="Create a brand-new catalog slot")
    new.add_argument("--slot", required=True, help="new slot id (must not already exist in v2)")
    new.add_argument("--source-repo", required=True, help="owner/repo on github.com")
    new.add_argument("--tag", required=True, help="tag name of the upstream release")
    new.add_argument("--name", required=True, help="display name")
    new.add_argument("--description", required=True, help="one-line description shown in the UI")
    new.add_argument("--category", required=True, help="catalog category (e.g. Loaders, Utilities & Tools, Multimedia)")
    new.add_argument("--author", required=True, help="author / maintainer handle")
    new.add_argument("--details-url", help="link for the UI 'details' button (default: repo root)")
    new.add_argument("--source", help="source releases URL (default: repo's /releases)")
    new.add_argument("--asset", help="asset filename to pick from the release (default: first allowed extension)")
    new.add_argument("--channel", choices=("stable", "experimental"), default="stable")
    new.add_argument("--version", help="override version string (default: tag with x-v/v prefix stripped)")
    new.add_argument("--release-notes", help="override release_notes (default: first paragraph of release body)")
    new.add_argument("--mirror-filename", help="override the mirror-side filename")
    new.add_argument("--min-fw", help="minimum PS5 firmware (e.g. '10.00')")
    new.add_argument("--max-fw", help="maximum PS5 firmware (e.g. '13.60')")
    new.add_argument("--icon-key", help="content-addressed icon key from icons.json")
    new.add_argument("--no-upload", action="store_true", help="skip mirror asset upload")
    new.add_argument("--dry-run", action="store_true", help="print the row and exit without writing")
    new.set_defaults(func=cmd_new)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
