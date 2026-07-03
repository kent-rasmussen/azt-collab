# Move share-diagnostics archives off zip to another format

- **Scope & relationships:** azt-collab/diagnostics. Touches `azt_collabd/server.py::_h_prepare_share_bundle` (archive build + filename, ~L3668–3724) and `azt_collab_client/ui/share.py::share_diagnostics_action` (`mime_type='application/zip'`, L639). Directly reverses part of the 0.52.19 zip decision. Doc surfaces: `azt_collab_client/CLAUDE.md` daemon-owned-state table + hard-rule #9, `CHANGELOG.md`, `CLIENT_INTEGRATION.md` §14b.
- **Vision / done-criteria:** Diagnostic bundle reaches Kent through the **Dome email server** (which strips `.zip`) AND still survives Signal's single-attachment `ACTION_SEND` path. Done when a tester can share diagnostics via Dome email and the attachment arrives intact + openable.
- **Deadline:** 2026-07-01 (today, urgent).

## Notes

- **Driver:** Dome email server strips `.zip` attachments. This is why the format must change.
- **Dual constraint (do not lose the Signal property):** 0.52.19 moved TO a single zip specifically because Signal's `ACTION_SEND_MULTIPLE` resolver runtime-filters URIs to image/video only (verified against `ShareRepository.kt` 2026-06-22). The replacement must stay a **single attachment shipped via `ACTION_SEND`** (which `share_files` picks automatically for a 1-item list). Signal's manifest accepts `application/*` and `*/*`, so any single-attachment MIME clears Signal — the constraint is really email + one-file + openable.
- **KEY UNKNOWN that decides the format:** does Dome strip by **extension** (`.zip`) or by **content sniffing** (zip magic `PK\x03\x04`, which also catches `.docx`/`.xlsx`/`.jar`/`.apk` — all zip containers)? If content-sniffing, any zip-family container is out and we need genuinely different magic bytes (gzip = `1f 8b`). Assume the worse case (sniffing) → prefer a non-zip container.

## Research — format options, ranked by low bug/change likelihood

Change surface is ~5 lines in two files either way; "change likelihood" below = risk the swap introduces a bug or a receiver-open problem.

1. **tar.gz (`.tar.gz` / `application/gzip`)** — RECOMMENDED primary.
   - stdlib `tarfile.open(mode='w:gz')`, drop-in for the `zipfile.ZipFile` block. No new deps (works on Android p4a).
   - gzip magic `1f 8b` ≠ zip family → defeats both extension AND content sniffing.
   - Keeps per-file structure (snapshot .txt + per-day logs) and compression.
   - Bug risk LOW (well-trodden stdlib). Open risk: Kent on Linux opens natively; Android/Windows testers don't need to open it — they only send it.

2. **Plain concatenated `.txt` (`text/plain`, no compression)** — zero-risk fallback.
   - Concatenate snapshot + all logs into ONE .txt with `===== FILE: <name> =====` banners. No archive lib at all → smallest bug surface.
   - Email servers almost never strip `.txt`; Signal accepts `text/*` single.
   - Cost: no compression → ~1 MB of text (snapshot + up to retention×256 KB logs); Dome may have a size cap. Loses nothing for triage (grep one file).

3. **Single gzipped text (`.txt.gz` / `application/gzip`)** — middle option.
   - Concatenate-with-banners then `gzip`. One file to `gunzip`, compressed, non-zip magic. stdlib `gzip`.
   - Slightly more code than #1 (build the concatenation) for the same magic-byte benefit; #1 keeps files separate for free, so #1 usually wins.

4. **Rename zip, keep zip content (`.aztdiag`/`.bin`, `application/octet-stream`)** — NOT recommended.
   - Only defeats extension-based stripping; content sniffing still catches it. Receiver must rename back to open. Fragile UX, MEDIUM open risk.

Deps needing external packages (`7z`, `zstd`) are out — not available under p4a on Android.

**Recommendation:** ship **tar.gz** (#1). If Dome also mangles compressed attachments, fall back to **plain .txt** (#2). Both keep the single-attachment ACTION_SEND / Signal property.

### Exact edit points (for when work starts)
- `server.py:3668` `import zipfile` → `import tarfile`; L3701–3716 `ZipFile(...).writestr/write` → `tarfile.open(archive_path,'w:gz')` with `TarInfo`+`addfile` for the in-memory snapshot and `tf.add(src_path, arcname=...)` for logs.
- `server.py:3670` `archive_name = f'azt_diagnostics_{stamp}.tar.gz'` (filename regex `_SHARE_BUNDLE_FILENAME_RE` already permits `.` so `.tar.gz` passes).
- `share.py:639` `mime_type='application/zip'` → `'application/gzip'`; update the comment above it.
- Bump `MIN_CLIENT_VERSION`? No — wire shape (token + items[uri_path]) is unchanged; only the file's bytes/MIME differ. No peer contract change.
- Docs: CLAUDE.md daemon-owned-state row (prepare_share_bundle line), CHANGELOG, CLIENT_INTEGRATION §14b zip mentions.

## Plans
(pending go-ahead on which format)

## Recorder-side mirror (2026-07-02, recorder 1.58.6)

The recorder does **not** inherit the daemon's tar.gz fix. Its Share
button calls a **recorder-owned** `share_log()` (main.py:9116), not
`share_diagnostics_action`/`prepare_share_bundle` — because it bundles
recorder-private per-day log files the daemon can't see alongside the
daemon logs (`get_daemon_log_files()`). So it built its own
`zipfile.ZipFile` and shipped via the recorder's own FileProvider.
Rebuilding the recorder therefore showed no change (nothing in the
recorder's path had changed).

Fixed by mirroring the format change locally: `zipfile`→`tarfile`
(`w:gz`), `azt_recorder_diagnostics_{stamp}.tar.gz`, intent
`mime_type='application/gzip'`. Daemon-log strings go in via
`TarInfo`+`addfile`; real recorder logs via `tf.add(arcname=...)`.
No client-contract change (peer-owned path). Needs a recorder APK
rebuild + reinstall to reach the device (the 1.58.5 rebuild predated
this edit).

### SUPERSEDED (2026-07-02) — shared helper shipped

Filed a NOTES_TO_DAEMON REFACTOR asking the format be hosted once in the
client. Daemon team shipped `azt_collab_client.diagnostics`
(`build_diagnostics_targz` / `diagnostics_archive_name` /
`DIAGNOSTICS_MIME`) in 0.52.27, and CLIENT_INTEGRATION.md § 14b-iii now
MANDATES peers use it. Recorder 1.58.8 dropped its hand-rolled tar block
and calls the helper; the 1.58.6 mirror is now dead history. The format
is single-sourced across daemon + all peers — the "fixed twice" foot-gun
is closed. Recorder-side collection/staging/dispatch unchanged. Still
needs a recorder APK rebuild to reach the device.
