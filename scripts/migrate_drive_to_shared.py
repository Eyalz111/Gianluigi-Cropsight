#!/usr/bin/env python3
"""
Migrate the old (My Drive, eyalz111-owned) CropSight Ops tree into the CropSight
Ops SHARED DRIVE by COPYING (server-side) — because external-owned content cannot
be MOVED into an org shared drive, but a copy makes a fresh ORG-OWNED file.
[2026-07-27 workspace migration P2]

Idempotent / resumable by SOURCE ID, not by name. Every copy is stamped with an
`appProperties.migrate_src = <source file/folder id>` marker; the "already copied?"
check matches that marker (with a name+size fallback so files copied by the older
name-based version of this script are still recognized and not re-copied).

Why by source-id, not name: Google Drive — unlike a filesystem — permits multiple
DISTINCT files (or folders) with the IDENTICAL name in one parent. A name-only skip
silently dropped the duplicate file / merged the duplicate folder (silent data
loss). Keying on the immutable source id makes each distinct item copy exactly once
and preserves the tree. Duplicate-named siblings are also logged as a loud WARNING
so the condition is never silent. [review #2/#3 2026-07-28]

Usage:
    python scripts/migrate_drive_to_shared.py            # DRY RUN: count folders/files/GB
    python scripts/migrate_drive_to_shared.py --apply    # copy everything (resumable)
    python scripts/migrate_drive_to_shared.py --apply --only "A,B"   # only top-level folders A,B

Source = OLD CropSight Ops root; Target = the CropSight Ops shared drive root.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from services.google_drive import drive_service  # noqa: E402

OLD_ROOT = "1hrKXN9gAvnwEF7xID_T4z33nrTivwhGT"   # My Drive "CropSight Ops"
SHARED_DRIVE = "0AIZNUiJD1vtpUk9PVA"             # "CropSight Ops" shared drive root
FOLDER = "application/vnd.google-apps.folder"
MARKER = "migrate_src"                            # appProperties key = source id


def _ascii(x):
    return (x or "").encode("ascii", "replace").decode("ascii")


def _q(name):
    return "'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _children(svc, fid):
    out, tok = [], None
    while True:
        r = svc.files().list(
            q=f"'{fid}' in parents and trashed=false", spaces="drive",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
            fields="nextPageToken, files(id,name,mimeType,size)",
            pageSize=1000, pageToken=tok).execute()
        out += r.get("files", [])
        tok = r.get("nextPageToken")
        if not tok:
            break
    return out


def _dst_candidates(svc, parent_id, name, is_folder):
    """Same-name items of the matching type already in the destination folder,
    each with its id, size, and appProperties (so we can match by source-id marker)."""
    op = "=" if is_folder else "!="
    q = (f"'{parent_id}' in parents and mimeType{op}'{FOLDER}' and trashed=false "
         f"and name={_q(name)}")
    return svc.files().list(
        q=q, spaces="drive", supportsAllDrives=True, includeItemsFromAllDrives=True,
        fields="files(id,size,appProperties)").execute().get("files", [])


def _marker(cand):
    return (cand.get("appProperties") or {}).get(MARKER)


def _find_dst_folder(svc, parent_id, src):
    """Destination folder previously created FROM this source folder.
    Marker match first; else an unmarked same-name folder (legacy copy). Returns
    id or None (None => caller creates a NEW folder, so two distinct same-name
    source folders map to two distinct destination folders)."""
    cands = _dst_candidates(svc, parent_id, src["name"], is_folder=True)
    for c in cands:
        if _marker(c) == src["id"]:
            return c["id"]
    for c in cands:
        if not _marker(c):        # legacy name-only copy, no marker
            # CLAIM it for this source. Returning it unclaimed meant the NEXT
            # distinct same-name source folder matched the same unmarked folder
            # and both subtrees merged into one — the exact silent merge the
            # source-id rewrite was meant to end. [code-review 2026-08-06]
            try:
                svc.files().update(
                    fileId=c["id"], supportsAllDrives=True,
                    body={"appProperties": {MARKER: src["id"]}},
                    fields="id",
                ).execute()
            except Exception as e:
                print(f"  [WARN] could not claim legacy folder {_ascii(src['name'])}: {e}",
                      flush=True)
            return c["id"]
    return None


def _already_copied_file(svc, parent_id, src):
    """True if this exact SOURCE file was already copied here. Marker match first;
    else an unmarked same-name file whose size matches (legacy copy). A distinct
    same-name file with different content (different size) is NOT matched -> copied."""
    src_size = src.get("size")
    for c in _dst_candidates(svc, parent_id, src["name"], is_folder=False):
        if _marker(c) == src["id"]:
            return True
        if not _marker(c):
            c_size = c.get("size")
            if (src_size is not None and c_size == src_size) or \
               (src_size is None and c_size is None):
                return True
    return False


def _warn_duplicate_siblings(kids, path, stats):
    """Loudly flag duplicate-named siblings so the condition is never silent."""
    seen = {}
    for k in kids:
        key = (k["mimeType"] == FOLDER, k["name"])
        seen.setdefault(key, []).append(k)
    for (is_folder, name), items in seen.items():
        if len(items) > 1:
            stats["dup_siblings"] += 1
            kind = "folder" if is_folder else "file"
            print(f"  [DUP] {len(items)} duplicate-named {kind}s in {_ascii(path) or '/'}: "
                  f"{_ascii(name)} - copied as distinct items (kept by source id)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="copy for real (default = dry run)")
    ap.add_argument("--exclude", default="", help="comma-sep TOP-LEVEL folder names to skip")
    ap.add_argument("--only", default="", help="comma-sep TOP-LEVEL folder names to copy (others skipped)")
    args = ap.parse_args()
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    svc = drive_service.service
    stats = {"folders": 0, "files": 0, "bytes": 0, "copied": 0, "skipped": 0,
             "gdocs": 0, "errors": 0, "dup_siblings": 0}

    def walk(src_id, dst_id, path):
        kids = _children(svc, src_id)
        # Runs in DRY RUN too: the dry run is the operator's review step, so
        # hiding duplicates until --apply disclosed them only after tens of GB
        # had already started copying. [code-review 2026-08-06]
        _warn_duplicate_siblings(kids, path, stats)
        for k in kids:
            name = k["name"]
            # top-level include/exclude filters (only applied at the root)
            if src_id == OLD_ROOT:
                if exclude and name in exclude:
                    print(f"  SKIP (excluded): {_ascii(name)}", flush=True); continue
                if only and name not in only:
                    continue
            if k["mimeType"] == FOLDER:
                stats["folders"] += 1
                child_dst = None
                if args.apply:  # only touch the target when actually copying
                    child_dst = _find_dst_folder(svc, dst_id, k)
                    if not child_dst:
                        child_dst = svc.files().create(
                            body={"name": name, "mimeType": FOLDER, "parents": [dst_id],
                                  "appProperties": {MARKER: k["id"]}},
                            supportsAllDrives=True, fields="id").execute()["id"]
                walk(k["id"], child_dst, path + "/" + name)
            else:
                stats["files"] += 1
                sz = int(k.get("size") or 0)
                stats["bytes"] += sz
                if not k.get("size"):
                    stats["gdocs"] += 1
                if args.apply:
                    if _already_copied_file(svc, dst_id, k):
                        stats["skipped"] += 1
                    else:
                        try:
                            svc.files().copy(fileId=k["id"],
                                             body={"name": name, "parents": [dst_id],
                                                   "appProperties": {MARKER: k["id"]}},
                                             supportsAllDrives=True, fields="id").execute()
                            stats["copied"] += 1
                        except Exception as e:
                            stats["errors"] += 1
                            print(f"  ERR copy {_ascii(name)[:40]}: {str(e)[:80]}", flush=True)
                if (stats["files"] % 50) == 0:
                    print(f"  ...{stats['files']} files seen "
                          f"(copied={stats['copied']} skipped={stats['skipped']}) "
                          f"{stats['bytes']/1e9:.2f} GB", flush=True)

    mode = "APPLY (copying)" if args.apply else "DRY RUN (counting)"
    print(f"=== migrate_drive_to_shared.py [{mode}] ===", flush=True)
    walk(OLD_ROOT, SHARED_DRIVE if args.apply else "DRYRUN", "")
    print(f"\nDONE. folders={stats['folders']} files={stats['files']} "
          f"({stats['bytes']/1e9:.2f} GB binary + {stats['gdocs']} native Google files)")
    if args.apply:
        print(f"  copied={stats['copied']} skipped(existing)={stats['skipped']} "
              f"errors={stats['errors']} duplicate-name-groups={stats['dup_siblings']}")
    else:
        print("  re-run with --apply to copy (server-side, org-owned, resumable by source id).")


if __name__ == "__main__":
    main()
