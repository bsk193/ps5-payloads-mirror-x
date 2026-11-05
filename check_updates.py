#!/usr/bin/env python3
"""Scan the catalog for slots whose upstream has a newer release.

Walks every slot in payloads_v2.json, resolves its `source` URL to a
GitHub repo, queries that repo's Latest release, and compares the tag to
the newest row we carry. Reports findings by default; with --apply, calls
add_payload_x.py bump for each slot that has an update.

Slots whose source URL isn't a GitHub release feed (gitea, file links,
unofficial builds that have no stable upstream) are skipped with a
reason; they still need manual attention via admin.html or a one-off
add_payload_x.py bump.

Usage:
  check_updates.py [--slot <id>]        # limit scan to one slot
                   [--apply]            # run add_payload_x.py bump for each update
                   [--yes]              # with --apply, skip confirmation prompts
                   [--include-prerelease]  # treat upstream prereleases as candidates
                                           # (default: stable Latest only)

Exit code 0 unless --apply was asked and at least one bump failed.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
V2_PATH = REPO / "payloads_v2.json"
ADD_PAYLOAD_X = REPO / "add_payload_x.py"

# Slots whose automated upstream check is unreliable or out of scope. The
# scanner does not try to bump these — they stay catalog entries that a
# human updates via admin.html or a direct add_payload_x.py call.
SKIP_SLOTS = {
    # etaHEN is mirrored from a dedicated repo; the scanner would race the
    # mirror. Bumps happen when a human adds a new release to the mirror.
    "etahen": "mirrored via bsk193/etaHEN-mirror; bump manually when mirror gains a release",
    # Jobee's Lair is a .exfat homebrew install, not a release-asset flow;
    # the catalog row is maintained by hand.
    "jobeeslair": ".exfat homebrew path; not a standard release bump",
    # pegasusdl switched from a .elf payload to a native-pkg artifact
    # (native-release.json + .sig + PPSA99998.ffpfsc + SHA256SUMS) at
    # native-v2.x. The catalog row only models .elf/.bin payloads, so the
    # scanner can't bump this slot automatically any more. A human decides
    # whether to carry the native pkg via a different distribution path,
    # or pin pegasusdl to its last .elf release (v1.7.0).
    "pegasusdl": "upstream v2.x changed format to native pkg (.ffpfsc); no .elf asset",
}

# Match github.com HTTP URLs: https://github.com/<owner>/<repo>[/...]
GITHUB_RE = re.compile(r"^https?://github\.com/([^/]+)/([^/?#]+)")


def gh_json(args: list[str]) -> dict | list | None:
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def github_repo_from_source(source: str) -> tuple[str, str] | None:
    m = GITHUB_RE.match(source or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    # Strip common suffixes if the URL ended at /releases etc. already caught by [^/]+
    return owner, repo.removesuffix(".git")


def _normalize(api_dict: dict) -> dict:
    """Map GitHub REST field names to the internal shape the rest of this script uses."""
    return {
        "tagName": api_dict["tag_name"],
        "publishedAt": api_dict["published_at"],
        "isPrerelease": api_dict.get("prerelease", False),
        "assets": api_dict.get("assets", []),
    }


def _is_api_error(data) -> bool:
    return isinstance(data, dict) and "message" in data and "tag_name" not in data


def get_latest_release(owner: str, repo: str, include_prerelease: bool) -> dict | None:
    """Return the release metadata dict, or None if nothing qualifies."""
    if not include_prerelease:
        # /releases/latest is the one GitHub flags as Latest; it excludes
        # drafts and pre-releases server-side.
        data = gh_json(["gh", "api", f"repos/{owner}/{repo}/releases/latest"])
        if not data or _is_api_error(data):
            return None
        return _normalize(data)
    # Otherwise walk the top of the release list and pick the newest
    # non-draft (pre-releases included).
    data = gh_json(["gh", "api", f"repos/{owner}/{repo}/releases?per_page=20"])
    if not data or not isinstance(data, list):
        return None
    rels = [r for r in data if not r.get("draft")]
    if not rels:
        return None
    rels.sort(key=lambda r: r.get("published_at", ""), reverse=True)
    return _normalize(rels[0])


def newest_slot_row(v2: dict, slot_id: str) -> dict | None:
    rows = [p for p in v2["payloads"] if p.get("id") == slot_id]
    if not rows:
        return None
    rows.sort(key=lambda e: e.get("last_update", ""), reverse=True)
    return rows[0]


def slot_carries_tag(v2: dict, slot_id: str, tag: str) -> bool:
    """Check whether the upstream tag or its version-stripped form is already a row."""
    bare = tag
    if tag.startswith("x-v"):
        bare = tag[3:]
    elif tag.startswith("v"):
        bare = tag[1:]
    for p in v2["payloads"]:
        if p.get("id") != slot_id:
            continue
        if p.get("version") in (tag, bare):
            return True
    return False


def scan_slot(v2: dict, slot_id: str, include_prerelease: bool) -> dict:
    """Return a result dict with keys: status, slot, reason, latest_tag?, current_version?, source_repo?"""
    newest = newest_slot_row(v2, slot_id)
    if not newest:
        return {"status": "error", "slot": slot_id, "reason": "no rows in catalog"}

    if slot_id in SKIP_SLOTS:
        return {"status": "skip", "slot": slot_id, "reason": SKIP_SLOTS[slot_id],
                "current_version": newest.get("version")}

    gh = github_repo_from_source(newest.get("source", ""))
    if not gh:
        return {"status": "skip", "slot": slot_id,
                "reason": f"source is not a github.com URL: {newest.get('source')!r}",
                "current_version": newest.get("version")}

    owner, repo = gh
    rel = get_latest_release(owner, repo, include_prerelease)
    if not rel:
        return {"status": "skip", "slot": slot_id,
                "reason": f"no qualifying release at {owner}/{repo}",
                "current_version": newest.get("version"), "source_repo": f"{owner}/{repo}"}

    tag = rel["tagName"]
    if slot_carries_tag(v2, slot_id, tag):
        return {"status": "ok", "slot": slot_id,
                "current_version": newest.get("version"), "latest_tag": tag,
                "source_repo": f"{owner}/{repo}"}
    return {"status": "update", "slot": slot_id,
            "current_version": newest.get("version"), "latest_tag": tag,
            "is_prerelease": rel.get("isPrerelease", False),
            "source_repo": f"{owner}/{repo}"}


def apply_bump(result: dict, yes: bool) -> bool:
    slot = result["slot"]
    tag = result["latest_tag"]
    repo = result["source_repo"]
    channel = "experimental" if result["is_prerelease"] else "stable"
    cmd = ["python3", "-I", str(ADD_PAYLOAD_X), "bump",
           "--slot", slot, "--source-repo", repo, "--tag", tag, "--channel", channel]
    print(f"\n--> {' '.join(cmd)}", file=sys.stderr)
    if not yes:
        reply = input(f"Apply bump? [y/N] ").strip().lower()
        if reply != "y":
            print("    skipped", file=sys.stderr)
            return False
    r = subprocess.run(cmd)
    return r.returncode == 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.strip().split('\n', 1)[0])
    p.add_argument("--slot", help="check only this slot id")
    p.add_argument("--apply", action="store_true", help="run add_payload_x.py bump for each update")
    p.add_argument("--yes", action="store_true", help="with --apply, skip the confirmation prompt")
    p.add_argument("--include-prerelease", action="store_true",
                   help="treat upstream prereleases as candidates (default: stable only)")
    args = p.parse_args()

    v2 = json.loads(V2_PATH.read_text())
    slot_ids = sorted({p["id"] for p in v2["payloads"] if "id" in p})
    if args.slot:
        if args.slot not in slot_ids:
            sys.exit(f"ERROR: slot {args.slot!r} not in catalog")
        slot_ids = [args.slot]

    print(f"scanning {len(slot_ids)} slot(s) ...", file=sys.stderr)
    updates: list[dict] = []
    for sid in slot_ids:
        r = scan_slot(v2, sid, args.include_prerelease)
        status = r["status"]
        cur = r.get("current_version", "?")
        if status == "update":
            label = " (prerelease)" if r.get("is_prerelease") else ""
            print(f"  UPDATE  {sid:30s}  catalog={cur!s:16s}  ->  upstream={r['latest_tag']}{label}  via {r['source_repo']}")
            updates.append(r)
        elif status == "ok":
            print(f"  ok      {sid:30s}  up to date ({cur})")
        elif status == "skip":
            print(f"  skip    {sid:30s}  {r['reason']}")
        elif status == "error":
            print(f"  ERROR   {sid:30s}  {r['reason']}")

    print(f"\nsummary: {len(updates)} update(s), {len(slot_ids) - len(updates)} up-to-date-or-skipped", file=sys.stderr)

    if not args.apply or not updates:
        return

    fails = 0
    for r in updates:
        if not apply_bump(r, args.yes):
            fails += 1
    print(f"\napplied: {len(updates) - fails} ok, {fails} failed", file=sys.stderr)
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
