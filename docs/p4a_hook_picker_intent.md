# p4a_hook.py change: PICK_PROJECT intent-filter on PythonActivity — **SHIPPED**

The PICK_PROJECT intent-filter described in this doc is shipped:
`p4a_hook.py:_inject_pick_project` (around line 291) injects the
filter into the server APK's `PythonActivity` whenever
`dist_name == 'aztcollab'`. The picker resolution + cross-package
launch flow it enables has been the production code path since
v0.28.x.

This stub is kept as a redirect for anyone who lands here from an
older bookmark or git-blame trail. The actual implementation is in
the hook source; verification commands at the bottom of git history
for this file (pre-2026-05-09).
