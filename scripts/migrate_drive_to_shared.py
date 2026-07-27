#!/usr/bin/env python3
"""
Migrate the old (My Drive, eyalz111-owned) CropSight Ops tree into the CropSight
Ops SHARED DRIVE by COPYING (server-side) — because external-owned content cannot
be MOVED into an org shared drive, but a copy makes a fresh ORG-OWNED file.
[2026-07-27 workspace migration P2]

Idempotent / resumable: a file is skipped if a same-name file already exists in
the target folder, so you can re-run after an interruption. Folders are matched by
name (find-or-create), so re-running merges rather than duplicating.

Usage:
    python scripts/migrate_drive_to_shared.py            # DRY RUN: count folders/files/GB
    python scripts/migrate_drive_to_shared.py --apply    # copy everything (resumable)

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


def _ascii(x):
    return (x or "").encode("ascii", "replace").decode("ascii")


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


def _find_child_folder(svc, parent_id, name):
    q = (f"'{parent_id}' in parents and mimeType='{FOLDER}' and trashed=false "
         f"and name={_q(name)}")
    r = svc.files().list(q=q, spaces="drive", supportsAllDrives=True,
                         includeItemsFromAllDrives=True, fields="files(id)").execute().get("files", [])
    return r[0]["id"] if r else None


def _find_child_file(svc, parent_id, name):
    q = (f"'{parent_id}' in parents and mimeType!='{FOLDER}' and trashed=false "
         f"and name={_q(name)}")
    r = svc.files().list(q=q, spaces="drive", supportsAllDrives=True,
                         includeItemsFromAllDrives=True, fields="files(id)").execute().get("files", [])
    return r[0]["id"] if r else None


def _q(name):
    return "'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="copy for real (default = dry run)")
    ap.add_argument("--exclude", default="", help="comma-sep TOP-LEVEL folder names to skip")
    ap.add_argument("--only", default="", help="comma-sep TOP-LEVEL folder names to copy (others skipped)")
    args = ap.parse_args()
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    svc = drive_service.service
    stats = {"folders": 0, "files": 0, "bytes": 0, "copied": 0, "skipped": 0, "gdocs": 0, "errors": 0}

    def walk(src_id, dst_id, path):
        for k in _children(svc, src_id):
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
                    child_dst = _find_child_folder(svc, dst_id, name)
                    if not child_dst:
                        child_dst = svc.files().create(
                            body={"name": name, "mimeType": FOLDER, "parents": [dst_id]},
                            supportsAllDrives=True, fields="id").execute()["id"]
                walk(k["id"], child_dst, path + "/" + name)
            else:
                stats["files"] += 1
                sz = int(k.get("size") or 0)
                stats["bytes"] += sz
                if not k.get("size"):
                    stats["gdocs"] += 1
                if args.apply:
                    if _find_child_file(svc, dst_id, name):
                        stats["skipped"] += 1
                    else:
                        try:
                            svc.files().copy(fileId=k["id"],
                                             body={"name": name, "parents": [dst_id]},
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
        print(f"  copied={stats['copied']} skipped(existing)={stats['skipped']} errors={stats['errors']}")
    else:
        print("  re-run with --apply to copy (server-side, org-owned, resumable).")


if __name__ == "__main__":
    main()
