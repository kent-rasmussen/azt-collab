# Changelog

Two packages live here. Versions move together for now (the client
embeds `MIN_SERVER_VERSION`, so when the wire format changes we bump
both); patch-level bumps in one without the other are fine.

- **azt_collabd** — server / daemon. Source of truth: `azt_collabd.__version__` (re-imported by `server.py` as `_VERSION` for the wire response).
- **azt_collab_client** — client library. Source of truth: `azt_collab_client.__version__`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely.

## [Unreleased]

### azt_collabd 0.43.0 / azt_collab_client 0.43.0 — Split commit and push; daemon-driven push policy; ``sync.work_offline`` toggle

Closes the NOTES_TO_DAEMON.md item filed by azt-recorder
1.43.1 (2026-05-15): debounced ``request_sync`` skipped the
commit step entirely while offline, so a field-session of
swipes piled up dirty files with ``commits_ahead=0,
n_changes=N`` rather than the per-swipe commits a user
expects. Synchronous ``sync_project`` (Sync button) committed
fine under the same offline conditions, proving the commit
step itself wasn't network-gated — only the debounced
pipeline was misordered.

Rather than patch the early-return inside ``_run_sync``, the
whole commit/push relationship is rethought: peers decide
where to cut a commit, the daemon decides when (and whether)
to push.

- **``commit_project(langcode)`` replaces ``request_sync``**
  (client + daemon). Same debounce / async / job_id /
  poll_job machinery — narrower contract. The RPC is now
  commit-only: it stages, commits, marks ``pending_push``,
  and returns. No fetch, no merge, no push. The old
  ``request_sync`` name kept as a backwards-compat alias in
  the client; the old ``/v1/projects/<lang>/sync_async``
  URL routes to the new ``commit`` handler on the server.
  Old peer code keeps working — the only behavioural change
  is the result no longer carries ``PUSHED``. Migrate
  result-handling that polls for ``PUSHED`` over to the
  scheduler-driven model.
- **Push moves entirely to the scheduler's drain loop**
  (``azt_collabd/scheduler.py``). The connectivity watcher
  tracks ``_online_since`` on offline→online edges and only
  fires the drain once ``now - _online_since >=
  settings.post_online_grace_s`` (default 60 s). Brief
  tethers the user enabled for something else don't burn
  their MB on pending pushes. The drain also respects the
  ``sync.work_offline`` master toggle.
- **``sync.work_offline`` toggle** —
  ``GET/POST /v1/config/work_offline``, persisted to
  ``$AZT_HOME/config.json``. When on, the watcher drain is
  a no-op and the user-gestured Sync button (``sync_project``)
  returns ``S.WORK_OFFLINE_ENABLED`` without attempting
  any push. Commits via ``commit_project`` are unaffected;
  only push is suppressed. Toggling OFF fires an immediate
  drain so the user doesn't wait a full
  ``connectivity_poll_s`` tick.
- **``S.WORK_OFFLINE_ENABLED`` status code** (mirrored
  daemon + client). Peers route the same way they handle
  ``AUTH_REQUIRED``: toast + ``open_server_ui()`` to the
  daemon settings screen anchored on the toggle.
- **Daemon settings UI**: new "Work offline" section with
  yes/no buttons, in ``azt_collabd/ui/app.py`` (above
  Diagnostic log). State refreshes on screen entry; toggling
  OFF fires the immediate drain server-side.
- **``ProjectStatus.work_offline``** carries the
  daemon-wide bool on every ``project_status`` response so
  peers can render a badge alongside ``commits_ahead`` —
  "5 commits waiting · offline mode" — without a second
  RPC.
- **``repo.py`` factored** into ``commit_repo``
  (stage + commit, no network) and ``push_repo`` (fetch +
  merge + push, no commit). ``sync_repo`` kept as the
  combined entry point for the user-gestured Sync button and
  legacy ``commit_audio_and_sync``; internally it now calls
  ``_commit_step_locked`` then ``_push_step_locked`` under
  one project lock.
- **``scheduler._drain_stuck_commits`` is now commit-only**
  (calls ``commit_repo`` instead of ``sync_repo``). Push for
  recovered commits happens via the regular drain pass.
- **``scheduler.is_online_cached()``** exposes the watcher's
  most recent observation as a module-level bool read —
  callers that don't need a fresh 3–6 s TCP probe should
  use this instead of ``net._has_internet``. Internal
  caller-only for now; not on the wire.
- **MIN_CLIENT_VERSION / MIN_SERVER_VERSION** lock-stepped
  at 0.43.0. Hard requirement: a 0.43 peer against a pre-
  0.43 daemon would still lose offline commits (the bug
  this release fixes); a pre-0.43 peer against a 0.43
  daemon would never observe ``PUSHED`` codes from
  ``commit_project`` and could mis-render its sync state.

### azt_collabd 0.42.0 / azt_collab_client 0.42.0 — Package-replacement: receiver reaps in-APK; drop peer KILL_BACKGROUND_PROCESSES; installed-vs-running reboot prompt

Follow-up to 0.41.28's suite-wide ``SuiteSelfReplaceReceiver``.
The earlier design paired a manifest receiver in every APK with a
peer-side ``killBackgroundProcesses(<server_pkg>)`` backstop on the
Check-again paths to handle OEMs that don't auto-kill the old
process during a package replace. The backstop required peers to
declare ``KILL_BACKGROUND_PROCESSES`` in their own
``android.permissions``, which is one more permission to explain
if/when the suite ever goes through a store review.

The new design moves the reap into the receiver itself: the
freshly-installed APK's receiver calls
``killBackgroundProcesses(getPackageName())`` (its own package's
old-code processes) before the self-kill. No cross-package kill;
no peer permission needed. A small peer-side reboot prompt
covers the migration window for users whose currently-installed
daemon is pre-0.42 (no in-receiver reap).

Minor bump because the receiver behaviour change is observable
across the suite and the peer permission surface area shrinks —
worth lock-stepping daemon + client at 0.42.0 to make the
"upgrade past this line" point unambiguous.

- **``SuiteSelfReplaceReceiver``** now calls
  ``ActivityManager.killBackgroundProcesses(getPackageName())``
  before ``Process.killProcess(myPid())``. ``SecurityException``
  is caught and logged — if the permission injection were to fail
  for any reason, the receiver still self-kills (i.e. degrades to
  the 0.41.28 behaviour on that APK).
- **``p4a_hook.py:_inject_self_replace_receiver``**: the
  ``<receiver>`` block stays on every suite APK; the
  ``<uses-permission android:name=
  "android.permission.KILL_BACKGROUND_PROCESSES" />`` element
  is gated on ``dist_name == 'aztcollab'`` so only the server
  APK ends up with the permission in its merged manifest. Peers
  compile the same Java receiver class but their manifest
  declares only the ``<receiver>``; at runtime the receiver's
  reap call hits ``SecurityException``, is caught, and it falls
  through to its self-kill — same net behaviour as the 0.41.28
  peer receiver, with no peer permission to explain. Anchor
  fixed to ``<application `` (with trailing space) so the
  injection doesn't land inside the explanatory comment in
  ``server_apk/manifest_extras.xml`` (whose prose mentions the
  literal ``<application>``). Idempotent via the
  ``self-replace-permission-injection`` sentinel.
  Reported by azt_recorder 1.42.29 via NOTES_TO_DAEMON.md.
- **Peer-side backstop removed.**
  ``azt_collab_client.ui.bootstrap._kill_server_background`` is
  deleted. The Check-again paths simply invalidate the release
  cache and re-enter ``_check_server`` — the next bind picks up
  the new code from the freshly-installed APK whose receiver did
  the reap during install.
- **Installed-on-disk vs. running detection.**
  ``azt_collab_client.ui.bootstrap._installed_server_version``
  reads the server APK's ``versionName`` via
  ``PackageManager.getPackageInfo``. ``_prompt_server_update``
  compares that to the version /v1/health reports; if installed
  > running, the user has sideloaded the new APK but Android
  kept the old daemon process alive (the pre-0.42 case where
  the receiver doesn't auto-reap). Instead of asking the user
  to re-download, ``_prompt_server_reboot_to_apply`` surfaces a
  "You have {installed} installed; running process is {running}.
  Restart your device to switch to the newer version." popup
  (Check again + Quit + maintainer mailto). Pure transition
  helper — once every field daemon is at 0.42 or newer the
  receiver's in-APK reap fires and the comparison should never
  trigger.
- **§ 2 (peer permissions)** updated: ``KILL_BACKGROUND_PROCESSES``
  no longer listed. The new note explicitly tells peer maintainers
  not to add it themselves.
- **§ 19 (package-replacement contract)** rewritten: the
  two-step receiver contract (reap then self-kill), the
  permission injection model, and the "peers MUST NOT add this
  permission" rule replace the prior peer-backstop section. The
  rollout-window note covers what happens for users still on
  pre-0.42 server APKs — peer surfaces the reboot prompt
  automatically.

No wire-format change. ``MIN_SERVER_VERSION`` / ``MIN_CLIENT_VERSION``
unchanged — the new behaviour is internal to the Java receiver,
the build-time manifest injection, and the peer-side bootstrap
flow.

### azt_collabd 0.41.29 / azt_collab_client 0.41.27 — Atomic-write orphan auto-recovery

Background. The ``atomic_open_write`` protocol is two-phase: peer
streams full LIFT bytes into ``<working_dir>/.azt_atomic_pending/
<token>``, then a separate ``atomic_finalize`` RPC renames the
scratch over the real LIFT. A crash, daemon kill, or transport
break between the two phases leaves the scratch on disk —
complete, well-formed LIFT, but never landed. The two orphans
field-reported in this session were exactly that: complete LIFT
files, sitting in ``.azt_atomic_pending/``, never finalized.

- **New module** ``azt_collabd/atomic_recovery.py``. Scans each
  registered project's ``.azt_atomic_pending/`` directory for
  orphan files ≥ 60 s old (skip in-flight Phase-1 writes) and
  classifies each:

  - Hash-equal to current LIFT → delete (confirmable garbage).
  - All shared guids byte-identical in canonical XML AND no
    orphan-only entries → delete (subset; no new info).
  - Otherwise → run ``lift_merge.three_way_merge(base=b'',
    ours=current, theirs=orphan)``. Write merged bytes
    atomically to the LIFT path, commit as ``"Recovered orphan
    from <iso-timestamp>"`` (author + committer = suite bot,
    same identity used for cross-peer merges). Conflicts get
    the existing ``<annotation name="azt-lift-conflict">``
    treatment — peers / viewers that already surface those
    annotations see recovery conflicts without any new code.
  - Merge raises (corrupt XML, broken byte stream from an
    interrupted Phase-1 write that *looked* > 60 s old) or
    any of lift_merge's guard kinds fire (parse-error,
    truncation-suspected, catastrophic-output) → move the
    orphan to ``.azt_atomic_orphans/unmergeable/<token>.lift``
    for manual inspection.

- **Scheduler integration.** ``scheduler._drain_atomic_orphans``
  runs every watcher tick (default 30 s) alongside the existing
  stuck-commit drain. Cheap when nothing is pending (single
  ``os.listdir`` on a typically-empty directory). Each
  non-trivial outcome logs to the daemon log.

- **ProjectStatus diagnostic.** New field
  ``n_recovered_today: int`` on the project_status response and
  the client-side ``ProjectStatus`` dataclass — purely
  informational, zero on healthy projects, positive when
  Phase-1-only writes were merged back in. Resets at the day
  boundary via ``last_recovery_day`` in projects.json.

- **No user-facing prompt.** In a no-delete-of-LIFT-entries world
  the merge is unambiguously lossless (orphan only ever has
  guids that current also has, plus potentially new field
  content); a "Merge or Discard?" prompt would ask users a
  question most aren't competent to answer, and the safe answer
  ("merge") is the only reasonable default anyway. Conflicts
  flow through the existing annotation channel.

- Versions: daemon 0.41.29 / client 0.41.27. Additive on the
  wire (new ProjectStatus field; pre-0.41.27 clients ignore
  unknown keys; pre-0.41.29 daemons emit nothing for it). No
  MIN floor bumps needed.

### azt_collabd 0.41.28 / azt_collab_client 0.41.26 — Suite-wide package-replacement handling: APK install now reaches the running process

Symptom this closes: a user sideloads the required server APK in
response to the peer's ``client_too_old`` prompt, relaunches the
peer, and still hits the same "AZT collab x.y.z or newer is
required" popup. The new APK is on disk; the OLD process is
still serving the provider with the OLD version. "Wait for an
update" is the wrong instruction — the update is right there.

- **Suite-wide ``MY_PACKAGE_REPLACED`` receiver.** New Java class
  ``org.atoznback.aztcollab.SuiteSelfReplaceReceiver`` at
  ``android/src/main/java/...`` handles the broadcast by
  self-killing the receiving process. ``p4a_hook.py`` grows
  ``_inject_self_replace_receiver`` to inject the manifest
  ``<receiver>`` into every APK in the suite (NOT gated on
  ``dist_name`` — server + every peer get it). Manifest receiver,
  NOT runtime: some Android versions / OEMs kill the old process
  as part of the replace, so a runtime-registered receiver
  wouldn't be alive to receive the broadcast; manifest receivers
  cold-start the new APK's code to deliver, which is exactly
  what we want.
- **Peer-side backstop.**
  ``azt_collab_client.ui.bootstrap._kill_server_background``
  dispatches
  ``ActivityManager.killBackgroundProcesses(<server package>)``
  from the Check-again paths in ``_do_check_again``. Belt-and-
  braces for the rollout window before every field server APK
  ships the receiver, and for the rare case where the
  ``MY_PACKAGE_REPLACED`` broadcast didn't fire. Harmless when
  the server is healthy (the next call lazy-spawns from the
  current APK either way), curative when it's stale.
- **Peer permission.** ``KILL_BACKGROUND_PROCESSES`` added to
  the required peer permissions in ``CLIENT_INTEGRATION.md``
  § 2. Normal-protection, no runtime grant prompt. Without it
  the helper raises and falls through to the legacy behaviour
  (user's next launch eventually picks up the new code once
  the OS recycles the old process for its own reasons).
- **Contract codification.** New § 19 "Package-replacement
  handling" in ``CLIENT_INTEGRATION.md`` formalises the rule:
  every suite APK MUST self-handle ``MY_PACKAGE_REPLACED``;
  peers MAY backstop with ``killBackgroundProcesses``; peers
  MUST NOT assume on-disk APK matches the running server
  process without verifying.

### azt_collabd 0.41.27 / azt_collab_client 0.41.25 — COMMIT_REPEATEDLY_FAILED + scheduler-driven retry: catch the "164 files in one commit" pattern even when the user is idle

User report: production commits arriving with ~164 files apiece, hours
or days of recording sessions, after long silent stretches where
nothing pushed at all. The pattern is "failure to commit for some
time, followed by a successful catchup commit." Until now the daemon
shipped a one-shot ``S.COMMIT_FAILED`` per failed attempt with no
across-attempts memory, so a streak of failures looked indistinguish-
able from one unlucky retry — the user kept recording, files piled up
on the device's daemon-private filesDir, and the eventual catchup
commit hid the magnitude of the gap.

- New status code ``S.COMMIT_REPEATEDLY_FAILED``: surfaced when the
  same project has hit ``S.COMMIT_FAILED`` two-or-more times in a
  row. Counter persisted at ``projects.json :: <langcode>
  .commit_failure_count``, bumped on every COMMIT_FAILED branch,
  cleared on every successful commit. Threshold = 2 because
  dulwich's ``porcelain.commit`` essentially only raises on
  persistent conditions (index corruption, refs problem, disk
  full, broken repo state); one failure can be a fluke, two means
  the underlying problem isn't self-healing. ``count`` and the
  last dulwich ``error`` ride the status params.
- **Scheduler-driven retry.** The connectivity-watcher loop now
  also drains stuck commits every tick (default 30 s) with
  exponential backoff (30, 60, 120, … s, capped at 1 hour). An
  idle device with a failed commit gets a second look without
  the user having to gesture the peer; recovery from a
  transient cause (lock released, disk freed, daemon restart)
  clears the counter automatically. Implementation:
  ``scheduler._drain_stuck_commits`` in
  ``azt_collabd/scheduler.py``.
- **``ProjectStatus`` exposes the streak (diagnostic).** The
  ``project_status`` RPC response gains
  ``commit_failure_count`` + ``last_commit_failure_at`` +
  ``last_commit_error`` for diagnostic surfaces (settings
  screens showing "last commit error: …"). The alarm itself
  still flows through ``result.statuses`` only — the counter
  persists between gestures, so the next peer-driven sync
  after a background failure naturally sees the elevated
  counter and carries ``COMMIT_REPEATEDLY_FAILED`` on its
  result. Peers do not need to synthesize the alarm from the
  polled count; § 17a in ``CLIENT_INTEGRATION.md``
  documents this explicitly.
- Routing: ``CLIENT_INTEGRATION.md`` § 17 lands the code in the
  same never-silenced bucket as ``DATA_LOSS_RISK`` — auto-sync
  still must surface it (silencing would hide active data loss
  in exactly the catchup-commit pattern the bug was filed
  against). The auto-sync code shape now iterates and surfaces
  both codes before the silencing branches consume the result.
- Translation: client catalog + French ``.po`` carry a
  data-loss-class user-visible message that names "Settings →
  Diagnostic log → Log server activity = yes, then Share daemon
  log so we can investigate" — same shape as the
  ``DATA_LOSS_RISK`` message, since the investigation surface
  is identical (the daemon log will show *why* the commits
  failed).
- ``MIN_CLIENT_VERSION`` ↑ 0.41.25, ``MIN_SERVER_VERSION``
  ↑ 0.41.27 — new status code + new ProjectStatus fields;
  pre-this-version clients have no translation and no
  poll-surface, falling back to the auto-sync result iteration
  alone.

### azt_collabd 0.41.21 / azt_collab_client 0.41.21 — Scan QR: fix IntentIntegrator autoclass path + bundle AndroidX transitively; multi-density server-APK presplash

Plus, adopting the multi-density splash pattern from
``NOTES_TO_DAEMON.md`` "be eager when you have room to" §9 for the
server APK itself:

- ``generate_presplash.py`` rewritten to emit one PNG per Android
  density bucket (ldpi 0.75x → xxxhdpi 4x, mdpi 320×533 baseline)
  under ``server_apk/presplash_variants/drawable-<bucket>/presplash.png``.
  Fonts and icon are scaled per bucket so each variant is sharp
  at its native size. The legacy hdpi-sized
  ``server_apk/presplash.png`` is also rewritten as the
  ``presplash.filename`` rare-fallback.
- ``server_apk/buildozer.spec.tmpl`` grows an
  ``android.add_resources`` listing pointing at the six bucket
  variants, so Android's resource resolver picks the right one at
  install / launch time. No runtime PIL-resize on first boot.

Run ``python generate_presplash.py`` once before each release
build to refresh the version stamp; the produced
``presplash_variants/`` is build output (gitignore candidate) but
the spec entry is permanent.

Plus the rest of the "be eager when you have room to" asks
filed under the same note:

- **``CLIENT_INTEGRATION.md`` § 18 "Low-power adaptive policy"**
  documents the three rules (OS signals not user toggle;
  automatic for resource decisions, user-facing for content /
  workflow; pre-built variants beat runtime regeneration),
  the gate-vs-don't-gate inventory, the multi-density
  ``android.add_resources`` recipe, the verification block,
  and the diagnostic-logging shape. ``CLAUDE.md`` carries the
  rationale (why automatic, why build-time-work-in-the-build).
- **``azt_collab_client.lowpower``** ships as a new module —
  the JNI plumbing peers were duplicating, plus a single source
  of truth for the thresholds (3 GB / 6 GB tier cuts, 0.15
  availMem ratio, 720 px lowMemory downsample). API:
  ``total_ram_mb()``, ``memory_state()``, ``is_low_memory()``,
  ``is_metered_network()``, ``have_room_for_prefetch()``,
  ``ram_tier()``, ``densityDpi()``, ``dpi_to_bucket()``,
  ``identify_drawable_variant()``, ``log_presplash_variant()``.
  Thresholds are module-level constants, override before first
  call. ``AZT_FORCE_LOW_MEMORY=1`` env flips every signal to its
  budget-device value for local testing.
- **Diagnostic recipe corrected.** The first-pass recipes
  (``Drawable.getIntrinsicWidth/Height()``,
  ``BitmapDrawable.getBitmap().getDensity()``) both reported
  device-scaled state and silently collapsed every bucket on
  any given device. ``identify_drawable_variant`` uses
  ``BitmapFactory.decodeResource`` with ``inJustDecodeBounds=
  true`` + ``inScaled=false`` instead — ``opts.outWidth`` /
  ``opts.inDensity`` then carry the native pixel width / source
  folder density of the file Android actually picked, so the
  bucket name can be identified unambiguously.
- **Server APK logs its own variant.** ``server_apk/main.py``
  calls ``log_presplash_variant(tag='presplash:server')`` at
  startup; sister apps log under their own distinct tag (e.g.
  ``'presplash'``) so combined logcat is grep-able.

**Daemon-driven CAWL prefetch: offline-gate + circuit breaker.**
0.41.4 added daemon-side offline backoff; 0.41.8 dropped it
because "the peer has a circuit breaker"; 0.41.11 moved iteration
into the daemon's ``_prefetch_worker`` and the peer's circuit
breaker silently stopped applying (it lived in the old per-image
peer iteration model). Net result on an offline boot: the daemon
hammered DNS for every entry in the requested paths list,
producing logcat spam shaped like ``[cawl] image fetch failed for
… URLError: <urlopen error [Errno 7] No address associated with
hostname>`` repeated N times in ~40 ms intervals.

``_prefetch_worker`` now:

1. Checks ``net._has_internet()`` once at start. If offline,
   marks state ``skipped_offline=True`` / ``finished=True`` and
   returns immediately — no iteration, no spam.
2. Tracks consecutive failures inside the loop. After
   ``_PREFETCH_CONSECUTIVE_FAIL_LIMIT`` (3) back-to-back
   ``get_image_path`` failures, marks ``circuit_open=True`` /
   ``finished=True`` and bails. Real fetches succeed in
   <500 ms; three offline-class failures bunched together mean
   the device dropped connectivity, not three individually
   missing files.

``_make_prefetch_state`` grows two fields (``skipped_offline``,
``circuit_open``) and a ``started_at`` timestamp.

**``cache_status`` surface widened.** ``cache_status(repo)`` now
returns a dict instead of a ``(cached, total)`` tuple:

```
{'cached': int, 'total': int,
 'offline': bool, 'circuit_open': bool,
 'finished': bool}
```

The ``GET /v1/projects/<lang>/cawl/cache_status`` HTTP response
gains the same three flags. When the worker was offline-skipped,
``cached`` falls back to the actually-on-disk count via
``_walk_image_count`` — so a device with prior cache shows e.g.
"1247 / 3000 (offline)" instead of "0 / 3000" each offline boot.

**Daemon settings UI banner** rendered three ways now:

- normal: ``Caching images: M / N (network in use — please stay online)``
- offline-skipped: ``Image cache: M / N (offline — will resume when online)``
- circuit-broken: ``Image cache: M / N (paused — connectivity lost)``

Old peers reading only ``cached`` / ``total`` from the response
keep working — the new flags are additive.

**Stage A: daemon-driven auto-prefetch.** The daemon now owns
the "warm the CAWL image cache" decision instead of waiting
for a peer-driven ``cawl/prefetch`` POST. ``_touch_project``
(which fires on every langcode-bound endpoint) now also calls
``cawl.auto_prefetch(repo)``. ``auto_prefetch``:

- Resolves the full index image path set via the cached
  index (no network).
- Throttles to at most one trigger per repo per 30 s, so the
  1 Hz cache-status poll doesn't re-probe ``_has_internet``
  every second.
- Defers to ``start_prefetch``'s existing idempotency. A
  running prefetch with matching paths is a no-op; a finished
  prefetch (success OR offline-skipped) restarts, which is the
  natural retry path when connectivity may have returned.

Peers may continue to POST ``cawl/prefetch`` with their own
working-set list — useful when the peer wants to warm a
subset different from the full index. The endpoint is
backward-compatible. Stage B (peer-side removal of the POST)
ships in a later peer release; today's change is additive.

**Offline → online auto-resume.** The scheduler's
connectivity watcher already fires on the offline → online
edge (every ``connectivity_poll_s``, default 30 s). On that
edge it now also calls ``cawl.on_online_edge()`` which clears
the auto_prefetch throttle for any repo whose last state was
``skipped_offline`` or ``circuit_open`` and re-fires
``auto_prefetch``. Cache warming resumes within ~30 s of
network return with no user action required.

The cache-status banner poll **stays at 1 Hz** even on
offline / circuit_open state — the response is just
in-memory dict lookups, and the ``[first-try]`` probe for the
cache_status path is already suppressed (see below). Keeping
the poll running is what makes the banner auto-update from
"offline — will resume" to live progress when
``on_online_edge`` does its work.

**CAWL prefetch policy: one variant per id (default) vs. all
variants.** New config knob
``$AZT_HOME/config.json :: cawl.prefetch_all_variants``,
default ``False`` — daemon's auto_prefetch warms one image
per CAWL id (the file whose basename contains the canonical
``__`` preferred-variant marker, falling back to the first
file in the id directory if no variant carries the marker).
Set to ``True`` to warm every image-shaped index entry —
heavier on network and disk but useful for users who want
the full set offline.

API surface:

- Daemon: ``store.get_cawl_prefetch_all_variants`` /
  ``store.set_cawl_prefetch_all_variants(bool)``.
- HTTP: ``GET / POST /v1/config/cawl_prefetch_all_variants``,
  body ``{enabled: bool}``.
- Client: ``azt_collab_client.get_cawl_prefetch_all_variants``
  / ``set_cawl_prefetch_all_variants``.
- Filter logic: ``cawl._filter_preferred_variant_per_id``
  applied inside ``_index_image_paths`` whenever
  ``prefetch_all_variants`` is False.

Flipping the policy doesn't retroactively re-warm an
in-flight worker; the next ``auto_prefetch`` trigger
(project-load, scheduler edge) picks up the new path set.
Existing on-disk cache entries are kept either way.

**Daemon SettingsScreen highlights missing contributor on
entry.** Peers that route a ``S.CONTRIBUTOR_UNSET`` sync
failure through ``open_server_ui()`` previously dropped the
user onto the settings page with no indication of *which*
field was the blocker — the peer-side translated toast
("Please set your name…") could flash for under a second
and be eaten by the screen transition. On screen entry, if
``contributor_input`` is empty and not already focused, the
input now takes focus (keyboard pops up on Android) and the
inline hint reads "Required: your name is used for commit
authorship; sync and publish refuse until this is set." in
the red status colour. Saving a non-empty value clears the
hint back to the normal "Saved." confirmation.

**``[data-loss-risk]`` detection + new ``S.DATA_LOSS_RISK``
status.** ``_stage_audio`` and ``_sync_repo_locked`` now walk
``project_dir`` for any file outside the staging filter
(``audio/`` / ``images/`` / ``*.lift`` / ``.git/`` /
``.azt_atomic_pending/`` / ``.azt-collab/`` / known top-level
files like ``.gitignore``). Anything else is a peer writing
to a path the daemon won't commit — silent data loss class.
Each finding emits ``[data-loss-risk] uncommittable file in
project_dir: <rel>`` to stderr (so a tester-shared daemon log
makes the issue obvious), and the sync ``Result`` carries
``S.DATA_LOSS_RISK`` with ``count`` and ``sample`` (up to 5
paths) params.

**Peer contract** (``CLIENT_INTEGRATION.md`` § 17): this status
is **never silenced**. Auto-sync and user-initiated sync both
surface the translated toast unconditionally, urging the user
to enable "Log server activity" and share the daemon log.
Status is bucketed separately from the config-class /
transport-class statuses that auto-sync silences, because this
one represents active data loss, not a configuration glitch.

**``[stage-audio]`` / ``[commit-audio]`` diagnostic logs.**
Field report: testers record 1000+ audio files but only ~146
land in each commit (and only 4 commits total). Without
``adb`` access to the remote testers' phones we can't run
``ls audio/`` or ``git status`` directly. Daemon-side logging
in ``_stage_audio`` now emits a one-liner per pass with the
counts that disambiguate the gap:

```
[commit-audio] start project_dir='…/projects/baf' contributor=…
[stage-audio]  project_dir='…/projects/baf'
               on_disk_audio=1042 on_disk_images=12
               status.unstaged=0 status.untracked=898
               paths_to_add=898
[commit-audio] _stage_audio returned n=898
[commit-audio] committed n=898 sha=abc123def456
```

- ``on_disk_audio`` ≫ ``status.untracked`` → ``porcelain.status``
  is truncating large untracked sets; the gap is dulwich's,
  not the peer's.
- ``on_disk_audio`` ≈ ~146 → peer write path is dropping
  bytes; gap is upstream.
- ``status.untracked`` ≈ ~146 and ``on_disk_audio`` ≈ ~146
  ≈ ``paths_to_add`` over multiple syncs → user's record
  count is overcounting attempts vs. successes.

Remote tester recipe: daemon settings UI → "Log server
activity: yes" → record + sync → "Email daemon log". ``<_PickerRoot>`` hardcodes ``back_to:
'picker'`` on the SettingsScreen instance — correct in
external mode (settings reached from picker via the gear,
back should pop to picker), but wrong in internal mode:
settings is the root the user reached from outside the
Activity (launcher tap or peer's ``open_server_ui()``), so
the KV Back button navigating to picker dumped the user on
a screen they never asked for. ``PickerApp.build`` now
clears ``back_to`` on the settings screen in internal mode,
which trips the KV's
``height: dp(48) if root.back_to else 0`` gating and hides
the button entirely. The OS back path (``_navigate_back``
internal branch) remains the only way to leave settings,
letting Android finish() the Activity and return the user
to wherever they came from.

**``Switch project`` button promoted out of the gated row.**
Previously sat alongside Grant collaborator + Share repo QR
inside ``project_actions_row``, which is hidden when the
current project has no remote. Switch is meaningful before
publish too (user may want to abandon an unpublished project
for another), so it now lives in its own always-visible RecBtn
directly under the gated row — same vertical position
relative to the rest of the screen, but unconditionally
tappable.

**``project_actions_row`` hides via detach instead of just
``height: 0``.** Same Kivy touch-intercept bug ``publish_row``
already worked around: a BoxLayout with ``height: 0, opacity:
0`` still has its children at their declared sizes in the
widget tree, so their ``on_press`` handlers receive taps at
coordinates that visually belong to buttons higher up.
Symptoms: tapping ``Connect to GitHub`` fired
``grant_collaborator()`` (the row's first button), tapping
``Publish`` (when present) fired ``switch_project()`` (the
row's third button), tapping ``Connect to GitLab`` looked
like a no-op (Share-repo-QR's ``_pick_publish_candidate``
returned ``None``). ``_refresh_project_actions_row`` now
detaches all three children when hiding and reattaches when
showing — mirror of ``_detach_publish_children`` /
``_reattach_publish_children`` already in place.

**Edge-to-edge: status bar no longer hides the picker's gear
icon.** Android 15+ enforces edge-to-edge by default — the
status bar overlays the app window unless we opt back into
the pre-API-35 reserved-inset behaviour. Top-of-screen
widgets (the picker's gear, every screen's TopBar) sat
under the status bar; bottom-anchored widgets would have
sat under the gesture bar the same way. ``PickerApp.on_start``
now calls ``WindowCompat.setDecorFitsSystemWindows(window,
True)`` on the Activity's UI thread (via p4a's
``android.runnable.run_on_ui_thread`` helper), restoring
inset reservation. Available because we already pull
``androidx.appcompat`` (which transitively brings
``androidx.core.view``).

**``PickerApp.font_name`` alias.** Settings UI code that opens
modals (``share_repo_qr``, ``grant_collaborator``) reads
``App.get_running_app().font_name`` directly — fine under the
old ``CollabUIApp`` which exposed ``font_name`` as a class
attribute, but ``PickerApp`` only had the private ``_font_name``.
Under the unified PickerApp on Android, tapping ``Share this
repo (QR)`` (and ``Grant collaborator access``) raised
``AttributeError: 'PickerApp' object has no attribute
'font_name'`` and Kivy's event-loop catch buried it — the user
saw "tap does nothing." New ``@property font_name``  on
PickerApp returns ``_font_name`` so both callsites resolve
identically across host App classes.

**UX cleanup after the picker+settings merge.**

- **Share-repo QR popup** — dropped the "Copy URL" button.
  Close is the only remaining action; the URL is visible
  above the QR for users who'd rather read it than scan it.
- **Install / update popup** — "Open install page" relabeled
  to ``More info`` and moved RIGHT of the Install button so
  the affirmative action lands where the eye expects.
- **Install / update popup status line** — split out of
  ``body_label`` into a dedicated ``status_label`` rendered
  in the ACCENT colour, bold, sp(15). "Tap install again to
  confirm" and other transient status messages now read as
  the current call-to-action instead of vanishing into the
  wall of explanatory text above.
- **Contributor input hint** — changed from a specific
  example name to ``first_name last_name`` (generic).
- **Contributor "Required" message** — ``contributor_msg``
  label now auto-grows on ``texture_size`` so the multi-line
  warning isn't truncated when the SettingsScreen surfaces
  it on entry.

**Ungraceful-shutdown detection via sentinel file.** New
``azt_collabd/crash_marker.py``: on startup, writes
``$AZT_HOME/process_running.json`` with this process's pid +
started_at, registers an ``atexit`` hook to delete it on
clean shutdown. On the NEXT startup, a leftover sentinel
means the previous process bypassed atexit (SIGSEGV, SIGKILL,
OOM-kill, ``os._exit``, kernel-level kill); a one-line
summary lands in ``$AZT_HOME/last_native_crash.json``.

``GET /v1/health`` now surfaces it alongside the existing
``last_crash``:

```
{"ok": true, ...,
 "last_native_crash": {
   "detected_at": 1747234567.123,
   "previous_pid": 12917,
   "previous_started_at": 1747234389.456,
   "signal": "",
   "thread_name": "",
   "approx_pc": "",
   "detection_source": "ungraceful-shutdown sentinel"}}
```

``last_crash`` and ``last_native_crash`` are complementary:
the former is written by the daemon's Python excepthook from
the dying process (caught exception, Python alive to write
it); the latter is detected on the *next* startup from
sentinel-file diff (signal handler bypassed Python entirely).
A peer's `[server-crash]` log helper can mirror both.

Closes NOTES_TO_DAEMON.md "Daemon-side surface for native
crashes" by the pragmatic route — no JNI sigaction handler,
no async-signal-safe C extension. ``signal`` / ``thread_name``
/ ``approx_pc`` ship as empty strings reserved for a future
sigaction-driven shape: when a real handler lands, it
populates them in the dying process before ``_exit()``, peers
see richer detail with no schema change.

**"Switch project" button on the daemon settings UI + unified
picker/settings Kivy app.** New ``Switch project`` button in
the "Current project" row, sibling to Grant collaborator and
Share-repo-QR. Tapping it navigates to the project picker
in-process — no Intent, no Activity transition — and the
picker's submit handler stamps the new langcode via
``set_last_project`` and navigates back to settings.

The unification: the server APK used to run two separate
Kivy Apps (``CollabUIApp`` for settings, ``PickerApp`` for the
picker), one chosen at startup from the launching Intent
action. With ``PythonActivity`` being ``singleTask`` (p4a
default), firing PICK_PROJECT on ourselves wouldn't spawn a
fresh Activity — Android would route through
``onNewIntent`` on the existing one. So the only path to an
in-process switch is one Kivy App that hosts both screen
sets. ``PickerApp`` (which already had ``SettingsScreen`` as
a sibling for the picker → gear → settings flow) is the
unified home; ``server_apk/main.py`` always invokes it now,
passing ``launch_mode='external'`` for PICK_PROJECT Intents
(existing peer-driven behaviour, picker is initial screen,
submit fires setResult/finish) or ``launch_mode='internal'``
otherwise (settings is initial screen, picker submit writes
``last_project`` + navigates back to settings).

``_navigate_back`` branches on ``_launch_mode``:
- external: existing behaviour (back from picker exits the
  Activity with setResult, etc.).
- internal: back from settings returns False so Android
  closes the Activity (matching pre-0.41.22 ``CollabUIApp``
  semantics); back from picker / langpicker navigates to
  settings instead of finishing the Activity.

``PickerApp.on_resume`` added: refreshes the active screen
when the Activity comes back to the foreground.

**Pairs with the peer-side ``CLIENT_INTEGRATION.md`` § 14a
contract.** The daemon-side button is a no-op for the peer's
loaded view until peers ship the ``App.on_resume`` ↔
``last_project()`` reconciliation hook documented there. Ship
the daemon button now; peers adopt the on_resume hook in
their next release; the UX is coherent end-to-end at that
point. Mismatched timing degrades gracefully — the user
lands back on the previous project (the old pre-button
behaviour), nothing destructive.

**Diagnostic log section follows the same binary-toggle
pattern.** The single ``Save daemon log to file`` /
``Stop saving daemon log`` button is replaced by a row reading
``Log server activity:`` followed by two side-by-side buttons
— ``yes`` and ``no`` — with the active state highlighted in
the GREEN accent. Status line underneath is preserved (it
shows the log file path / "log capture is off" / byte count
on screen entry). Share + Email buttons below stay disabled
while logging is off and re-enable once the user picks
``yes``. Same convention as the wordlist row, the language
selector, etc.

**Daemon settings UI exposes the toggle.** New section on the
SettingsScreen, between "Refresh Status" and "Diagnostic log".
Section label reads ``Wordlist ({name}) images`` where
``{name}`` is the active project's wordlist (derived from the
image-repo slug — ``kent-rasmussen/images_CAWL`` →
``CAWL`` — via the new ``cawl.wordlist_name`` helper). Row
underneath reads ``Cache images:`` followed by two side-by-
side buttons — ``1 per line`` and ``all`` — with the active
mode highlighted in the GREEN accent (matching the language-
selector row's convention). Label updates on each
``refresh()`` so switching projects between visits to the
SettingsScreen renames the section to match.

**``cache_status`` cached count capped at ``requested`` in
offline-skipped state.** The walk-count fallback I introduced
this release (so an offline boot with prior cache shows e.g.
"1247 / 3000 (offline)" instead of "0 / 3000") counts every
file in the on-disk cache directory, which accumulates across
working sets and past sessions. Peer-reported case had the
disk holding 2220 files while the current ``requested`` was
1661, producing a "cache warm: 2220/1661" banner that tripped
peer-side "fully warm, hide and stop polling" logic and
looked like a daemon accounting bug. ``cache_status`` now
returns ``min(walk_image_count, requested)`` in the
offline-skipped branch; the active and circuit_open branches
were already accurate.

**jnius pre-warm at server-APK startup (main thread).** A
tombstone caught during the intermittent ``:provider`` crash
showed ``art::JNI::CallObjectMethodA`` SEGV at NULL on
``Thread-4`` (an unnamed Python-spawned thread, NOT our
prefetch worker). Two daemon-side helpers — ``paths.azt_home``
and ``store._autodetect_device_name`` — do their first jnius
work lazily on whichever thread happens to need the value
first. Python-spawned threads attach to the JVM via
pyjnius's auto-attach with the bootclassloader; first-time
``CallObjectMethodA`` on app-context fields from those
threads is the leading suspect for the NULL deref (per the
0.33.x classloader-attach precedent).

``server_apk/main.py`` now calls ``azt_home()`` and
``get_device_name()`` once on the main thread, immediately
after ``install_callbacks``. Both then serve from cached
state (process memory / config.json) for every subsequent
caller on any thread — no JNI dispatch from background
workers needed.

**Named all unnamed daemon-side worker threads.** The
``Thread-4`` in the tombstone could have been any of several
unnamed ``threading.Thread`` / ``threading.Timer`` spawns in
the daemon. Naming them lets the next crash backtrace
identify the worker directly:

- ``sync-fire-<langcode>`` (Timer / immediate sync workers
  in ``scheduler.py``)
- ``gh-device-flow-<id>`` (GitHub device-flow OAuth polling)
- ``clone-<id>`` (clone-job worker)
- ``httpd-shutdown`` (graceful loopback HTTP shutdown)

The CAWL prefetch worker was already named.

**``start_prefetch`` no longer spawns a second worker while
one is already running.** Pre-fix: a different ``requested``
count between calls would replace the state dict and start a
new thread; the old worker kept iterating and writing to the
new dict via ``_prefetch_state.get(repo)``. With Stage A
shipping ``auto_prefetch`` from every ``_touch_project``
*and* pre-Stage-B peers still POSTing their own
``cawl_prefetch`` working set, two workers regularly arrived
on overlapping timelines — both doing urllib/SSL fetches +
jnius-cached class work simultaneously. Leading suspect for
a NULL-deref SIGSEGV in the daemon's ``:provider`` process
~2 s after the second prefetch POST.

New behaviour: if an unfinished worker exists for the repo,
``start_prefetch`` returns its state and does NOT start
another. Different repos still proceed independently
(``_prefetch_state`` is repo-keyed). The peer's working
subset of a daemon-warmed full index will see its targets
populated by the running worker — no semantic loss.

**Bootstrap self-update no longer proposes installing an older
release over a locally-installed newer build.** The probe used
``needs_update = version_newer OR digest_changed OR mandatory``,
where ``digest_changed`` would trip on any GitHub-side asset
change. When a developer adb-sideloads a version newer than the
latest published tag and then any GitHub release publishes a
new digest, the probe would propose downgrading. New
``local_newer`` gate suppresses ``digest_changed`` when the
installed version is strictly above the latest tag. ``mandatory``
overrides remain unchanged — server-told-too-old still prompts
regardless. Diagnostic ``[bootstrap] _probe`` log line now
includes the ``local_newer`` boolean.

**Server APK no longer ships maintainer scripts.** The
``source.include_exts = py`` glob was sweeping two
maintainer-only Python files into ``classes.dex`` /
``private.tar``:

- ``server_apk/test_install.py`` — desktop integration
  smoke-test for the kill-recovery flow; sibling to
  ``test_install.sh``.
- ``azt_collabd/data/cawl/generate_seed.py`` — script that
  regenerates the bundled CAWL index JSON from GitHub at
  release-cut time.

Neither has any runtime role; both are now in
``source.exclude_patterns`` so they stay out of the APK.

**``[first-try]`` probe suppressed for cache_status polls.**
The always-on first-try diagnostic probes added in 0.41.16
were valuable for the no-adb field tester but emitted two
lines per cache_status poll at 1 Hz — pure noise on a normal
session. Transport now suppresses the probe when
``path.endswith('/cawl/cache_status')``. "First-try"
semantically doesn't apply to the Nth call of a polling
loop; all other RPC calls remain fully instrumented.

**Docs reorg — NOTES_TO_DAEMON.md is a live queue only.** The
two "standing notice" items that had accumulated there are
promoted to canonical homes:

- "Daemon is the sole authoritative source" (daemon-owned
  state table + four daemon obligations) → ``CLAUDE.md`` hard
  rule #8 + new "Daemon-owned state" section. It's an
  architectural invariant the client architecture depends on;
  ``CLAUDE.md`` is the right shelf.
- "Project-bound surfaces now in daemon UI (Phase 3)" →
  ``CLIENT_INTEGRATION.md`` § 12b "Project-bound actions live
  in the daemon settings UI", with the Phase-1 / Phase-3
  sequencing constraint preserved. It's peer-facing direction;
  the contract is the right shelf.

NOTES preamble tightened to call out the antipattern
explicitly: standing rules belong in ``CLAUDE.md`` /
``CLIENT_INTEGRATION.md``, not in the queue file. Otherwise
the queue silently turns into a reference shelf and stops
being a queue.

---

Two coupled bugs surfaced when testing the picker's "Scan QR"
affordance against the 0.41.20 server APK. The first masked the
second; both had to be fixed to make the button work.

Two coupled bugs surfaced when testing the picker's "Scan QR"
affordance against the 0.41.20 server APK. The first masked the
second; both had to be fixed to make the button work.

**1. Autoclass path corrected** in
``azt_collab_client/ui/qr_scan.py``. Was
``com.journeyapps.barcodescanner.IntentIntegrator``; the class
actually lives at
``com.google.zxing.integration.android.IntentIntegrator`` — the
journeyapps AAR re-ships ZXing's original IntentIntegrator at its
historical package path even though the rest of the library is
under ``com.journeyapps.barcodescanner``. Module docstring updated
to call out the mismatch.

**2. AndroidX transitive deps listed explicitly** in
``server_apk/buildozer.spec.tmpl``. The zxing-android-embedded
4.3.0 POM declares its AndroidX deps (fragment, appcompat) as
``implementation`` rather than ``api``, so Gradle uses them to
compile the AAR's own classes but does NOT propagate them to the
consuming APK's classes.dex. Result: the journeyapps classes
reference ``androidx.fragment.app.Fragment`` /
``FragmentActivity`` / ``AppCompatActivity`` but the Android
verifier can't resolve those references at class-load time, and
``autoclass(...IntentIntegrator)`` raises
``NoClassDefFoundError: Landroidx/fragment/app/Fragment;`` even
with the autoclass path fix in (1).

New ``android.gradle_dependencies``:

```
com.journeyapps:zxing-android-embedded:4.3.0,
androidx.appcompat:appcompat:1.6.1,
androidx.fragment:fragment:1.6.2,
org.jetbrains.kotlin:kotlin-stdlib:1.8.20,
org.jetbrains.kotlin:kotlin-stdlib-jdk7:1.8.20,
org.jetbrains.kotlin:kotlin-stdlib-jdk8:1.8.20
```

Listing appcompat + fragment explicitly forces Gradle to pull them
into the project classpath, so the dex actually carries the
``Landroidx/...`` implementations the journeyapps code references.

The three kotlin-stdlib pins resolve a transitive-version conflict
that surfaced as ``:checkReleaseDuplicateClasses`` failing with
``Duplicate class kotlin.collections.jdk8.CollectionsJDK8Kt``:
``androidx.fragment:1.6.2 → lifecycle-runtime:2.6.2`` pulls
``kotlin-stdlib:1.8.20`` (the post-merge artifact that already
ships the JDK7/JDK8 helper classes), while the same lifecycle-
runtime transitively pulls ``kotlinx-coroutines-android:1.6.4 →
kotlin-stdlib-jdk{7,8}:1.6.21`` (the pre-merge split artifacts
that also ship them). Forcing the ``-jdk7`` / ``-jdk8`` resolution
up to 1.8.20 lands on the empty metadata-only redirect artifacts
Kotlin started shipping at 1.8 once the split was deprecated, so
the duplicate-class collision disappears with no functional
change to anything else in the build.

**Build note.** Re-run ``server_apk/build_buildozer_spec.sh`` to
regenerate ``buildozer.spec`` from the template after pulling, then
``buildozer android clean && buildozer android release`` — the dist
tree caches Gradle resolution, so a clean is required to pick up the
new dependency list.

**Floor:** no bumps. Server APK rebuild required to ship the fix
since qr_scan runs inside the picker subprocess hosted by the server
APK, and the AndroidX deps need to be in *that* APK's dex.

### azt_collabd 0.41.20 / azt_collab_client 0.41.20 — docs: routing table moves to contract; atomic_open_write note added

Docs-only release closing two long-standing gaps between
``CLAUDE.md`` (philosophy / rationale) and
``CLIENT_INTEGRATION.md`` (conformity contract). Per the
docs-separation rule, conformity material belongs in the
contract; rationale belongs in CLAUDE.md.

**Sync-result routing table moved to contract** as new
``CLIENT_INTEGRATION.md`` § 17 (Routing on sync results). Full
table of status codes × auto-sync vs. user-initiated sync
behaviour, the canonical code shape for both contexts, and an
explicit ``S.*`` constant reference noting which constants
shipped in 0.41.13 (``SERVER_UNAVAILABLE`` /
``SERVER_ERROR``). The CLAUDE.md "Peer contract: routing on
sync results" section now carries only the rationale (per-
code meanings, the pre-0.34.1 anti-pattern, why the auto/user
distinction lives peer-side) and points to the contract for
the actual table + code.

**``atomic_open_write`` FD-path documented in § 8** of the
contract. Pre-0.41.7 the URI form of ``atomic_open_write``
shipped LIFT bytes as base64 inside the JSON-RPC body and hit
Binder's ~1 MB per-transaction cap on Android — silent
failure for LIFT > ~700 KB. Peers rebuilding against 0.41.7+
pick up the two-phase FD-write + finalize protocol
transparently; the contract now notes the rebuild-for-large-
LIFTs implication so peer maintainers know it's a free
correctness win.

No code changes in this release; docs only.

### azt_collabd 0.41.19 / azt_collab_client 0.41.19 — `share_log_file` + French translations + docs

Follow-on to 0.41.18 in response to recorder 1.41.24's filing
(NOTES_TO_DAEMON.md 2026-05-13). Two changes:

**``share_log_file(log_path, prev_path=None, ...)`` helper**
added to ``azt_collab_client/ui/share.py``. Reads a log file
(plus optional previous-session log) from disk, bundles into
one ``text/plain`` blob with section breaks, inserts into
MediaStore Downloads to get a real ``content://`` URI, and
dispatches an ``Intent.ACTION_SEND`` with ``EXTRA_STREAM``.

Unlike ``share_text``, this attaches as a real file (receivers
can save it; payload size isn't bounded by Intent extras), and
unlike ``share_running_apk`` it handles two source files +
sets a sensible default ``display_name``. Mirrors the
MediaStore-insert pattern from ``share_running_apk`` so the
underlying jnius dance is shared at the call-site level.

Recorder will replace its peer-side stand-in with one
``share_log_file(log_path=_LOG_PATH, prev_path=…)`` call once
it picks up 0.41.19.

**Daemon UI's "Share daemon log" migrates to
``share_log_file``** (reading ``$AZT_HOME/daemon.log`` from
disk directly — both daemon-UI and daemon-proper processes
share filesDir on Android, so file-based access works without
an additional RPC for the bytes). Email button still uses
``email_text`` since ``ACTION_SENDTO`` with a ``mailto:`` URI
restricts the picker to email apps.

**French translations** added for all 0.41.17-0.41.19 strings:
"Diagnostic log", "Save daemon log to file", "Stop saving
daemon log", "Share daemon log", "Email daemon log", and
related status / error messages. Plus the helper-side
``Share log`` / ``AZT log`` / ``Could not share log`` /
``Log file is empty`` / ``Log file: {path}`` strings.

**Docs.** ``CLIENT_INTEGRATION.md`` § 14b now lists all three
share helpers (``share_text``, ``email_text``,
``share_log_file``) with picking-between guidance.
``azt_collab_client/CLAUDE.md`` carries the rationale for the
share-module extraction and the daemon-log toggle's
hot-toggle design.

### azt_collabd 0.41.18 / azt_collab_client 0.41.18 — share helpers extracted + email-log button

Follow-on to 0.41.17. Two changes:

**``share_text`` and ``email_text`` extracted into
``azt_collab_client/ui/share.py``** alongside the existing
``share_running_apk``. Both reusable by any peer:

- ``share_text(text, subject='', chooser_title='', on_error=None)``
  — ``Intent.ACTION_SEND`` with ``EXTRA_TEXT``. Any
  ``text/plain``-handling share target accepts it.
- ``email_text(text, to='', subject='', on_error=None)`` —
  ``Intent.ACTION_SENDTO`` with a ``mailto:`` URI. Restricts the
  picker to email apps only.

The daemon UI's "Share daemon log" button now delegates to
``share_text`` instead of inlining the JNI dance.

**"Email daemon log" button** added to the Diagnostic log
section alongside "Share daemon log". Uses ``email_text`` for
the email-only picker affordance — better UX than the generic
share sheet when the user's intent is specifically "send this
to the developer".

### azt_collabd 0.41.17 / azt_collab_client 0.41.17 — daemon-log-to-file toggle + share button

Remote tester can't run logcat, so daemon-side diagnostic
output (``[boot-trace-daemon]``, ``[cawl]``, ``[recent]``,
``[first-try]`` from the daemon UI / picker subprocess) was
unreachable. Added:

- **Config knob** ``logging.daemon_log_to_file`` in
  ``$AZT_HOME/config.json``. Default off.
- **Stderr tee** in the daemon. When the toggle is on,
  ``sys.stderr`` is wrapped to mirror writes to the original
  destination (logcat) AND to ``$AZT_HOME/daemon.log``. Tee
  is hot-installable / hot-removable — no daemon restart
  needed.
- **Endpoints.** ``POST /v1/logging/daemon_log_to_file`` (set
  toggle, install/remove tee in-process); ``GET
  /v1/logging/daemon_log`` (returns log contents + current
  toggle state + file path).
- **Settings UI.** New "Diagnostic log" section with two
  buttons: "Save daemon log to file" (toggle) and "Share
  daemon log" (Android ``Intent.ACTION_SEND`` with the log
  content as ``EXTRA_TEXT``). Status line under shows current
  state + file size.
- **Client wrappers.** ``set_daemon_log_to_file(enabled)`` /
  ``get_daemon_log()``.

The share intent uses ``EXTRA_TEXT`` (text/plain) rather than a
file-URI attachment so any text-handling share target accepts
it — email composers, messaging apps, file savers. Daemon
truncates to the last 256 KB to fit comfortably in an intent
extra; the diagnostic value lives in the tail anyway.

### azt_collabd 0.41.16 / azt_collab_client 0.41.16 — first-try probes always-on for this build

Remote tester can't run logcat (the device they have access
to isn't local). The previous env-var gate
(``AZT_DEBUG_FIRST_TRY=1``) was the wrong shape — they have
no way to set env vars on the device. Flipping the
``first_try_log`` gate to always-on for this build so the
probes write to peer stderr (which lands in
``/sdcard/azt_recorder.log``) without the tester having to
configure anything.

Restore the env gate after the crash is diagnosed; the gate's
``if not os.environ.get('AZT_DEBUG_FIRST_TRY'): return`` is
preserved in the module docstring for easy reinstatement.

### azt_collabd 0.41.15 — ContentProvider waits for Python callbacks (H5 defensive fix)

The "first-try-fails, second-try-works" pattern reported on
the Tecno KN4 (Helio G81, 4 GB RAM, Android 16) is most likely
Android killing the daemon's ``:provider`` process during a
user's brief navigation away (settings screen, etc.) and then
lazy-spawning it on the next peer call. The respawn race:
Android creates the AZTCollabProvider Java object and routes
the incoming peer call to it on a binder thread, while
Python's ``install_callbacks()`` is still initializing on
SDLThread. The provider sees ``sDispatch == null`` /
``sOpenFile == null`` and returns ``daemon_not_ready``, which
the peer surfaces as a crash. Second tap: Python is now
initialized, callbacks registered, call succeeds.

Defensive fix in ``AZTCollabProvider.java``: ``call()`` and
``openFile()`` now wait up to 3 seconds (50 ms polling) for
the Python callback to register before returning the
"daemon_not_ready" error. On a healthy respawn, Python
finishes ``install_callbacks()`` in well under a second and
the first peer call queues briefly behind it instead of
failing. On a truly-down daemon, the 3 s timeout still fires
and the failure surfaces the same as before.

Harmless if the bug wasn't H5: the wait loop only runs while
the callbacks are null, which is the respawn window only.
``ping`` requests (used by discovery probes) still bypass the
wait so transport-discovery latency is unaffected.

### azt_collabd 0.41.14 / azt_collab_client 0.41.14 — env-gated first-try-fails diagnostics

User reported a transient crash on the SettingsScreen →
"select new project" path: first try crashes, second works.
Nothing in logcat suggests a cause. Added probes for five
hypotheses, all gated behind ``AZT_DEBUG_FIRST_TRY=1`` so
they're inert when the env var isn't set. New helper
``azt_collab_client/_debug.py`` provides ``first_try_log``.
Probes:

- H1 (cache poll leaks past screen leave): in
  ``SettingsScreen._stop_cawl_cache_poll`` and
  ``_tick_cawl_cache_status`` — logs Clock event lifecycle
  + current screen at every tick.
- H2 (picker cold-start race): in
  ``ProjectPickerScreen.on_enter`` and ``_populate_projects``
  + ``picker_app.main`` — timestamps each phase.
- H3 (subprocess invocation): in ``picker_app.main`` entry
  + return — argv + dt.
- H4 (URI grant not propagated): in
  ``lift_io._open_content_uri`` — wraps
  ``openFileDescriptor`` with explicit exception logging
  so any swallowed ``SecurityException`` surfaces.
- H5 (daemon respawn drops the call): in
  ``transports.android_cp.call`` — logs bundle-null on
  return.

Enable with ``adb shell setprop … AZT_DEBUG_FIRST_TRY 1``
or by setting the env var in the launch path. When set,
``[first-try] <label> k=v ...`` lines appear in logcat at
each probe site.

### azt_collabd 0.41.13 / azt_collab_client 0.41.13 — CAWL: TTL-cached os.walk + quieter resolve logs + S.SERVER_UNAVAILABLE / S.SERVER_ERROR constants

**Cache-count undercount fixed.** 0.41.10's incremental
counter (lazy-seed + per-fetch increment) had a race that
produced an undercount in the wild (peer warmed 1661, daemon
reported 1257). Tracing didn't fully pin the race but the
failure-mode (silently wrong UI total) is bad enough that I
replaced the scheme rather than patching it. ``_walk_image_count``
is now a TTL-cached ``os.walk`` — 500 ms TTL, ~50 ms uncached
on the canonical 1700-image set, near-zero CPU at 1 Hz
polling. Accurate by construction: it counts what's actually
on disk, no event-based bookkeeping that can drift. Dropped
``_note_image_cached``, ``_cached_image_count``,
``_cached_count_seeded`` and their call sites. The TTL-cached
walk fallback is only used when no prefetch job is active for
the repo (otherwise the prefetch state still wins; same logic
as 0.41.11).

**Quieter resolution logs.** The
``[cawl] get_image_path: no index-resolution for X`` line was
firing for already-nested paths even though those paths
needed no resolution and the fetch was succeeding. Net effect
was a logcat full of scary "no index-resolution" lines for
calls that were working fine. Now: pass-through is silent;
the "flat basename not in index" case still logs because
that's a real "peer asked for something the daemon doesn't
know" situation.

**``S.SERVER_UNAVAILABLE`` / ``S.SERVER_ERROR`` constants.**
The peer-routing example in ``azt_collab_client/CLAUDE.md``
("Peer contract: routing on sync results") shows
``result.has_any(S.SERVER_UNAVAILABLE, S.SERVER_ERROR)`` but
``status.py`` didn't actually export those constants —
``AttributeError`` at runtime for conformant peer code. The
string literals were always emitted on results
(``Status('SERVER_UNAVAILABLE', …)`` from the wrappers'
transport-failure branches); only the typing-aid constants
were missing. Added to both ``azt_collab_client/status.py``
and the mirrored ``azt_collabd/status.py``. Peer note filed
2026-05-13 against 1.41.16 was the trigger.

### azt_collabd 0.41.12 / azt_collab_client 0.41.12 — quiet the 1 Hz poll logs

Three log-noise sources fired once per second once the
cache-status poll kicked in:

- ``[recent] GET /v1/recent/last_project → 'X' (from ...)``
  (daemon-side, ``_h_get_last_project``).
- ``[recent] last_project → 'X'`` (client-side wrapper).
- Both happen because the daemon UI's poll resolved
  ``last_project()`` every tick to know which project to
  query.

Fixes:

- Drop the success log from both sites. Error paths still
  log (``ServerUnavailable``, ``not ok``); they're rare and
  useful. The setter (``_h_set_last_project``) still logs
  because it's a real state change.
- Daemon UI now resolves the langcode once at poll start
  (``_start_cawl_cache_poll``) and reuses it across ticks.
  The user doesn't switch projects while sitting on the
  settings screen, so two RPCs/sec just to re-confirm the
  same langcode was overhead with no benefit.

### azt_collabd 0.41.11 / azt_collab_client 0.41.11 — daemon-driven CAWL prefetch + accurate progress

The 0.41.9 cache-status indicator reported "files on disk vs.
all image-shaped entries in the index". For the canonical
``kent-rasmussen/images_CAWL`` set that's "959 / 3231" — the
total is the count of every image file across all variants,
but the peer typically warms one variant per CAWL identifier
(~1661 files). The banner plateaued at ~1661/3231 with no way
for the user to tell whether the system was done. Misleading
for the indicator's core purpose ("don't disconnect, you're
not done yet").

Root cause: the peer iterated its working-set and called
``get_image_path`` per entry. The daemon saw a stream of
independent requests with no "session" concept; its
progress-reporting could only count the on-disk count
against the index's total — a structural over-count.

This release flips the iteration. Peer hands the daemon a
single list of paths via ``POST /v1/projects/<lang>/cawl/
prefetch``; daemon spawns a background worker that iterates
the list, warms each path through ``get_image_path`` (cache
hit or GitHub fetch), and tracks per-job ``requested`` /
``completed`` / ``failed`` counters. ``cache_status`` now
reads from that job state when a prefetch has run, so the
progress bar reflects work the peer actually wants done.

**Endpoints.**

- ``POST /v1/projects/<lang>/cawl/prefetch`` with body
  ``{paths: [...]}`` — kicks off the worker, returns
  ``{requested, completed, finished}`` immediately. Idempotent:
  a second call with the same paths-set against an active job
  returns the existing state; a call with a different set
  replaces it.
- ``GET /v1/projects/<lang>/cawl/cache_status`` — unchanged
  wire shape (``{image_repo, cached, total}``). When a
  prefetch is active or completed for the repo, ``cached`` =
  job's ``completed`` and ``total`` = job's ``requested``,
  giving a progress bar that ends at 100%. Falls back to the
  old "on-disk vs. index" semantics when no prefetch has run.

**Client wrapper.** ``cawl_prefetch(langcode, paths)`` returns
the initial state dict; peers chain it with the existing
``cawl_cache_status(langcode)`` poll for progress display.

**On-demand path untouched.** ``CAWLHandle(...).open_read``
still works for any individual image; daemon serves from
cache or fetches on demand exactly as before. The new path
is just for bulk warming; individual reads share the same
cache.

**Daemon UI banner.** Updated to reflect the same numbers —
when a prefetch is in flight, the daemon UI's top-banner
shows "Caching images: M / N (network in use — please stay
online)" against the job's actual counts. Auto-hides when
``cached >= total``.

**Logging.** Removed ``_touch_project`` from the
``cache_status`` handler — a 1 Hz status poll isn't "user is
working on this project" signal and was flooding logcat with
``[recent] _touch_project`` lines.

**Peer adoption.** Contract updated at
``CLIENT_INTEGRATION.md`` § 10 with the new wiring shape and
the rationale ("daemon-driven, not peer-driven"). Peers
calling ``CAWLHandle.open_read`` in a loop for bulk warming
should migrate to ``cawl_prefetch`` for accurate progress;
their on-demand single-image calls don't need to change.

### azt_collabd 0.41.10 — CAWL cache-status: top-banner placement + 1 Hz poll + memoised counts

Follow-on to 0.41.9. Three iterations on the cache-status
indicator after first contact with the user:

**Banner moves to the top of the SettingsScreen.** Previously
the status line was inside the bottom Status section, which by
design is low-attention "what's going on" diagnostic info. The
caching indicator's whole purpose is the *opposite* — grab
attention so the user doesn't disconnect network. Now lives
as a pinned BoxLayout banner directly under the TopBar, above
the ScrollView, so it can't scroll out of view. Accent-coloured
background + bold text + an explicit "please stay online"
clause in the message.

**Poll interval drops to 1 Hz.** 5-second polls made the
counter look broken — 10+ images cached per refresh produced
visible-but-jumpy progress. 1 Hz feels live.

**``cache_status`` is now near-zero cost per call.** The 1 Hz
poll needed this: the previous implementation did one
``os.walk`` over the daemon-owned images dir (~50-100 ms for
the canonical 1700-image set) plus one ``_read_cached_index``
JSON parse per call. At 1 Hz that's 5-10% daemon CPU, hot
enough to notice on a phone.

Memoisation:

- ``_cached_image_count[repo]`` is an in-memory counter. Lazy
  ``os.walk`` seeds it once per repo per daemon process;
  ``_note_image_cached(repo)`` increments it on every
  successful image-fetch + cache-write. Reset on daemon
  restart (re-seeds on first call). No invalidation needed —
  the counter only grows because cached images aren't
  removed.
- ``_total_count_cache[repo]`` is mtime-keyed. The
  ``_count_index_images`` lookup parses the cached index file
  once per (repo, index mtime); subsequent calls return the
  cached count. Invalidates automatically when ``get_index``
  refreshes and rewrites the cache file.

Net: cold-start ``cache_status`` is one ``os.walk`` + one JSON
parse; steady-state is a dict lookup + an ``os.path.getmtime``.

### azt_collabd 0.41.9 / azt_collab_client 0.41.9 — CAWL cache-status endpoint

The first cold-cache prefetch on the canonical
``kent-rasmussen/images_CAWL`` repo pulls ~1660 image binaries
sequentially through the daemon; on a typical mobile connection
this takes minutes during which the user has no in-app
indication that the daemon is using their network. They might
naturally disconnect Wi-Fi between gestures and end up with a
half-warm cache (every uncached image then has to fetch on
demand, which is exactly what the prefetch was avoiding).

This adds a project-scoped cache-status endpoint peers can
poll on a short interval to drive a "Caching images: M / N"
indicator:

- Daemon: ``GET /v1/projects/<lang>/cawl/cache_status`` →
  ``{ok, image_repo, cached, total}`` where ``cached`` is the
  count of image files in the on-disk cache for the project's
  resolved image_repo and ``total`` is the image-shaped index
  entries.
- Client: ``cawl_cache_status(langcode)`` wrapper returns the
  same shape as a dict (no Result wrapper — this is a status
  query, not a state-changing op). Empty values on any
  transport / not-found failure so the peer can poll
  unconditionally without exception handling.

Cost: one ``os.walk`` over the daemon-owned images dir per
poll. Bounded by total_count (~1700 files for the canonical
set); fast enough for a 5-second poll interval. No network.

**Daemon UI mirror.** The settings screen
(``python -m azt_collabd ui`` / ``open_server_ui()``) also
surfaces this progress: a small line in the Status section
that says "Caching images: M / N (network in use)" while a
prefetch is running, auto-hides when the cache catches up.
Polled on a 5-second ``Clock.schedule_interval`` while the
SettingsScreen is visible; cancelled in ``on_leave`` so it
doesn't wake the daemon for a screen the user can't see.

**Peer-side adoption.** The recorder / future viewer should
mirror the same indicator on their own loading screen so
users see the progress without navigating into Sync Settings.
Contract documented in
``azt_collab_client/CLIENT_INTEGRATION.md`` § 10 with the
copy-paste shape.

### azt_collabd 0.41.8 — CAWL: drop daemon-side offline backoff (peer has a circuit breaker already)

0.41.4 added a daemon-side 60s offline backoff that suppressed
``[cawl] image fetch failed`` log spam when a peer iterated a
~1700-image set on a device with no network. After diagnosis
of an "55 images succeed, then 10 fail silently" pattern in
the wild, the backoff was actively making things harder to
debug: when the daemon went silent it was impossible to tell
from the peer side whether a fetch had been attempted at all
or whether the daemon had short-circuited on a stale backoff
window. The peer already has its own circuit breaker that
suppresses pulls after N consecutive failures, so the daemon
log-spam concern was solved peer-side anyway.

This release rips out the daemon-side backoff entirely.
``get_image_path`` and ``get_index`` now attempt the fetch on
every cache miss (lock-coalesced, as before) and emit a
verbose log line per failure. The peer's circuit breaker (in
``lift.py: _CAWLImageResolver._pull``) is the right place for
the "stop trying after N failures" policy.

Removed: ``_OFFLINE_BACKOFF_SECONDS``, ``_offline_state_lock``,
``_offline_until``, ``_offline_suppressed``,
``_is_in_offline_backoff``, ``_note_fetch_failure``,
``_note_fetch_success``. The ``http.client.HTTPException``
catch added in 0.41.4 stays — InvalidURL et al. still must not
escape uncaught.

### azt_collabd 0.41.7 / azt_collab_client 0.41.7 — atomic_open_write via FD + finalize (Binder cap on writes)

Same Binder per-transaction cap (~1 MB) that broke the CAWL
index read in 0.41.2 also breaks the LIFT atomic write for
projects whose LIFT exceeds ~700 KB (base64 inflates 1.33× and
the JSON envelope blows past the cap). Symptom is identical:
``ContentResolver.call`` Bundle drops on the way to the
daemon, transport raises, ``atomic_commit_bytes`` returns
``SERVER_UNAVAILABLE``, ``_UriAtomicWriteFile.commit`` raises
``IOError``, the audio-save path (or any other write through
``LiftHandle.atomic_open_write``) fails. The user-visible
break is "stopping a recording loses the entry"; the daemon
never sees the call so there's no daemon-side log.

**Two-phase write.** Bytes now cross the IPC boundary via the
ContentProvider FD path (no Binder size cap), then a tiny
RPC finalizes the atomic rename under ``project_lock``:

1. Peer generates ``token = secrets.token_hex(16)``, opens
   ``content://<auth>/<lang>/_atomic_pending/<token>`` for
   write via ``ContentResolver.openFileDescriptor``, writes
   the buffered bytes through that kernel FD. The daemon's
   ``_resolve_path`` routes ``_atomic_pending`` to
   ``<working_dir>/.azt_atomic_pending/<token>`` (new write-
   only route, token-validated against
   ``^[A-Za-z0-9_-]{1,64}$``).
2. Peer calls ``POST /v1/projects/<lang>/atomic_finalize``
   with ``{token, path}``. The daemon validates the path
   against the same whitelist
   ``atomic_commit`` uses (``<file>.lift`` /
   ``audio/<file>`` / ``images/<file>``), reads the pending
   file for size + sha256, then ``os.replace``s it under
   ``project_lock``. Returns ``ATOMIC_COMMITTED`` with the
   same params shape as ``atomic_commit_bytes``.

**Atomicity preserved.** The rename still happens under the
project lock so concurrent peer-vs-peer / peer-vs-merge
writers can't tear the destination. Phase 1 writes to a
unique per-token scratch path so two concurrent peers don't
collide on the pending file either. The only new failure
surface is "phase 1 succeeds, phase 2 fails": the scratch
file may linger under ``.azt_atomic_pending/`` on the
daemon. No automatic GC yet — operationally OK since the
total scratch volume is bounded by recent failed writes.
The daemon best-effort unlinks on rename failure.

**Backward compatibility.** ``_UriAtomicWriteFile.commit``
falls back to the legacy single-RPC ``atomic_commit_bytes``
path if phase 1 raises (pre-0.41.7 daemon that doesn't know
``_atomic_pending``) or phase 2 returns ``SERVER_ERROR``
(daemon missing the route). The legacy path still works
against any 0.36.0+ daemon for payloads under the cap, so
small-LIFT projects don't regress on a mixed-version
deployment.

**Where the bump goes.** The daemon side ships in the server
APK. The client-side rewrite of ``_UriAtomicWriteFile.commit``
+ the new ``atomic_finalize_pending`` wrapper lives in
``azt_collab_client`` — peer apps bundle this at build time,
so peers must be rebuilt to pick up the new write flow. Once
a peer is on 0.41.7+, atomic LIFT writes of any size up to
~10 MB cross cleanly.

### azt_collabd 0.41.6 — CAWL: literal-%20 in filename + drop defensive url-field re-encode

Surfaced once 0.41.5 got us past the flat-basename resolution
to a real fetch URL: a subset of canonical ``kent-rasmussen/
images_CAWL`` filenames literally contain ``%20`` as part of
the filename (not as URL encoding for a space). The actual
on-disk filenames look like
``2d%20minimalistic%20black%20and%20white%20line%20art%20of%20right%20elbow__bw.png``.

``Uri.getPath()`` on Android URL-decodes once, so the Python
side receives the literal-character filename — including the
literal ``%20`` substrings. The previous
``quote(rel_path, safe='/%')`` preserved those ``%``
characters unchanged into the URL, so GitHub decoded ``%20`` →
space and looked for a file with literal spaces, which doesn't
exist — 404.

Fix: ``quote(rel_path, safe='/')`` (drop ``%`` from safe). The
``%`` in literal-``%20`` filenames now encodes to ``%25``,
producing ``%2520`` in the URL. GitHub decodes once → literal
``%20`` → matches the actual filename on disk.

Also removed the defensive ``url``-field re-encoding in
``_h_cawl_index`` that 0.41.3 added. With the new encoding
rules, ``_fetch_index_from_github`` now emits correctly-
encoded ``url`` fields, and the defensive re-encode would
double-encode them (``%2520`` → ``%252520``) and break peers
that actually use ``entry['url']``. The current Stage-2 peers
use ``CAWLHandle`` (which goes through the daemon's fetch
path, not the per-entry ``url``), so removing the defensive
layer doesn't regress anything we ship.

**Decision log: idempotent encoding vs. canonical encoding.**
0.41.4's ``safe='/%'`` was an attempt at idempotent encoding —
"if input is already encoded, don't double-encode." That logic
is fundamentally ambiguous: ``%20`` in the input means either
"encoded space" or "literal ``%20`` in the filename" and we
can't tell from input alone. The canonical-encoding rule
(always encode ``%`` to ``%25``) gives consistent semantics:
peer-side input is always literal-character (URI decoding
gives that for free), daemon always encodes once for HTTP.
Peers that want to pass pre-encoded paths are broken under
this model — they shouldn't.

### azt_collabd 0.41.5 — CAWL: flat-basename → nested-path resolution via index

The canonical ``kent-rasmussen/images_CAWL`` repo keeps images
under category subdirs (``0001_body/<basename>.png``,
``0002_head/<basename>.png``, …). A peer parsing the index
typically extracts a CAWL identifier + a flat basename, then
calls ``CAWLHandle(langcode, basename).open_read()`` with just
the basename — it doesn't need to track the category prefix
because every category is part of the same CAWL set.

Before this fix the daemon would receive that flat basename
and ask GitHub for ``HEAD/<basename>.png`` (top level) — which
returns 404 because the file actually lives at
``HEAD/0001_body/<basename>.png``. After the offline-backoff
kicked in on the first 404, all subsequent image fetches in
the same minute also went silent → user-visible "no images."

New helper ``_resolve_basename_via_index(repo, rel_path)``:
if ``rel_path`` is a flat basename (no ``/``) and the index
has exactly that basename under some nested path, the daemon
canonicalizes to the nested path before computing the cache
target / fetch URL. Both the on-disk cache and the GitHub
request use the canonical nested path; subsequent flat-basename
requests for the same file hit the cache directly because the
canonicalization is deterministic.

If the index isn't cached yet, ``_resolve_basename_via_index``
returns ``rel_path`` unchanged so the network fetch attempt
fails honestly (rather than silently rewriting to a wrong
path). The index seed is bundled in the APK, so this only
matters in the rare case where the seed is missing and a
network fetch hasn't run yet.

### azt_collabd 0.41.4 — CAWL: SSL via certifi + URL encoding + offline-backoff coalescing

Three related image-fetch fixes that surfaced once 0.41.3's
slim index let the peer actually request binaries.

**URL encoding for paths with spaces / unsafe chars.** Both the
per-file ``url`` field in ``_fetch_index_from_github``'s emitted
index and the request URL in ``_fetch_image_bytes_from_github``
now percent-encode the path component. ``safe='/%'`` so the
encoding is idempotent (won't double-encode an already-encoded
input). CAWL filenames commonly include spaces / commas /
parens; raw URLs containing those raise
``http.client.InvalidURL`` at ``_validate_path`` time, before
the request goes out. The except clause in ``get_image_path``
also now catches ``http.client.HTTPException`` (parent of
``InvalidURL``); previously it only caught ``OSError`` /
``URLError`` and an InvalidURL escaped uncaught past the
offline-backoff handler, turning into a Java-side
``FileNotFoundException`` with no peer-visible
``[cawl] image fetch failed`` log.

The rest of this entry covers two related fixes that ride on
the same release:

Two related image-fetch fixes that surfaced once 0.41.3's slim
index let the peer actually request binaries.

**SSL bundle on Android.** ``_fetch_index_from_github`` and
``_fetch_image_bytes_from_github`` were using raw
``urllib.request.urlopen`` without calling ``net._ensure_ssl()``
first. Every other network site in the daemon does call it; the
CAWL module was the lone holdout. On p4a Android (no system CA
store) this manifested as ``SSL: CERTIFICATE_VERIFY_FAILED``
for every image fetch. Fix: both call sites now call
``_ensure_ssl()`` before the urlopen. The patch is idempotent
and globally monkey-patches ``ssl._create_default_https_context``
to use certifi's bundle, so once it has run any stdlib HTTPS
works.

**Offline backoff (per-process, shared between index + image
fetches).** When a connect-class urllib error fires
(URLError / OSError / TimeoutError), the daemon now enters a
60s cooldown during which subsequent CAWL fetch attempts
short-circuit silently. Without this, a peer iterating a
~1700-image set on a fully-offline device (or one with
broken DNS / SSL) spammed logcat with 1700 near-identical
``[cawl] image fetch failed`` lines, drowning real signal.
Coalesced semantics:

- First failure in a fresh window → one verbose log line
  identifying the repo + cause.
- Subsequent failures in the same window → silent skip
  (no network attempt, no log).
- Any successful fetch → backoff cleared immediately + one
  ``[cawl] network recovered`` log with the suppressed count.

The window is per-daemon-process module-state; restart
clears it. ``_OFFLINE_BACKOFF_SECONDS = 60`` is tuned for
"long enough that a 1700-image swipe-prefetch loop quiets
down, short enough that the user reconnecting wifi gets
images within a minute". Index lookups in the window serve
from cache (stale OK) per the existing fallback policy.

### azt_collabd 0.41.3 — slim CAWL index over JSON-RPC (image extensions only)

Server-side companion to 0.41.2's FD-route client fix. The
0.41.2 fix only helps peers that have rebuilt against the
updated ``azt_collab_client``; existing peer installs keep
calling ``cawl_index`` over the JSON-RPC path and keep
receiving an empty response because the ~1.5 MB index Bundle
exceeds the Binder per-transaction cap.

Per the recorder peer's 2026-05-13 filing
(NOTES_TO_DAEMON.md), the daemon now filters the index
response to ``.png`` / ``.jpg`` / ``.jpeg`` paths before
serializing. The canonical ``kent-rasmussen/images_CAWL`` repo
includes ~3700 non-image blobs (README, LICENSE, .gitignore)
that every peer's parser discards on receipt anyway —
filtering server-side just stops shipping bytes nobody uses.
Reduces the wire from ~5479 entries (~1.5 MB) to ~1700
entries (~470 KB), well under the Binder ceiling.

**Where the filter applies.** Only the JSON-RPC dispatch
(``_h_cawl_index``). The file-route URI
(``<lang>/cawl/index.json`` via ContentProvider, which 0.41.2
clients use on Android) still serves the raw cache file
unfiltered — file FDs have no Binder size cap, so there's no
reason to slim, and the peer self-filters on extension in
either case. So:

- Pre-0.41.2 peer + 0.41.3 daemon → JSON-RPC path, ~470 KB,
  fits, peer gets a populated index without rebuilding.
- 0.41.2+ peer + 0.41.3 daemon → FD path, full index,
  unaffected.
- Pre-0.41.2 peer + pre-0.41.3 daemon → JSON-RPC path, full
  index, Binder drops the Bundle, peer reads empty (the
  regression).

**Decision log: filter at serve, not at cache.** The on-disk
cache (``$AZT_HOME/cawl/<owner>/<repo>/index.json``) keeps
the canonical full set GitHub returned. Cache stays repo-
faithful; serve-time filter is cheap and reversible. A
future endpoint that wants the full set (admin UI, indexing
tool) can read the cache directly.

### azt_collabd 0.41.2 / azt_collab_client 0.41.2 — CAWL index over file FD on Android (Binder 1 MB cap)

Patch fix: the daemon was serving the populated CAWL index
(``files=5479`` in the success log added in 0.41.1), but the
peer's ``cawl_index(langcode)`` wrapper read ``{}`` — peer
logged ``[cawl] _load: ... repo='' files=0`` and never
requested any images. Root cause: ``ContentResolver.call``
ships responses as a ``Bundle`` over Binder, which caps
single transactions at ~1 MB. The populated index
(~1.5 MB with 5000+ entries × long GitHub raw-content URLs)
exceeds the cap; the Bundle is dropped on the way back, the
peer's transport raises (caught by the wrapper as
``ServerUnavailable``), and the wrapper returns ``{}``. The
daemon-side success log fires regardless because the handler
ran — the gap is in the IPC return trip, not the dispatch.

**Fix.** On Android, ``cawl_index`` now reads the on-disk
index file directly via the ContentProvider's existing file
route (``<lang>/cawl/index.json``). ``ContentResolver.openFile
Descriptor`` returns a kernel FD with no Binder size cap; the
peer reads the JSON bytes and parses locally. The daemon's
``_resolve_cawl_path`` already populated the cache via
``cawl.get_index`` before returning the path, so the file is
guaranteed present (seed-on-cold-cache covers the
no-network-on-install case). Desktop loopback HTTP has no
such cap and keeps the JSON-RPC path.

**Client (azt_collab_client):**

- ``cawl_index(langcode)`` now branches on platform:
  Android → file-route via new ``lift_io._cawl_index_via_fd``
  helper; desktop → existing ``GET
  /v1/projects/<lang>/cawl/index`` over loopback HTTP.
  Empty-on-failure contract preserved on both paths.
- New ``lift_io._cawl_index_via_fd(langcode)`` — opens
  ``content://<authority>/<lang>/cawl/index.json`` via
  ``_open_content_uri``, reads, parses. Same URI shape
  ``CAWLHandle`` uses for image bytes.

**Decision log.** Not changing the daemon-side wire shape;
the JSON-RPC endpoint stays correct and serves desktop. The
asymmetry (HTTP path on desktop, FD path on Android) lives
on the client side because that's where the Binder cap is
visible. A future symmetric refactor could move both peers
to the FD path uniformly, but desktop has no FD provider
available without adding a new loopback file-serving
endpoint — not worth the surface area for an IPC-layer
workaround.

### azt_collabd 0.41.1 / azt_collab_client 0.41.1 — CAWL nested paths + success-path logging

Patch fix: 0.41.0's CAWL image fetching silently rejected any
``rel_path`` containing ``/``, which is exactly the shape the
canonical ``kent-rasmussen/images_CAWL`` repo uses
(``0001_body/foo.png``-style category subdirs). Net effect:
the seed index was served fine, but every per-image request
returned silently → peers saw no images, no daemon log line
recorded the failure. This release accepts nested rel-paths
and adds success-path logging so the next similar gap is
visible from logcat alone.

**CAWL daemon (azt_collabd/cawl.py):**

- ``_looks_safe_basename`` → ``_looks_safe_rel_path``. Accepts
  ``/`` between components; rejects ``..``/``.``, absolute
  paths, backslashes, and empty components.
- ``get_image_path`` now takes ``rel_path`` (not ``basename``).
  Composes the on-disk target under
  ``<cache_root>/<repo>/images/<rel_path>``; verifies
  containment with ``realpath`` + ``commonpath`` (belt-and-
  braces against symlink tricks). Creates intermediate cache
  subdirs on first write.
- ``_fetch_image_bytes_from_github`` URL-encodes each path
  component for the raw URL (``urllib.parse.quote(path,
  safe='/')`` — keeps slashes between components intact;
  encodes spaces, commas, parens, etc. that CAWL filenames
  commonly contain).

**Transport routing:**

- ``android_cp._resolve_cawl_path`` accepts 2+ segments under
  ``images/`` (was strict ``[images, basename]``). Joins
  remaining segments back into the rel-path that
  ``cawl.get_image_path`` validates.
- ``server._match_cawl_image_path`` accepts 7+ segments; per-
  component URL-decodes via ``urllib.parse.unquote``; rejects
  post-decode traversal tricks (``%2E%2E`` → ``..`` and
  ``%2F`` → ``/`` inside a single segment).

**Client (azt_collab_client/lift_io.py):**

- ``CAWLHandle(langcode, rel_path)`` — the ``basename`` arg
  renamed to ``rel_path``. ``handle.basename`` kept as a
  read-only alias for back-compat with peer log lines.
- ``CAWLHandle.open_read`` URL-encodes the rel-path with
  ``urllib.parse.quote(safe='/')`` before composing the
  ``content://`` URI or the loopback HTTP URL. Slashes
  between components preserved; unsafe characters percent-
  encoded.

**Success-path logging — new in both endpoints:**

- ``[cawl] served index for repo=… langcode=… files=N`` on
  every successful ``_h_cawl_index``. ``files=0`` is the
  early-warning signal that something's upstream-wrong
  (empty seed, mis-resolved repo, …).
- ``[cawl] served image repo=… path=… bytes=…`` on every
  successful ``_h_cawl_image_bytes``.
- ``[cawl] image rejected: project_not_found / no_image_repo_configured``
  and ``[cawl] image unavailable: repo=… path=…`` on the
  refusal paths. The 0.41.0 bug went unseen because none of
  the success-or-rejection paths logged — only the
  network-fetch-failed path did.

**Tests:** new in ``tests/test_cawl.py``:

- ``test_get_image_path_accepts_nested_rel_path`` — regression
  test for the 0.41.0 bug.
- ``test_get_image_path_accepts_spaces_and_special_chars`` —
  CAWL filenames in the canonical repo have these.
- ``test_fetch_url_encodes_path_components`` — slashes
  preserved, unsafe chars percent-encoded.
- ``test_match_cawl_image_path_accepts_nested_path``,
  ``_url_decodes_components``,
  ``_rejects_traversal_post_decode``,
  ``_rejects_slash_in_decoded_segment``.
- ``test_h_cawl_image_bytes_serves_nested_rel_path`` —
  end-to-end through the binary handler.
- ``test_resolve_path_cawl_image_accepts_nested`` — through
  the ContentProvider routing.
- Existing path-traversal test updated to remove
  ``sub/file.jpg`` from the rejected-shapes list and add
  ``a/../b`` / ``foo//bar`` as new rejection cases.

**Floor:** patch bump. No wire-format change beyond accepting
strictly more rel-path shapes. Pre-0.41.1 daemons reject
nested paths silently; pre-0.41.1 clients that pass flat
basenames work against 0.41.1 daemons unchanged (the new
endpoints accept both).

## [0.41.0] - 2026-05-12

### azt_collabd 0.41.0 / azt_collab_client 0.41.0 — collaborator UI consolidation + QR share/scan

Project-bound actions (Grant collaborator, Share repo) move into
the daemon settings UI's SettingsScreen, bound to the
``last_project()`` the daemon already tracks. Peers shrink to
a single "Open Sync Settings" button (``open_server_ui()``) for
these flows — same pattern as the GitHub Connect / GitLab
forms already on this screen.

Plus a QR pair: the daemon UI generates a QR of the published
repo URL ("Share this repo"), and the picker's clone flow
scans QRs to pre-fill its URL textbox.

**Daemon UI (azt_collabd/ui/app.py):**

- New ``project_actions_row`` in SettingsScreen, gated on
  ``last_project()`` resolving to a project that has a
  ``remote_url``. Mutually exclusive with the existing
  ``publish_row`` (which is gated on no remote) — the user
  sees one "what can I do with this project" surface at a
  time, appropriate to the project's current state.
- ``Grant collaborator access`` button → invokes the shared
  ``grant_collaborator_popup(langcode=last_project())``. The
  popup itself already lives in
  ``azt_collab_client.ui.popups`` (no work needed there).
- ``Share this repo (QR)`` button → opens a new
  ``_show_share_repo_qr_popup`` that renders the remote URL
  as a QR via segno + a Kivy ``Image`` widget, with a "Copy
  URL" fallback that goes through ``kivy.core.clipboard``.
- ``SettingsScreen.publish()`` updated for the 0.40.0 wire —
  no longer passes ``contributor=`` to ``init_project``
  (daemon reads from store). Adds ``S.CONTRIBUTOR_UNSET`` to
  the publish-failed-codes set so the publish msg routes
  correctly when the user hasn't entered their name yet.
- ``CollabUIApp.font_name`` now an instance attribute (was
  a local in ``build()``) so screens can pass it to shared
  popups for visual consistency (CharisSIL across daemon UI
  surfaces).

**QR generation (segno):**

- New requirement: ``segno`` in
  ``server_apk/buildozer.spec`` (and ``.tmpl``). Pure-Python,
  ~50 KB, no native deps. PNG output uses Pillow which Kivy
  already pulls in.
- ``_show_share_repo_qr_popup(url, langcode, font_name)`` —
  module-level helper in ``app.py``. Generates the QR with
  ``error='M'`` (15% correction, good camera tolerance) and
  ``scale=8`` (~250 px square in the popup). Falls back to
  ``_show_segno_missing_popup`` on desktop installs where
  segno isn't pip-installed.

**QR scan (zxing-android-embedded):**

- New requirement: ``com.journeyapps:zxing-android-embedded:4.3.0``
  in ``android.gradle_dependencies`` (server APK only). ~500 KB
  AAR; pulls in the camera-preview CaptureActivity + barcode
  decoder. Android-only.
- New permission: ``CAMERA`` in ``android.permissions``. ZXing
  requests the runtime grant itself at CaptureActivity launch;
  we only need the manifest entry.
- New module ``azt_collab_client/ui/qr_scan.py``:
  ``scan_qr(on_result, on_cancel, prompt)`` launches ZXing's
  ``IntentIntegrator``, reads ``SCAN_RESULT`` from
  ``onActivityResult``, marshals the callback to the Kivy main
  thread. ``available()`` is the cheap probe peers use to gate
  the UI affordance on platforms where ZXing isn't bundled.
- ``clone_url_popup`` (the picker's clone-by-URL flow) grows a
  "Scan QR" button next to the URL textbox when
  ``qr_scan.available()`` is True. On scan success the
  textbox is filled with the decoded URL and the existing
  ``_refresh_label_from_url`` derives the langcode. Desktop
  / no-ZXing builds keep the original "paste URL" UI.

**Floor:** no bumps. Daemon UI additions are server-side only;
peer wire surfaces unchanged. Pre-0.41 daemons / clients
interoperate normally (peers just don't see the new daemon-UI
buttons, which is fine — those weren't there before either).

**Recorder follow-up (deferred to a NOTES_TO_PEERS item back
to the recorder team).** Now that the consolidated surface
exists in the daemon UI, the recorder's CollabScreen Publish +
Grant collaborator sub-screens become redundant. Strip-out
follows Phase 3 from NOTES_TO_DAEMON.md (don't combine with
this release — peer that strips before daemon grows the
replacement loses the feature entirely until both sides
converge).

### azt_collabd 0.40.0 / azt_collab_client 0.40.0 — commit author moves to daemon; device-name disambiguator

Two coordinated changes, one release:

1. **Contributor name is strictly daemon-owned now.** Peers no
   longer pass a commit-author name on the wire; daemon endpoints
   ignore any ``body['contributor']`` and read the stored value
   directly. If no name is set, commit-issuing endpoints refuse
   with the new ``S.CONTRIBUTOR_UNSET`` status — peers route the
   user to the daemon settings UI (``open_server_ui()``) to set
   their name rather than silently producing meaningless
   ``"Recorder"`` commits.
2. **New ``device_name`` field** disambiguates commits when the
   same human contributes from multiple devices. The git author
   email slot becomes ``<safe_contributor>@<safe_device>`` so
   GitHub's author-aggregation still groups by person, while
   ``git log --format='%ae'`` differentiates by device. Auto-
   populates from the OS on first read (Android:
   ``Settings.Global.DEVICE_NAME`` → ``Build.MANUFACTURER +
   MODEL``; desktop: ``socket.gethostname()``); user can override
   via the settings UI for a friendlier label.

**Why this lands together.** Both are corollaries of the
"daemon is the sole authoritative source for per-user state"
rule (NOTES_TO_DAEMON.md, recorder 1.41.3 filing). Pre-0.40 the
contributor name was duplicated across peer and daemon, with
the peer's pass-through silently winning even when the user
typed a name in the daemon UI; the literal ``"Recorder"``
default in the client wrapper turned every peer that didn't
override it into a commit signed "Recorder". 0.40 closes both
issues by removing the wire surface and replacing the
placeholder ``@device`` email slot with a real disambiguator.

**Wire changes:**

- ``POST /v1/projects/init`` — ignores ``body['contributor']``.
- ``POST /v1/projects/<lang>/sync`` — ignores ``body['contributor']``.
- ``POST /v1/projects/<lang>/sync_async`` — ignores
  ``body['contributor']``; the enqueued job runs against the
  stored contributor at exec time. If unset, scheduler returns
  ``Result(CONTRIBUTOR_UNSET)`` which peers see via
  ``poll_job(job_id)``.
- ``GET /v1/config/device_name`` — new. Returns the stored or
  auto-detected device name (always non-empty after first read).
- ``POST /v1/config/device_name`` — new. Sets / clears the
  override. Whitespace stripped; empty clears and re-triggers
  autodetect on next read.

**Client API changes:**

- ``init_project(working_dir, remote_url, branch='main')`` —
  ``contributor`` kwarg removed.
- ``sync_project(langcode)`` — ``contributor`` parameter
  removed.
- ``request_sync(langcode)`` — ``contributor`` parameter
  removed.
- New ``get_device_name()`` / ``set_device_name(name)``
  wrappers, exported in ``__all__``.
- New ``S.CONTRIBUTOR_UNSET`` status code, translation in
  ``translate.py``.

**Daemon-side changes:**

- ``store.resolve_contributor`` **removed**. Was the host of the
  ``'Recorder'`` fallback. Any in-tree caller that still imports
  it fails at import time — fail-loud.
- ``store.get_device_name`` / ``store.set_device_name`` new.
  Auto-populates on first read; persists the autodetect so
  subsequent reads are stable.
- ``repo._default_author(contributor, device_name=None)`` —
  ``device_name=None`` lazy-looks-up via ``store.get_device_name()``;
  ``''`` explicitly skips the lookup (deterministic test
  output). Email slot is ``<safe_contributor>@<safe_device>``;
  the literal ``@device`` placeholder is gone.
- ``scheduler.Job`` no longer carries a ``contributor`` field;
  ``request_sync(langcode)`` signature drops the second
  positional. Pre-0.40 ``jobs.json`` entries with the field
  decode cleanly (ignored on load).
- ``_h_init_project`` / ``_h_project_sync`` refuse upfront with
  ``S.CONTRIBUTOR_UNSET`` when ``store.get_contributor()`` is
  empty. ``_h_project_sync_async`` enqueues unconditionally;
  the scheduler's exec-time re-check at ``_run_sync`` is the
  defence-in-depth.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bumps. Pre-0.40 clients that still pass ``contributor`` in the
body get their value silently ignored (strict improvement —
the override they sent was the bug). Pre-0.40 daemons talking
to a 0.40 client work unchanged (the client just doesn't send
the field). No corruption surface, no forced cut-over.

### azt_collabd 0.39.0 / azt_collab_client 0.39.0 — per-project `repo_slug` field

Closes the recorder 1.41.3 ask from
``NOTES_TO_DAEMON.md``: the GitHub-repo-name override that the
publish path uses now lives on the daemon's project record, not
in peer prefs.

**Why this is needed.** Most projects publish to a repo named
after the project's ``langcode``, and the daemon's
``projects.json`` key is the right value to display. But a user
can legitimately want a *different* repo name (vanity slug,
project-style naming convention, collision avoidance with an
existing GitHub repo) without changing the LIFT
``<form lang="…">`` tag. Pre-1.41.3 the recorder persisted that
override as a suite-wide ``peer_pref`` scalar
(``collab_langcode``), which was wrong on two counts:
peer-prefs are global but the override is per-project, and
peer-side storage of project-identity data violates the
no-daemon-owned-caches rule (also documented in
``NOTES_TO_DAEMON.md`` "Daemon is now the sole authoritative
source"). 1.41.3 dropped the peer-side mirror; this release
gives the data its canonical daemon-side home.

**Wire shape:**

- ``Project.repo_slug`` field (string, default empty).
  Returned by ``open_project`` / ``project_status`` /
  ``list_projects`` so peers can read it without an extra
  round-trip.
- New endpoint ``POST /v1/projects/<lang>/repo_slug`` —
  body ``{repo_slug: '<name>'}``. Whitespace stripped before
  persist. Empty string explicitly clears (callers fall back
  to using ``langcode``). 404 on unknown project, 400 on
  missing field.
- New client wrapper ``set_repo_slug(langcode, slug)`` —
  returns the updated ``Project`` or ``None`` on transport
  failure / unknown project, same shape as
  ``set_cawl_image_repo``.
- ``register_project`` now accepts ``repo_slug=…`` for the
  initial-creation path (alongside the existing
  ``cawl_image_repo`` kwarg). ``None`` preserves any existing
  value; ``''`` explicitly clears.

**Default-semantics rule for callers:** unset / empty
``repo_slug`` is the typical case — callers should treat that
as equal to ``langcode``. The daemon does NOT auto-fill the
field with the langcode; the field stays empty until the user
explicitly overrides. That keeps "did the user actually choose
a different name?" decidable from the data alone.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bumps. The field is purely additive — pre-0.39 daemons don't
emit it, the client-side dataclass defaults to ``''`` for
forward-compat. A 0.39 client calling ``set_repo_slug``
against a pre-0.39 daemon gets ``None`` (the endpoint returns
404), which is the same failure shape every other setter
wrapper uses.

**Removed from NOTES_TO_DAEMON.md:** the "Per-project
repo-slug override (publish path)" entry — shipped.

### azt_collabd 0.38.1 / azt_collab_client 0.38.1 — CAWL index seed (install-day-no-network)

Closes the install-day-no-network gap that Stages 1+2 (0.38.0)
couldn't solve on their own: a freshly-installed device that has
never reached GitHub now has a bundled CAWL index to serve, so
peers can render illustrations on first launch.

**Daemon side:**

- New ``_seed_index_if_bundled(repo)`` in ``azt_collabd/cawl.py``.
  ``get_index(repo)`` calls it before going to the network when
  the on-disk cache file is missing — copies the bundled asset
  from ``azt_collabd/data/cawl/<owner>/<repo>/index.json`` into
  ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` if it exists.
- Silently no-ops when (a) no seed is shipped for the requested
  repo, (b) the cache already has data, (c) the bundled JSON is
  malformed. The first case is the common one: only the
  suite-canonical repo is typically seeded; fork / per-project-
  override repos keep their old install-day behaviour (no
  illustrations until the network fetch lands).
- The seed is treated as an ordinary cache entry once written:
  fresh-within-TTL → serve directly; past-TTL → attempt a
  network refresh and fall back to the seed if offline (the
  pre-existing stale-cache fallback). When the device first
  gets online, the next refresh overwrites the seed with current
  data.

**Bundle layout:**

```
azt_collabd/data/cawl/
    <owner>/<repo>/
        index.json
```

Subdirectory name matches the on-disk cache layout exactly.
``azt_collabd/data/cawl/generate_seed.py`` is the maintainer
script: with no args it refreshes the suite-canonical seed
(daemon-global default ``kent-rasmussen/images_CAWL``); pass
``owner/repo`` or set ``AZT_CAWL_IMAGE_REPO`` for a fork /
non-canonical image set. Uses the same
``cawl._fetch_index_from_github`` codepath the daemon does at
runtime, writing to the right directory.
``azt_collabd/data/cawl/README.md`` documents the wire shape +
when to re-run.

The daemon-global ``cawl_image_repo`` default is no longer empty
— recorder 1.41.3 removed its own hard-coded fallback under the
no-daemon-owned-caches rule, so the daemon is now the sole
source of this slug at runtime. Default set to
``kent-rasmussen/images_CAWL`` to preserve the recorder's
pre-1.41.3 behavior; fork shipping a different CAWL set should
override via ``azt_collabd.configure(cawl_image_repo=…)`` or
``AZT_CAWL_IMAGE_REPO``.

**Buildozer:**

- ``server_apk/buildozer.spec`` and ``buildozer.spec.tmpl`` add
  ``json`` to ``source.include_exts`` so the bundled seed lands
  in the APK. No other build-config changes; new seed
  directories under ``azt_collabd/data/cawl/`` are picked up
  automatically.

**What's NOT bundled, and why:**

The image binaries themselves are explicitly **not** in the
seed. 1701 images at 50–200 KB each ≈ 100–300 MB per APK
release — wrong trade for a one-time first-launch UX gain.
Image rendering on day-one without connectivity simply doesn't
happen; the user gets illustrations once the device first
reaches ``raw.githubusercontent.com``, with the daemon-side
lazy cache (shipped in 0.38.0) covering steady-state perfectly
fine. If a future session proposes "bundle the whole CAWL
image set in the APK", that's a re-litigation of a 2026-05-12
decision — answer is no.

**Floor:** patch bump (no wire-format change). The seed is
purely additive on the daemon side; pre-0.38.1 clients get
exactly the same wire shape from ``GET /v1/projects/<lang>/
cawl/index`` — they just benefit from a populated cache they
didn't have to fetch themselves.

### azt_collabd 0.38.0 / azt_collab_client 0.38.0 — CAWL Stage 2: per-project image_repo, image-binary RPC, first non-JSON endpoint

Completes the CAWL daemon-side migration that 0.37.0 started, and
corrects 0.37.0's daemon-global ``cawl_image_repo`` stopgap to a
per-project field on the Project record. CAWL is now a fully
daemon-owned suite-scoped resource: peers consume both the index
and the image binaries via the daemon, with one cache per repo
per device regardless of peer count.

**The reframing.** 0.37.0 used a daemon-global ``cawl_image_repo``
configured at daemon startup. That conflicted with the
"sole authoritative source" architectural invariant the recorder
1.41.3 just established (NOTES_TO_DAEMON.md): per-project
identity / configuration data belongs on the project record, not
in peer prefs and not in daemon-global config. Different projects
can legitimately point at different image sets (vanity fork,
culturally specific imagery, etc.) so the slug is a per-project
override with a daemon-global fallback.

**Project record (azt_collabd/projects.py):**

- New ``Project.cawl_image_repo`` field (string, default empty).
  Empty falls back to the daemon-global default; non-empty
  overrides for this project.
- ``register(..., cawl_image_repo=None)`` accepts the kwarg.
  ``None`` preserves any previously-set value across
  re-registration; empty string explicitly clears.
- New ``set_cawl_image_repo(langcode, repo)`` setter for the
  endpoint.
- Client-side ``Project`` mirror gains the field with default
  empty (forward-compat with pre-0.38 daemons that don't emit it).

**Cache module (azt_collabd/cawl.py):**

- ``get_index(repo)`` now takes a repo slug. Cache moves to
  ``$AZT_HOME/cawl/<owner>/<repo>/index.json`` so multiple
  projects pointing at the same image_repo share one cache
  directory.
- New ``get_image_path(repo, basename)``: lazy fetch from
  ``raw.githubusercontent.com``, cache at
  ``<owner>/<repo>/images/<basename>``, return absolute
  filesystem path. Path-traversal-safe basename validation.
  ``None`` when fetch fails and no prior cached copy exists.
- New ``resolve_image_repo(langcode)``: per-project value
  preferred; daemon-global ``config.cawl_image_repo()`` is the
  fallback for projects without an override.
- Lock-coalesced fetches keyed by cache file path (not module-
  wide), so two repos can fetch in parallel without
  serializing.

**Endpoints:**

- ``GET /v1/projects/<lang>/cawl/index`` (replaces the 0.37.0
  ``GET /v1/cawl/index``). Daemon resolves the project's
  cawl_image_repo internally; response carries
  ``index_repo`` alongside ``index`` so peers can see which
  repo answered.
- ``GET /v1/projects/<lang>/cawl/images/<basename>`` returns
  **raw binary image bytes**. First non-JSON endpoint on the
  loopback HTTP server; new ``_send_bytes`` handler bypasses
  JSON dispatch (the dispatch table stays JSON-only). Content-
  type derived from the file extension
  (``image/jpeg``/``png``/``gif``/``webp`` known; falls back
  to ``application/octet-stream``).
- ``POST /v1/projects/<lang>/cawl_image_repo`` setter for the
  per-project override. Body ``{cawl_image_repo: 'owner/repo'}``;
  empty string explicitly clears.

**ContentProvider (azt_collabd/android_cp/service.py):**

- ``_resolve_path`` extended with two new shapes:
  ``<lang>/cawl/index.json`` (3-seg, triggers lazy index fetch)
  and ``<lang>/cawl/images/<basename>`` (4-seg, triggers lazy
  image fetch).
- CAWL paths resolve to ``$AZT_HOME/cawl/<owner>/<repo>/...``
  (away from the per-project working_dir) so the dedup-by-repo
  property of the cache layer is preserved on Android.
- Write modes (``w``/``a``) rejected — peers don't write CAWL
  files. Returns ``None`` so the Java side surfaces
  ``FileNotFoundException``.

**Client side:**

- ``cawl_index()`` → ``cawl_index(langcode)``. The 0.37.0
  shape is gone; pre-0.38 callers must pass a langcode.
- New ``set_cawl_image_repo(langcode, repo)`` wrapper.
- New ``CAWLHandle(langcode, basename).open_read()`` —
  binary file-like for a CAWL image. Branches transport
  internally: Android opens the ContentProvider URI
  (zero-copy via kernel FD); desktop hits the loopback HTTP
  endpoint and returns ``io.BytesIO`` wrapping the response.
  Read-only (peers don't write CAWL images). Raises
  ``FileNotFoundError`` on 404 / no cached copy / fetch
  failure; raises ``ServerUnavailable`` on transport failure.
- All exposed via ``__all__`` and re-exported.

**What's left (not in 0.38.0):**

- ⏳ APK-bundled INDEX seed (independent piece; ~50 KB asset
  in ``server_apk/assets/cawl/index.json`` so install-day-no-
  network gets a populated index without GitHub access).
  Image binaries are NOT bundled — a 100–300 MB per-release
  payload is the wrong trade; lazy daemon caching covers the
  steady state. Filed in NOTES_TO_DAEMON.md.
- ⏳ Peer migration in the recorder (swap direct
  ``urllib.request.urlopen(raw_url)`` + per-peer cache for
  ``CAWLHandle.open_read()``; UI affordance to set
  ``cawl_image_repo`` per project). Lives in the recorder
  repo; not blocked on anything here.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
bump. The 0.38.0 endpoints are net-new; pre-0.38 clients
calling them get 404, the wrappers return ``{}`` / ``None``,
peers continue to use their pre-migration paths. Older clients
talking to a 0.38 daemon work unchanged — they just don't call
the new endpoints. The 0.37.0 ``GET /v1/cawl/index`` endpoint
is removed; no peer has adopted it yet (0.37.0 didn't ship a
real release).

**Bootstrap decline cadence: one-shot, not permanent.**
``bootstrap()``'s self-update decline mechanism (the "Not now"
button on the peer self-update prompt and the
already-declined branch of the server-too-old install prompt)
is now **one-shot**. Recording a decline suppresses exactly
the next launch's prompt and clears, so the cadence is
prompt → decline → skip → prompt → decline → skip → … rather
than the previous "never ask again for this version" shape.
A new upstream version still invalidates the stored value
the same way (exact-string compare).

Motivation: permanent decline-by-version painted users into
a corner where reconsidering required waiting for the next
upstream tag. One-shot gives the user a launch's breathing
room without trapping them. Implementation: new
``_consume_decline(repo, version)`` does the read-then-clear
in one step; ``_declined_version`` stays as a non-destructive
peek for tests / diagnostic use.

### azt_collabd 0.37.0 / azt_collab_client 0.37.0 — daemon-owned CAWL image-URL index cache

Moves CAWL image-URL index ownership from the peer to the daemon
to close out the 60/hr GitHub rate-limit symptom reported in
NOTES_TO_DAEMON.md (filed 2026-05-11). The fundamental reframing:
the index is *suite-scoped* shared infrastructure, not
peer-scoped, so it belongs on the daemon and peers consume it.

**Why this is needed.** Pre-0.37, each peer hit
``api.github.com/repos/<image_repo>/git/trees/HEAD?recursive=1``
directly on every project load and cached the result in a
per-peer in-memory dict. GitHub caps unauthenticated REST at
60 requests / hour / IP, which a dev rebuild loop, CI run, or
multi-peer device blows trivially. Once exhausted, the
resolver returns empty for the rest of the session and entries
without a locally-cached image render with no illustration.
Three structural failure modes flow from peer ownership:

1. **Rate limit exhaustion** — described above.
2. **Per-peer duplication** — N peers on the same device each
   do the same work; Android's sandbox prevents sharing even
   the cache file.
3. **Install-day-no-network** — a fresh install with no
   connectivity has no way to populate the index, so
   first-launch UX is "no illustrations" regardless of what's
   on disk.

The recommendation discussion (transcript 2026-05-12) ranked
three hosting fixes (bundle in APK / proxy through daemon /
sign with GitHub App token) and then re-framed: the deeper
problem is *where the cache lives*. Moving ownership to the
daemon serves the same data, removes the per-peer fan-out,
and lets the time-bounded refresh policy do the rate-limit
work once per device per day.

**Daemon side (`azt_collabd/cawl.py`):**

- New module owns ``$AZT_HOME/cawl/index.json``.
  ``get_index(force_refresh=False)`` returns the index dict,
  refreshing from GitHub on cache miss / past-TTL / explicit
  force. TTL is 24h (``_INDEX_TTL_SECONDS``).
- Lock-coalesced fetch: two peers calling ``get_index`` on a
  cold cache result in exactly one network round-trip; the
  second caller reads the freshly-written file.
- Stale-cache fallback: a network failure with a cached copy
  on disk returns the cached copy (even if past TTL). A
  network failure with no cache returns ``{}``. Peer code
  treats ``{}`` the same way it treated its pre-migration
  empty resolver dict, so there's no new "daemon failed"
  branch to write.
- Daemon stays naming-convention-agnostic. The wire shape is
  ``{repo, branch='HEAD', fetched_at, files: [{path, url}]}``.
  Peers do the filename → CAWL-identifier mapping themselves
  (the recorder has its own convention; future peers may
  differ).

**Config (`azt_collabd/config.py`):**

- New ``cawl_image_repo`` config kwarg on ``configure()``,
  with ``AZT_CAWL_IMAGE_REPO`` env-var override. Empty
  default — peers must configure it before any fetch
  happens. An unconfigured daemon short-circuits
  ``get_index()`` to ``{}`` without any network call, so a
  misconfigured launch doesn't silently hammer the wrong
  GitHub repo.

**Wire shape:**

```
GET /v1/cawl/index
→ {
    "ok": true,
    "index": {
        "repo":       "<owner>/<repo>",
        "branch":     "HEAD",
        "fetched_at": <unix-seconds>,
        "files": [
            {"path": "cawl-1234.jpg",
             "url":  "https://raw.githubusercontent.com/.../cawl-1234.jpg"},
            ...
        ]
    }
  }
```

Empty dict at ``index`` (``{}``) means "no images known" —
same shape peers got from an empty pre-migration resolver,
so no new failure branch is required.

**Client side:**

- New ``cawl_index()`` wrapper in
  ``azt_collab_client/__init__.py``. Returns the dict on
  success, ``{}`` on transport failure or empty daemon
  response. No raw ``ServerUnavailable`` reaches the caller.
- Re-exported from ``__all__``.

**What this fixes vs. what's left.**

- ✅ Index fetch no longer per-peer. One daemon-side fetch
  per device per 24h TTL.
- ✅ Rate-limit blow-up under dev rebuild / multi-peer use.
- ✅ Stale-cache survival across GitHub outages.
- ⏳ Image *binaries* still fetched by peers directly from
  ``raw.githubusercontent.com``. That endpoint is on a much
  more permissive rate-limit domain (effectively unmetered
  for normal use), so it isn't the bottleneck. Migration to
  a daemon-served provider URI for binaries is Stage 2 — same
  shape (suite-scoped resource, daemon ownership, peer
  consumes via provider) but a larger touch (every peer's
  image-resolution path). Filed as the remaining piece in
  NOTES_TO_DAEMON.md after the 0.37.0 cut.
- ⏳ Install-day-with-no-network still has no bundled
  index seed. ~50 KB index JSON shipped as a server APK
  asset would close the gap; daemon copies into
  ``$AZT_HOME/cawl/`` on first start if empty. NOT
  bundling image binaries — 100–300 MB per release is
  the wrong shape for Android distribution; daemon-side
  lazy caching (Stage 2 RPC) is how the binary
  deduplication / cross-peer-sharing wins land.

**Floor:** neither MIN_SERVER_VERSION nor MIN_CLIENT_VERSION
is bumped. The endpoint is purely additive — a pre-0.37 client
calling ``cawl_index()`` gets ``{}`` from a 0.37 daemon and is
fine; a 0.37 client calling ``cawl_index()`` against a pre-0.37
daemon hits a 404, the wrapper falls back to ``{}``, and the
peer continues to resolve illustrations from its own legacy
fetch path. Peers migrate at their own pace; no hard cut-over.

### azt_collabd 0.36.0 / azt_collab_client 0.36.0 — `atomic_commit` RPC for URI atomic writes, MIN_SERVER_VERSION lifted hard to 0.36.0

Closes the last cross-process atomic-write gap: peers writing
LIFT / audio / image bytes through a ``content://`` URI on
Android now ship the full payload to the daemon, which performs
the tempfile + ``os.replace`` atomic write in its own process.

**Why this is needed.** On Android the daemon's working_dir
lives in the standalone server APK's private filesDir. Peers
write to it via ``ContentResolver.openFileDescriptor``, which
returns an FD into the daemon's filesystem. ``ftruncate(fd, 0)``
+ subsequent writes through that FD are NOT atomic from any
other observer's perspective: a concurrent peer write, or the
daemon's own merge-output write, can see torn bytes mid-write.
The 2026-05-12 ``baf`` repro showed exactly this — two peer
serializations interleaved through the FD path produced
malformed XML which the daemon then misparsed catastrophically.

The 0.35.4 client added a path-keyed lock so a single peer
process can't race with itself, but cross-peer-process and
peer-vs-daemon races stayed open. 0.36.0 closes them.

**Wire shape:**

```
POST /v1/projects/<lang>/atomic_commit
{
  "path": "<rel_path>",
  "data_b64": "<base64-encoded-bytes>"
}
```

``rel_path`` is one of ``<file>.lift``, ``audio/<file>``,
``images/<file>`` — same whitelist as the ContentProvider's
``_resolve_path``. Path-traversal and out-of-whitelist shapes
return 400 before any filesystem touch.

The daemon serializes the write through ``project_lock`` (so it
can't overlap with a sync's merge-output write or another
atomic_commit) and writes via tempfile + ``os.replace`` in its
own process. The destination is always a complete copy of one
version, never a torn mix.

Response: ``{ok: True, result: {statuses: [{code: 'ATOMIC_COMMITTED',
params: {bytes_written, sha256}}]}}``. The sha256 lets the peer
verify the bytes that landed match what it sent.

**Client side:**

- New ``atomic_commit_bytes(langcode, rel_path, data) -> Result``
  wrapper in ``azt_collab_client/__init__.py``. Transport
  failures translate to ``SERVER_UNAVAILABLE`` / ``SERVER_ERROR``
  per the existing contract; the peer never sees a raw
  ``ServerUnavailable``.
- New ``_UriAtomicWriteFile`` in ``lift_io.py`` buffers writes
  in memory and ships them on commit. Memory cost: ~1.33× the
  file size (base64 encoding) during the encode-and-send window.
  For LIFT (tens of MB at worst) this is fine.
- ``LiftHandle.atomic_open_write`` on a URI now returns
  ``_UriAtomicWriteFile`` (the 0.35.4 fallback to plain
  ``open_write`` is gone). Filesystem-path callers still get
  the local ``_AtomicWriteFile`` (tempfile + ``os.replace`` in
  the peer process).
- ``MediaHandle`` inherits the same shape — audio and image
  atomic writes also go through the RPC on URI projects.

**Floor:** ``MIN_SERVER_VERSION 0.35.4 → 0.36.0`` (hard).
Peers rebuilt against 0.36.0 clients won't pair with pre-0.36.0
daemons, so the install/update prompt fires before any sync
attempt. Pre-0.36.0 clients are still allowed against a 0.36.0
daemon — they just don't get the atomic-URI-write benefit (the
old client doesn't know about the endpoint). MIN_CLIENT_VERSION
does NOT bump for that reason: the new endpoint is additive,
not breaking.

### azt_collabd 0.35.4 / azt_collab_client 0.35.4 — atomic LIFT writes from peers, forensic dumps on every guard trip, MIN_SERVER_VERSION lifted to 0.35.4

Two complementary pieces from the same investigation:

**Client side (`azt_collab_client/lift_io.py`):**

- `LiftHandle.open_write` is now **serialized within the peer
  process via a path-keyed reentrant lock**. Two threads of the
  same peer calling `open_write` on the same target queue
  rather than race. Pre-0.35.4 a rapid-succession `open_write`
  pattern (e.g., the recorder serializing the LIFT twice in
  close succession after two audio captures) could interleave
  at the byte level — each `open_write` opens an independent FD
  and `ftruncate(0)`s the file, so two writes from offset 0
  produce malformed bytes with torn tag boundaries. The
  `baf` 2026-05-12 repro shows two same-lang `<gloss>` elements
  with one's `<text>` mid-stream embedded in the other's; that's
  the signature.
- New `LiftHandle.atomic_open_write` context manager. Writes go
  to a sibling tempfile with a random suffix; on clean exit
  (`__exit__` with no exception), `os.replace` atomically renames
  the tempfile over the destination. On exception, tempfile is
  removed and the destination is untouched. Filesystem paths get
  true atomic semantics; content:// URIs fall back to the
  lock-protected `open_write` (ContentResolver has no clean
  atomic-rename for arbitrary Provider URIs). Two concurrent
  `atomic_open_write` calls on the same destination are safe:
  each writes its own random-suffixed tempfile, and whichever
  `os.replace` runs last wins — the destination is *always* a
  complete copy of one version, never torn.
- `MediaHandle` inherits both behaviors transparently
  (audio and image writes also benefit).

**Daemon side (`azt_collabd/lift_merge.py` + `repo.py`):**

- New `build_diagnostic_xml` / `diagnostic_filename` /
  `is_guard_kind` / `DIAGNOSTICS_SUBDIR` helpers in
  `lift_merge.py`. The diagnostic XML schema captures:
  - `guard` kind, daemon version, UTC timestamp.
  - `merge-context`: lift path + the three commit SHAs
    (local / remote / base) so the bytes are reachable via
    `git show` from any clone.
  - `process`: pid, ppid, executable, cwd.
  - `thread`: name + ident of the thread that hit the guard,
    plus the names + idents of every other live thread (so
    a concurrent-call hypothesis can be tested from the
    dump).
  - `caller-stack`: the `traceback.extract_stack` slice at
    guard-fire time, file + line + function for each frame.
  - `filesystem-state`: stat results for the working-tree
    LIFT path, .git directory, and `.azt-collab/diagnostics`
    so disk-full or permission anomalies are recoverable.
  - `inputs`: per side, byte length, sha256, parsed entry
    count, parse-error message (when parsing failed).
  - `merged`: byte length, sha256, entry count, parse-error
    (when applicable).
  - `conflict-fields`: the diagnostic strings the guard
    produced (e.g., the `_looks_truncated` or
    `_looks_catastrophic_output` message).
  - `recent-trace`: a slice of the in-process ring buffer
    (`_TRACE_RING_SIZE = 500` entries, default last 120 s)
    capturing the daemon's pre-guard activity. Every
    `[sync-trace]` / `[merge-trace]` / `[merge-diag]` line
    in `azt_collabd` now routes through `lift_merge.trace()`,
    which appends to the ring AND prints to stderr — so
    dumps carry the same time-precise log slice that logcat
    would have shown if anyone had been looking.
- `repo._merge_diverged` now dumps the diagnostic to
  `<working_dir>/.azt-collab/diagnostics/<utc>-<guard>-<nonce>.xml`
  whenever a guard fires on a .lift merge. The file gets staged
  into the merge commit by the existing `_stage_all` call and
  pushed to the remote alongside the safe merge result. A
  pre-existing `_write_merge_diagnostic` helper does the write
  via tempfile + `os.replace` so a half-written diagnostic can't
  be staged.

  Best-effort: a diagnostic-write failure logs to stderr and
  the merge proceeds. We don't want the audit trail to block
  the merge if e.g. the disk is full.

  **User isn't bothered.** The file lives under a hidden
  `.azt-collab/` directory and is mentioned only in
  `[merge-diag]` log lines. The intent is forensic — when a
  guard fires (rare; ideally never), the daemon team or a
  future-LLM analysis can `git log .azt-collab/diagnostics/`
  on any clone of the repo, find the dump, and reconstruct
  exactly what the merger saw. No console prompts, no UI
  surfacing.

**Versioning + floor:**

- `azt_collabd 0.35.3 → 0.35.4`.
- `azt_collab_client 0.35.3 → 0.35.4`.
- `MIN_SERVER_VERSION 0.35.3 → 0.35.4` hard. Pre-0.35.4 daemons
  still have all the guards (those landed in 0.35.1–0.35.3) but
  log guard trips to stderr only — Android logcat is ephemeral
  and not retrievable. The user explicitly asked that every
  guard firing be recoverable from the repo for post-hoc
  analysis; pinning the floor here is the discipline that
  enforces it.

**Why this is the right shape, in one sentence:** any future
guard firing automatically leaves a small structured XML file
in git that says exactly what the merger saw — without the
user having to do anything, and without polluting their LIFT
or audio data.

### azt_collabd 0.35.3 / azt_collab_client 0.35.3 — output-side catastrophic-loss guard, MIN_SERVER_VERSION lifted hard to 0.35.3

The closed merge note (filed 2026-05-11, "merge driver reorders
entries to guid order") was reopened on 2026-05-12 with new
commit-level evidence from the `baf` project: merge commit
`679c102` produced 1 entry from inputs of 1702 and 1700 entries
(base ~1700). The input-side truncation guard added in 0.35.1
**cannot have fired** for these inputs (both well above the
threshold). Yet the daemon's `lift_merge.three_way_merge`
committed a 1-entry merge result, annotated `azt-lift-conflict
value="theirs"` on the surviving entry, with the original guid.

**Bug-shape analysis** (recorded so the institutional
knowledge survives even if the proximate cause is never
narrowed):

The surviving entry's annotation form — single entry,
`value="theirs"`, original (non-`-theirs`-suffixed) guid — is
produced by exactly one pre-v3 code path: the `delete-modify`
branch, which fires when `ours_entries.get(guid) is None`
while `theirs_entries.get(guid)` is present (with content
differing from base). For 1699 of the 1700 entries to be
dropped through that branch's sibling code (`if _canon(b) ==
_canon(t): continue   # they didn't change it; we deleted`),
**the merger's internal `ours_entries` view had to be
near-empty at the moment of the merge**. Yet `git show
dc69264:baf.lift | grep -c '<entry '` shows 1702 entries in
the committed blob.

That contradiction — committed blob is full, the merger's view
was near-empty — points at a non-deterministic ordering
problem we can't reproduce without the daemon logs from the
exact minute (`Tue May 12 13:17:40 UTC 2026`). Plausible
proximate causes ranked by likelihood:

1. **Two `_merge_diverged` calls raced.** Concurrent syncs
   (the auto-sync on project select + a manual sync, or two
   peers' sync requests on the daemon, or any debounce race)
   each independently called `_walk_tree` on a working-tree
   snapshot. If one snapshot caught a mid-write LIFT (peer's
   `MediaHandle.open_write` had `ftruncate(0)`'d the file at
   `lift_io._open_content_uri` line 211 but the subsequent
   write hadn't completed), the merger saw a truncated ours
   and committed the destructive merge. The OTHER call (with
   the full file) then committed `dc69264` on top — making
   the committed blob look healthy in retrospect.
2. **Mid-write commit, immediately rewritten.** A peer's
   write to the LIFT was interrupted (process killed, OOM,
   activity teardown) right after `ftruncate(0)` and before
   the bulk write completed. The daemon's commit_audio_and_sync
   captured the truncated state, merged, and committed
   `679c102`. The peer restarted, finished writing, and
   committed `dc69264` later — making the "before" commit
   look healthy in retrospect.
3. **A path-level cache or staging issue** in
   `_merge_diverged` returning blob bytes that don't match
   what's now at `git show dc69264:baf.lift`. Less likely
   given dulwich reads commit→tree→blob deterministically,
   but not ruled out.

Without the daemon logs of that minute, we cannot prove
which (if any) of these was the proximate cause. **What we
CAN do is make the next occurrence harmless.**

**Most likely proximate cause** (refined after the user's
follow-up observation that the surviving entry shape —
single entry, `value="theirs"`, ORIGINAL guid — uniquely
identifies the **delete-modify** branch, not modify-modify
— which forces the conclusion that `ours_entries` was empty
at merge time):

`grep -c '<entry '` is a regex line count, not an XML
validator. If `dc69264:baf.lift` had 1702 `<entry ` text
matches AND a structural XML defect somewhere (unclosed tag,
embedded null byte, bad encoding sequence, anything ET
refuses), `git show` prints the raw bytes (succeeds — git
doesn't validate) but `ET.fromstring` raises `ParseError`.
The pre-0.35.2 `_parse` caught `ParseError` and **silently
returned an empty LIFT doc with no signal back to the
caller** — so the merger's `ours_entries` came back empty,
every guid hit the delete-modify branch (1699 dropped via
`continue   # they didn't change it; we deleted`, 1 emitted
as a theirs-annotated entry).

That fits the evidence exactly without invoking races or
staging mysteries. The pre-0.35.2 silent-ParseError-masking
was already addressed by 0.35.2's `_parse` returning
`(root, error_msg)` — the merger now refuses to commit when
ours/theirs fails to parse. The output-side guard in 0.35.3
is a complementary defense: catches the symptom whatever the
proximate cause, including ones we haven't thought of.

**Forensic trace added.** `three_way_merge` now logs
`[merge-trace] path=... base=N ours=M theirs=K
ours_err='...'` at the start of every invocation. Next
time anything looks weird, the logs themselves answer "did
`_parse` mask an error, or did the merger genuinely see N
entries?" without needing forensic git archaeology.

**Output-side `_looks_catastrophic_output` guard.** Refuses
to commit a merge whose entry count is < 1/4 of the smaller
healthy input. Skips small projects (base < 50 entries; the
ratio doesn't generalize at small scale) and skips when an
input was itself tiny relative to base (input-side guard had
jurisdiction; don't double-attribute). When triggered: keeps
the larger input intact verbatim, emits a single
`catastrophic-merge-output` Conflict carrying the full count
diagnostic. Defense-in-depth: catches the symptom regardless
of which proximate cause produced the algorithmic loss inside
the merger.

For the actual `baf` numbers (1, 1702, 1700, 1700): the guard
fires unambiguously. Even if a future bug produces some other
algorithmic loss, as long as the output is dramatically
smaller than the inputs, the guard catches it.

**Why this isn't redundant with the input guard.** The input
guard (`_looks_truncated`) checks the SHAPE of the inputs —
useful when one input arrived obviously truncated. The output
guard checks the SHAPE of the result — useful when both
inputs looked healthy at parse time but the algorithm lost
data internally. They're independent layers. The bug repro
above is the canonical case where input-side guard CANNOT
fire (both 1700-ish) but output-side guard MUST fire (1).

**`MIN_SERVER_VERSION` lifted 0.35.1 → 0.35.3 (hard).** The
proximate cause for the 0.35.1 collapse is undetermined and
could recur; forcing the floor ensures every peer paired with
a daemon has the output guard. Standard discipline matching
prior hard floors (0.34.0 sync, 0.34.1 reorder, 0.35.1
input-truncation).

**Tests** (`tests/test_lift_merge.py`):

- `test_full_sides_one_entry_differs_keeps_all_entries`:
  100-entry base/ours/theirs where only one entry differs.
  Output must contain 100 entries (with a field-level
  conflict on the differing one). Locks in the v3 recursive
  merge's correctness for this case, and via the output
  guard ensures even a regression in the recursive merge
  doesn't slip past.
- `test_catastrophic_output_guard_fires_directly`: direct
  unit tests of `_looks_catastrophic_output` covering: the
  bug numbers (trip), healthy output (skip), 50%-delete
  (skip), small project (skip), already-tiny-input (skip,
  input guard's territory).

**Wire-compat.** Additive: existing conflict kinds and
result shape unchanged. New `catastrophic-merge-output`
Conflict kind shows up only if the guard fires. Existing
peers see this as a generic CONFLICTS result; new peers can
distinguish via `Conflict.kind`.

### azt_collabd 0.35.2 — LIFT merge: recursive field-level conflict resolution + parse-error guard

The v1/v2 merge produced "two whole entries with synthetic
``-theirs`` guid suffix" on every modify-modify conflict — correct
but unresolvable in practice. A 1700-line entry conflict where the
only divergence is a `<text>` byte is invisible to the user; they
won't sit and diff two thousand lines to find the one that
differs. Field reports confirmed: nobody resolves these.

**v3 recursive merge.** Conflicts now express at the **narrowest
LIFT-multi level** that contains the divergence. A same-lang
``<text>`` conflict produces two same-lang ``<form>`` siblings each
carrying its own text and a single ``<annotation
name="azt-lift-conflict" value="ours|theirs"/>`` marker — one
``<entry>``, one ``<lexical-unit>``, two ``<form>``s. A
``<pronunciation>`` conflict duplicates at pronunciation level
(entry-level otherwise stays single). A gloss-text conflict
duplicates at the ``<gloss>`` level inside a single sense. Only
when a conflict genuinely can't be narrowed (entry-attribute
differences with no element-children divergence) does the
whole-entry duplication fallback kick in — with the synthetic
guid suffix kept for that rare case.

Implementation: ``_merge_pair`` + ``_walk_children`` recursive
helpers, plus a ``_MULTI`` policy table mapping
``(parent_tag, child_tag)`` → schema multiplicity. Unknown pairs
default to multi (safer to over-allow than under-allow). The
entry-level ``<annotation name="azt-lift-conflict" value="conflict">``
marker now carries a ``<trait name="azt-lift-conflict-fields"
value="...">`` listing slash-delimited paths from the entry root
to each conflict site (e.g.,
``lexical-unit/form[lang=en],sense[id=A]/gloss[lang=en]``) — peer-
side resolvers can jump to the conflicting sub-elements without
re-walking the merged tree.

**Parse-error guard.** ``_parse`` no longer masks
``ET.ParseError`` silently. When ``ours`` or ``theirs`` fails to
parse (mid-write truncation that breaks XML, etc.), the merge
refuses entirely — keeps the side that parsed cleanly, surfaces
a ``parse-error`` Conflict in the result. Pre-0.35.2 the silent
mask + the merge body's "absent from ours = ours deleted" rule
combined to produce catastrophically destructive merges when
the input was structurally invalid. Detection is now at the
input layer where the data still tells us what's wrong.

**Empty-side guard, small-project case.** The 0.35.1 truncation
guard only triggered on ≥50-entry projects (the ratio threshold
needs absolute size to avoid false-positives on legitimate small
edits). The empty-side case — ours has 0 entries while base has
any and theirs has any — now triggers regardless of project
size. Catches a 5-entry project where one peer's write got
``ftruncate(0)``'d mid-flight. False-positive only for users
who *intentionally* clear every entry, which doesn't happen in
this suite's peer flows.

**Wire-compat.** Same shape: emits LIFT bytes, peers read them
as normal LIFT. Old peers reading the v3 output see the
duplicated forms/glosses as normal LIFT content (forms ARE
schema-multi inside multitext containers; same-lang siblings are
schema-valid even if semantically "one per writing system"
conventionally) — no crashes, just a peer that doesn't recognise
the new annotation pattern. The ``conflict-fields`` trait value
changed from flat tag names to slash-delimited paths; peers
parsing it should treat the value as opaque text or a
comma-separated path list. No ``MIN_SERVER_VERSION`` bump —
peers don't actively consume the conflict format yet.

**Tests.** ``tests/test_lift_merge.py`` adds coverage for the
same-lang text case, pronunciation case, parse-error case,
empty-side small-project case, one-sided-change-no-conflict
clean path, and the entry-level marker's path-list trait.

### azt_collab_client 0.35.2 — peers may write image bytes through the provider (gate removed)

`MediaHandle(path_or_uri, kind='image').open_write()` no longer
raises `PermissionError`. 0.18.0 through 0.35.1 raised under an
"images are read-only from peers; the daemon owns image
additions" rule, which on inspection turned out to be an
**unsubstantiated policy** — every mention of it (`lift_io.py:149-170`,
`CLAUDE.md`'s cross-package-access section, the 0.18.0 CHANGELOG
entry) asserted the rule but none cited a driving concern.
The recorder team filed a NOTES_TO_DAEMON entry (2026-05-12)
showing that the rule made the entire in-app image-selection
feature silently no-op on URI projects, with four call sites
gated off (`_download_and_set`, `_copy_and_set`,
`_save_remote_image`, and the workers under it).

**Decision:** symmetry with audio. The daemon's provider
already supports image writes (`_resolve_path`'s
`_ALLOWED_MEDIA_DIRS = ('audio', 'images')` whitelist
auto-mkdirs the parent on first write). The two-write pattern
(image bytes through `MediaHandle`, illustration ref through
`LiftHandle`) is the same shape audio uses today and has
worked correctly in the field. Binary-conflict resolution on
basename collisions falls through to `repo._merge_diverged`'s
existing `non-lift-modify-modify` branch (merging-side wins
on disk, both versions remain in git history) — same handling
audio's `.wav` files get.

**Wire-compat:** purely client-side change. Daemon-side
provider already supports image writes; no daemon code changed.
Older peers paired with 0.35.2+ daemons keep their old
`PermissionError` gate (they bundle the old client) so no
behavior change there. Newer peers paired with older daemons:
the daemon's provider has always allowed image writes through
`_resolve_path`, so this works wire-side, but pre-0.35.1
daemons have the merge-truncation gap — the existing 0.35.1
`MIN_SERVER_VERSION` hard floor blocks that pairing anyway. No
floor bump for 0.35.2.

**Peer call-site cleanup** (recorder, viewer, future peers):
drop the `is_uri: return` gates that were routing around the
removed PermissionError. Use the same `MediaHandle` shape as
audio. No new endpoint, no wrapper, no recorder-side
infrastructure work beyond removing the gates.

### azt_collabd 0.35.1 / azt_collab_client 0.35.1 — LIFT merge: truncation guard + field-level conflict annotations; MIN_SERVER_VERSION lifted hard to 0.35.1

Field-reported 2026-05-12 (NOTES_TO_DAEMON.md, closed): a peer's
post-merge LIFT shrank from ~1700 entries to 1, leaving only the
single conflicting entry annotated `azt-lift-conflict="theirs"`.
The reporter hypothesized a sibling bug to the closed reorder-by-guid
issue ("union computed wrong"); the current code actually walks
`union(ours, theirs)` correctly, so that hypothesis didn't fit. The
real shape: `ours` arrived at the merge with a near-empty entry
list while `base` and `theirs` had the full template. Every base
entry absent from `ours` and unchanged in `theirs` then took the
"they didn't change it; we deleted it" branch — correctly, given
the inputs — producing the 1-entry destructive merge. The merge
algorithm wasn't lying; the **inputs** were corrupted upstream
(peer-side write race, partial commit, or sandbox sync hiccup
between recorder and daemon — not narrowed in this session).

**Defensive guard (`_looks_truncated`).** When all three sides
have non-trivial entry counts AND one side's count is less than
1/50 of the other AND the larger side has ≥50 entries, refuse
the destructive merge. Keep the larger side intact (unchanged
bytes; whatever was in the merge commit before this fix would
have been destructive, so we bias toward preserving data), and
return a single `Conflict(kind='truncation-suspected', fields=[…])`
in the result. Upstream callers see `S.CONFLICTS` and surface
the diagnostic; nothing destructive lands in git. Thresholds
are intentionally conservative — legitimate large-scale
deletions still go through (you can delete up to 98% of a
project in one commit without tripping the guard).

**Field-level conflict info.** Per-entry conflicts (modify-modify,
add-add) now annotate the `<annotation name="azt-lift-conflict">`
element with a `<trait name="azt-lift-conflict-fields" value="…">`
sub-element listing the LIFT child-element keys that actually
diverged — `lexical-unit`, `citation`, `field[type=SILCAWL]`,
`sense[id=…]`, `pronunciation`, etc. The `Conflict` dataclass
gains a `fields: list[str]` parallel field, surfaced via
`to_dict()` for any peer-side merge UI. Lets a recorder
(re-recording audio for a sense) ignore conflicts that don't
touch `pronunciation`; lets a viewer (or future merge resolver)
focus the user on the specific sub-elements that need attention
instead of asking them to diff the whole entry by eye.

Modify-delete / delete-modify conflicts don't carry field info —
the conflict there is "entry exists vs doesn't," not
sub-element divergence.

**Wire-compat:** additive. Older peers see the new trait as
unknown LIFT content (which their LIFT readers tolerate by
design — annotations are extensible) and the new
`truncation-suspected` Conflict kind as just another conflict
they surface generically.

**`MIN_SERVER_VERSION` raised 0.35.0 → 0.35.1 (hard).** Per the
reporter's ask #5: pre-0.35.1 daemons have no truncation guard,
so a peer paired with one can still hit the destructive merge.
The floor bump prevents that pairing — sync refuses with
`server_too_old` until the user updates the server APK. Same
discipline as the 0.34.1 reorder fix.

### azt_collabd 0.35.0 / azt_collab_client 0.35.0 — surface broken GitHub refresh-token state with a deadline-aware toast; codify auto/user sync contract

Field-observed in this session's first sync trace: the daemon's
``get_valid_github_token`` had been silently swallowing
``incorrect_client_credentials`` from the OAuth refresh endpoint
("Return the old token — it might still work"). That's a humane
fallback in the short term, but it converts an 8-hour countdown
into a silent cliff: once the existing access token expires, every
authenticated git op starts failing with no user-visible warning
that the user needs to re-auth.

**Daemon side.** ``azt_collabd/store.py``:
``get_valid_github_token`` now records ``refresh_broken=True`` +
the error string + the check timestamp on refresh failure, and
clears the flag on a subsequent successful refresh.
``set_github_tokens`` (called by the device-flow completion path)
also clears the flag — fresh tokens supersede any prior
refresh-failure state. ``get_status`` exposes
``github.refresh_broken`` and ``github.access_token_expires_at``
(unix timestamp = ``token_time + 8h``) so peers can read the
state via the existing credentials-status RPC without polling a
new endpoint. New helper ``github_refresh_state()`` returns the
same fields for daemon-internal use.

**New status code:** ``S.AUTH_REFRESH_STALE`` (mirrored in
``azt_collab_client/status.py``). Carries
``params['expires_at']``. Appended to every sync result —
``_h_project_sync`` and ``scheduler._run_sync`` both call a
shared ``server._annotate_with_auth_health(res)`` after running
the sync, so the status piggybacks on whatever the underlying op
returned (typically ``PUSHED + AUTH_REFRESH_STALE`` during the
access-token's last hour of life).

**Client side.** ``azt_collab_client/translate.py`` adds a
handler for ``S.AUTH_REFRESH_STALE`` that renders
"GitHub session needs re-authentication — current access
expires {deadline}. Open GitHub Connect and tap Re-authenticate."
``_format_deadline`` converts ``expires_at`` to a relative
phrase ("in 47 minutes", "in 3 hours", "now (already expired)")
so the user reads how much runway they have without dragging
timezone / locale plumbing into a one-shot string. The
"refresh-broken" state is also visible to peers via
``get_credentials_status() → github.refresh_broken`` for
peers that want a startup banner.

**Peer contract.** Documented in
``azt_collab_client/CLAUDE.md`` § "Peer contract: routing on
sync results" — auto-sync silences this code (per the existing
auto/user contract; we don't disrupt mid-flow); user-initiated
sync surfaces ``translate_status(status)`` as a toast. No
routing — the toast text already names GitHub Connect /
Re-authenticate as the next step. The state clears when the
user completes a fresh device flow.

**Wire compatibility.** Purely additive at the wire layer:
older peers paired with a 0.35.0 daemon see the new code as
an unknown status (verbose-but-non-fatal translate fallback);
older daemons paired with a 0.35.0 peer never emit the code,
so the peer never branches on it.

**``MIN_SERVER_VERSION`` raised 0.34.1 → 0.35.0** anyway, as a
*soft* requirement (no wire incompatibility to enforce). The
real reason: the peer contract changes in CLAUDE.md (auto-sync
silencing config-class codes, user-initiated routing /
toasting, deadline-aware ``AUTH_REFRESH_STALE`` handling) need
peer rebuilds to take effect. Bumping the floor forces every
peer paired with a 0.35.0+ daemon to rebuild against the
0.35.0 client, where the contract is documented and the
``AUTH_REFRESH_STALE`` translation is wired. Without the bump,
peers can keep running their pre-0.35.0 client and silently
disrupt project flows on auto-sync — the exact symptom that
surfaced as the "selected B got A" picker complaint earlier
in the 0.34.x development cycle.

### azt_collabd 0.34.1 / azt_collab_client 0.34.1 — LIFT merge preserves document order, MIN_SERVER_VERSION lifted to 0.34.1

Field-reported by the recorder team (NOTES_TO_DAEMON.md, 2026-05-11):
the very first real merge on any project rewrites the LIFT file
into guid-alphabetical order, irreversibly destroying the project's
semantic document order (template-driven SILCAWL order for new
projects; whatever the contributor established otherwise). The
change is committed and pushed before any peer can observe it, and
ElementTree round-trips preserve whatever order they parse, so all
subsequent edits cement the scrambled order. Repro confirmed
against `kent-rasmussen/sw-US-x-kent`: the merge commit `29d1266`
puts the entry whose guid sorts first (`002b6d2c-…` → SILCAWL
1572) at the top of the file, with every entry following in strict
guid order.

**Root cause.** `azt_collabd/lift_merge.py:three_way_merge`
walked `sorted(all_guids)` and appended to `merged_root` in that
order. Deterministic, yes — but the wrong determinism.

**Fix.** Walk `ours` in document order, then theirs-only entries
in theirs's document order. Anchoring on `ours` is the
conventional "the merging side keeps the order it was already
working against" pick, and it makes merge commits diffable: only
actually-changed entries move, instead of the whole 1700-entry
file appearing to be rewritten. Base-only guids (deleted on both
sides) are naturally excluded — they were a `continue` no-op in
the old loop body anyway. Same body, new traversal.

**MIN_SERVER_VERSION raised 0.34.0 → 0.34.1.** Pre-0.34.1 daemons
will commit and push a scrambled file on the next merge, with no
peer-visible warning before the damage hits git history. Hard
gate is preferable to silent fallback. Peers paired with a 0.34.0
daemon get the standard `server_too_old` bootstrap prompt.

**Repair for already-scrambled projects is deliberately manual**,
not automated, and unlikely ever to be. The natural order is
application-meaningful (SILCAWL row for template-derived projects;
headword for free-form lexica; sometimes a contributor's
deliberate manual sequence). A unilateral "re-sort everyone's
LIFT" utility can't know which of those applies, and silently
re-ordering a contributor's intentional sequence is the same
class of damage as the original bug. Project owners who want to
restore a known template order on a scrambled project do it by
hand, as one explicit commit, with explicit understanding of
what they're choosing to lose.

### azt_collabd 0.34.0 / azt_collab_client 0.34.0 — sync correctness: three load-bearing fixes, MIN_SERVER_VERSION lifted to 0.34.0

Two-device sync between Android peers was silently broken across the
entire 0.33.x line: after the first race between two phones pushing,
the daemon entered a state where every subsequent sync attempt
acted on a phantom remote, produced malformed merge commits, lost
the same race three times, and finally surfaced `PUSH_FAILED` or
(worse) a misleading `REPO_NOT_AUTHORIZED` against an unrelated
GitHub install. Three independent bugs were stacked; fixing one only
exposed the next. They're shipped together as a single minor bump.

`azt_collab_client.MIN_SERVER_VERSION` is raised from `0.31.0` to
`0.34.0`. A peer that gets through bootstrap will refuse to talk to
any older daemon — the user is forced to install/update the server
APK before sync re-engages. This is intentional: pre-0.34 daemons
*appear* to work and quietly corrupt local repo state by accumulating
malformed merge commits and never advancing
`refs/remotes/origin/<branch>`, so silent fallback is worse than the
hard gate.

**(1) `_merge_diverged` now produces real two-parent merge commits.**
The pre-0.34 code called `porcelain.commit(repo, merge_heads=[remote_sha], ...)`,
but dulwich 1.2.1's `porcelain.commit` doesn't expose `merge_heads`
as a public kwarg (it's an internal-only path used by `amend=True`).
The call raised `TypeError`, fell into a legacy graft-the-parent-
after-the-fact fallback (commit without merge_heads, then mutate
`commit.parents` post-hoc + re-add to the object store), which
silently produced a commit whose stored parents were `[local_sha]`
only. GitHub's `git-receive-pack` correctly rejected the push as
`DivergedBranches` because the "merge" commit didn't actually
contain `remote_sha` as an ancestor. Fix: drop down to
`repo.get_worktree().commit(merge_heads=[remote_sha])` —
the worktree-level API DOES accept `merge_heads` and sets
`c.parents = [old_head, *merge_heads]` atomically before writing
the object and advancing the ref. The graft fallback is removed;
the worktree API is in dulwich's public surface since 1.0.

**(2) HTTP 403 detection no longer false-positives on hex SHAs.**
The pre-0.34 code checked `'403' in str(exc)` to decide whether a
dulwich push exception was a real auth failure. dulwich's
`DivergedBranches.__str__` expands to `"(b'<current_sha>', b'<new_sha>')"`
— two 40-char hex SHAs. Random hex contains the trigraph `'403'`
~1 push in 250 by chance; the field trace had
`e41db428f68e9f7f6334`**`037`**`345d6450...`, which matched. The
false positive routed a diverged-branch failure through
`diagnose_403`, exiting the sync flow with a bogus
`REPO_NOT_AUTHORIZED` before the retry/merge could run. Fix:
`re.search(r'\b403\b', str(exc))` via a new `_is_http_403` helper
applied to all four call sites. Word boundaries don't fire inside
an all-word-char hex SHA but do fire in dulwich's
`"unexpected http resp 403 for <url>"` `GitProtocolError` message.

**(2b) `diagnose_403` now scopes by repo owner.**
When a real 403 *does* happen, `diagnose_403` was calling
`check_app_installed(token)` without `account_login`, so it grabbed
the first install in `/user/installations` whose `app_slug` matched.
A user who's a collaborator on five orgs that each installed
azt-collaboration got the first listed (`MattGyverLee` in the field
trace, install id 121228993, `selected` repos) instead of the
repo's own owner (`kent-rasmussen`, install 130605088, `all` repos),
and the follow-on `check_repo_in_installation` correctly answered
"no" — surfacing `REPO_NOT_AUTHORIZED` for a repo the user actually
has access to via their personal install. Fix: parse the repo
owner from `remote_url` first and pass it as `account_login` so
the install inspected is the one that should host the repo.

**(3) `porcelain.fetch` / `porcelain.pull` are called with the
remote NAME, not the URL.** Dulwich's `porcelain/__init__.py:fetch`
only runs `_import_remote_refs` (which writes
`refs/remotes/<name>/<branch>`) when `get_remote_repo` could resolve
the first positional arg back to a configured `[remote "<name>"]`
section. Passing a URL always misses (no section is named
`https://...`), so `remote_name = None` and the gate at line 4550
skips the ref import. The pack transferred successfully (HTTP 200
in logs) but the local tracking ref stayed frozen at whatever
`porcelain.clone` wrote at project-create time. Every subsequent
sync read a stale `remote_sha` from `refs/remotes/origin/<branch>`
and acted on a phantom state of the world. Field trace: actual
remote tip moved to `76201a5d…` ~25 minutes before the user opened
the recorder, but the daemon kept reading
`new_remote=42535766…` (the clone-time SHA) on every retry fetch,
merged against the phantom, and lost the push race three times in
a row. Fix: pass `'origin'` to every `porcelain.fetch` / `pull`
call site in `azt_collabd/repo.py`. Dulwich resolves `'origin'` via
`[remote "origin"]` (which `_init_repo_locked` /
`_clone_repo_locked` always populate), uses the `username` /
`password` kwargs we still pass explicitly, and — critically — runs
`_import_remote_refs` so the tracking ref advances on each fetch.
Push paths are unchanged: they already advance the tracking ref
manually via `repo.refs[remote_ref] = local_sha` after a successful
push (the line landed earlier for the `(+N)`-counter regression).

**Observability.** The retry-path fetch + merge in
`_sync_repo_locked` was wrapped in a bare `except Exception: pass`
— failures inside `_merge_diverged` were swallowed. Added
`[sync-trace]` lines for the retry fetch SHA, the retry merge SHA,
and any exception so future divergence loops are diagnosable from
logcat alone. (Reading the field trace was load-bearing for
isolating bugs 2 and 3.)

**(4) Auto-update download URL now matches the actually-published
asset name — and tolerates peer literals that drift from it.** The
recorder's bootstrap was wired with
`peer_asset_filename='azt_recorder.apk'` (Python-pkg underscore
form), but the published GitHub asset for `kent-rasmussen/azt-recorder
v1.39.0` is `aztrecorder.apk` (Android-package-segment form, no
underscore — matches `buildozer.spec → package.name`). The
`releases/latest/download/<wrong-name>` redirect 404'd; users got
"Download failed: HTTP Error 404" on tapping Update. The convention
itself is consistent (published name = `package.name`); only two
call sites typed the wrong form.

Two-layer fix in the client so peer-side typos can't break this
again:

- `default_asset_filename()` helper in `azt_collab_client.ui.update`
  derives ``<activity.getPackageName().rsplit('.', 1)[-1]>.apk`` from
  the running peer at runtime. `asset_filename` /
  `peer_asset_filename` in `check_for_update` /
  `install_apk_from_url` / `bootstrap` / `share_running_apk` are
  now keyword-optional and default to that helper. The recorder's
  call sites drop their explicit literals; new peers don't need
  to pass the name at all.
- **Resilient fallback** for peers that DO pass an explicit name
  (forks, older peer code rebuilt against the new client without
  having dropped the literal): the asset-lookup paths in
  `check_for_update` and `install_apk_from_url` now retry with
  the runtime-derived name when the explicit name isn't found in
  the release. `install_apk_from_url` further sources the download
  URL from the release JSON's `browser_download_url` rather than
  the caller-baked `releases/latest/download/<name>` URL, so a
  wrong peer-baked literal can't poison the actual download.
  Logged via `[update] explicit asset … not in release; falling
  back to derived …` so the drift is visible in logcat.
- Forks publishing under a non-default scheme still pass
  `asset_filename=` and get exactly that — fallback only fires
  when the explicit name returns no match.

### azt_collab_client 0.33.7 — docs/ cleanup: prune shipped plans, organise residual work
- **``docs/daemon_boot_plan.md``** rewritten as status-first.
  Phase A and Phase B2 marked SHIPPED with measured outcomes;
  Phase B1 + Phase C trimmed to "not shipped / not worth
  shipping unless …" notes with the trigger conditions
  spelled out. Cost-model speculation replaced with measured
  numbers from R500-class slow tablet (2026-05-09 harness
  run).
- **``docs/github_connect_ux_audit.md``** —
  recommended-implementation-order list at the bottom
  refreshed: items #1–#7 are done/declined (audit-trail
  strikethroughs preserved per the doc's own rule); items
  #8–#13 re-prioritised in current order. No content removed.
- **``docs/p4a_hook_picker_intent.md``** reduced to a redirect
  stub. The PICK_PROJECT intent-filter injection it described
  shipped in v0.28.x and is now in
  ``p4a_hook.py:_inject_pick_project``.
- **``docs/STATUS.md``** added. One-page index of "what
  shipped recently" + "what's open, prioritised" across all
  the docs in this directory. Reference docs
  (``research_notes_2026-05.md``, ``test_plan.md``,
  ``CLIENT_INTEGRATION.md``) listed but not duplicated.

### azt_collab_client 0.33.6 — measurement-driven decisions documented (Q2 + Q3 answered)
- **Q2 (doze) ANSWERED.** Measured on R500 tablet
  post-Phase-B2: doze runs (peer wait 49-68ms, daemon boot
  600-770ms) statistically indistinguishable from baseline
  (45-66ms / 593-1131ms). The Android-15 issue was the
  freezer, not doze proper. Phase B2's
  ``BIND_ABOVE_CLIENT`` is sufficient; no
  foreground-service-with-type variant needed.
- **Q3 (prewarm) ANSWERED.** Daemon Python boot ~600ms
  steady-state, ~1.1s first-cold-of-session. Prewarm
  overlap window ~1.9s. With prewarm, daemon boot fits
  entirely inside Kivy init; peer wait is **~50–60ms**.
  Without prewarm, peer wait would be the full
  ~600–1000ms.
- ``CLIENT_INTEGRATION.md`` § 3 reframed: prewarm in
  ``App.build()`` is now **required** for every peer (was
  "optional, measure first"). Cost of always calling it is
  essentially zero on devices where it doesn't help, and
  it's a 10× UX improvement on slow tablets.
- ``docs/daemon_boot_plan.md`` Q2 + Q3 marked answered with
  the measured numbers; remaining content kept for context.

### azt_collabd 0.33.0 + azt_collab_client 0.33.5 — bindService alone bootstraps Python (Service.onCreate self-delivers onStartCommand)
- **0.33.4 wasn't enough.** The connector's peer-side
  ``startService`` ALSO threw
  ``BackgroundServiceStartNotAllowedException``: cold-start
  ``App.build()`` fires before the peer's UID has been
  promoted to foreground (logcat shows
  ``UidRecord{...CEM bg:+50ms}``). Android 12+ blocks even
  cross-package starts from that state.
- **Real fix: server APK side.**
  ``AZTServiceProviderhost.onCreate`` now overrides Service
  lifecycle to self-deliver ``onStartCommand`` with
  ``getDefaultIntent(this, "")``. PythonService's normal start
  path runs on every Service creation, including from
  ``bindService`` with ``BIND_AUTO_CREATE``. So peers can
  start the daemon with ``bindService`` alone — which
  Android allows from background contexts. No more
  cross-package ``startService`` needed at all.
- **Connector simplified.**
  ``AZTServiceConnector.ensureBound`` no longer constructs
  Intent extras or calls ``startService``. Just one
  ``bindService`` with ``BIND_AUTO_CREATE | BIND_ABOVE_CLIENT``.
  All the Python-startup logic lives in the server APK's
  Service ``onCreate`` override.
- **Both APKs need rebuild + reinstall.** Server APK gets the
  new ``onCreate``; peer gets the simplified connector. Server
  APK bumped to 0.33.0 (lock-step with this change set).
- **Backward compat.** Legacy callers that still call
  ``startService`` (e.g., ``AZTCollabProvider.onCreate``'s
  fallback start, foreground caller paths) hit
  ``PythonService.onStartCommand`` which short-circuits when
  ``mService != null`` — Python only starts once regardless of
  start-path mix.

### azt_collab_client 0.33.4 — connector also startService (Android 12+ background-start fix)
- **Smoking gun on R500.** Logcat showed
  ``W ActivityManager: Background start not allowed: service
  Intent { cmp=...AZTServiceProviderhost }`` followed by
  ``E AZTCollabProvider:
  BackgroundServiceStartNotAllowedException`` thrown from
  ``AZTServiceProviderhost.start`` invoked from
  ``AZTCollabProvider.onCreate``. Android 12+ blocks
  ``startService`` from background contexts; the server APK's
  ``:provider`` lazy-spawn is a background context, so the
  Provider's ``onCreate`` self-start has been silently failing
  on every cold call. Python never started → no
  ``[boot-trace-daemon]`` lines, every peer compat probe got
  ``daemon_not_ready``, every B2 test was running against a
  daemon that had never finished init.
- **Fix.** ``AZTServiceConnector.ensureBound`` now does a two-
  step startup from the peer's *foreground* context (which
  Android allows): (1) ``startService`` with the full
  PythonService Intent extras (``serviceEntrypoint=service.py``
  etc., mirrored from
  ``AZTServiceProviderhost.getDefaultIntent`` on the server APK
  side; ``createPackageContext`` resolves the server APK's
  ``filesDir`` so ``androidPrivate`` / ``pythonHome`` /
  ``pythonPath`` aim at the right tree); (2) ``bindService``
  for OOM priority + freezer mitigation as before. The
  Provider's onCreate self-start stays in place as a no-op
  fallback for non-peer callers but is no longer load-bearing.
- **Why ``Provider.onCreate``'s start fails but the connector's
  start succeeds.** The Provider's runs in the server APK's
  own background process; the connector's runs in the peer's
  foreground process. Android 12+ allows the latter.

### azt_collab_client 0.33.3 — prewarm now binds on the main thread (worker JNI classloader scope fix)
- **Symptom.** R500 logcat showed
  ``[android_cp] AZTServiceConnector.ensureBound failed:
  ClassNotFoundException`` from the prewarm worker even after
  the connector ``.java`` was confirmed compiled into the APK
  (verified by ``unzip -p classes.dex | grep AZTServiceConnector``).
  The bind eventually succeeded — but only later, from the main
  thread via some other ``rpc.call`` path. So prewarm wasn't
  actually doing its B2 job: by the time the bind landed, the
  daemon's ``:provider`` process had already started cold-spawn
  with no priority hint.
- **Cause.** ``threading.Thread`` workers on Android attach to
  the JVM with the system bootclassloader, not the app
  classloader; pyjnius's ``autoclass`` calls from those workers
  can't find app-defined classes like our connector.
- **Fix.** ``prewarm()`` now does the autoclass + ``ensureBound``
  call synchronously on the caller's thread (typically the
  peer's ``App.build()`` main-thread context) BEFORE spawning
  the worker that does ``check_server_compat``. The bind is
  active at the earliest possible cold-start moment instead of
  racing the daemon's lazy-spawn. Worker still calls
  ``check_server_compat`` so the daemon-warmup retry loop has
  something to do; its (still failing) autoclass in
  ``discover()`` is a logged no-op since the bind is already
  in place.

### azt_collab_client 0.33.2 — point peer's android.add_src at canonical path, not the symlink
- 0.33.1 said ``android.add_src = android/src/main/java`` (via
  the peer's ``android/`` symlink). User report: still
  ``ClassNotFoundException`` on rebuild. Diagnosis: buildozer's
  ``android.add_src`` does not reliably follow symlinks; the
  copy/merge step lands an empty tree (or a broken symlink) in
  the dist's ``src/main/java/``.
- ``CLIENT_INTEGRATION.md`` § 2 now mirrors the server APK's
  approach: ``android.add_src = ../azt-collab/android/src/main/java``
  — pointing at the canonical filesystem path directly,
  bypassing the symlink. Brute-force fallback (copy the file
  into the peer repo) documented but flagged as brittle.

### azt_collab_client 0.33.1 — document peer's android.add_src requirement for B2
- 0.33.0 framing said "no peer code change required" — true for
  Python source, but peers DO need a one-line ``buildozer.spec``
  addition (``android.add_src = android/src/main/java``) for the
  new Java connector to compile into their APK. Otherwise:
  ``[android_cp] AZTServiceConnector.ensureBound failed:
  ClassNotFoundException`` on every cold start, bind never
  happens, freezer mitigation degrades to pre-B2.
- ``CLIENT_INTEGRATION.md`` § 2 now lists the line as required;
  § 3's "Automatic since 0.33.0" subsection points at the spec
  line when troubleshooting the ClassNotFoundException symptom.
- Server APK already had the equivalent line
  (``android.add_src = ../android/src/main/java`` in
  ``server_apk/buildozer.spec.tmpl``); peers historically didn't
  need one because the suite's Java tree was server-internal.
  B2 makes it shared.

### azt_collab_client 0.33.0 — Phase B2: peer holds bindService for OOM priority
- **Symptom this fixes.** R500-class Android-15 tablets showed
  endless ``daemon_not_ready`` 503s on cold start: peers
  triggered ``:provider`` lazy-spawn fine (Java provider
  responded), but Python's ``install_callbacks()`` never
  completed because the OS's app freezer suspended the cached
  ``:provider`` process mid-init. User-visible "AZT
  Collaboration not responding" + the manual "Open AZT
  Collaboration" workaround that papered over it.
- **Fix.** New peer-side Java connector
  ``android/src/main/java/org/atoznback/aztcollab/
  AZTServiceConnector.java`` issues ``bindService`` against
  the server APK's existing ``AZTServiceProviderhost`` with
  ``BIND_AUTO_CREATE | BIND_ABOVE_CLIENT``. Inheriting the
  peer's foreground priority defeats the freezer; the
  ``:provider`` process stays alive and Python finishes init.
  Bonus: ``:provider`` stays warm across the peer's session,
  so 2nd / 3rd / Nth RPCs in the same session don't re-pay
  daemon-cold-start.
- **Wire-up.** ``azt_collab_client/transports/android_cp.py``'s
  ``discover()`` calls ``Connector.ensureBound(activity)`` after
  the canonical ping succeeds. Idempotent; the connector is a
  static singleton. Async — we don't wait for
  ``onServiceConnected``; the existing compat-probe retry loop
  handles the bind-vs-Python-init race naturally.
- **No peer code change required.** Every peer that imports
  the client gets the new behaviour by virtue of bumping the
  bundled client. ``CLIENT_INTEGRATION.md`` § 3 documents the
  automatic behaviour + the diagnostic surface
  (``AZTServiceConnector.isBound()`` and ``dumpsys activity
  processes`` priority bucket).
- **No server APK change required.** ``AZTServiceProviderhost.
  onBind`` was already returning a stub ``Binder`` and tracking
  ``sBoundCount`` from the original sticky-bound design;
  peers were just never binding. Server APK can stay at 0.32.1.
- **Plan-doc** (``docs/daemon_boot_plan.md``) updated to mark
  B2 shipped + record the verification commands.

### azt_collab_client 0.32.2 — document prewarm + boot-trace harness in CLIENT_INTEGRATION.md
- New § 3 sub-section "Optional: pre-warm in ``App.build()``"
  documenting the ``prewarm()`` hook, its tradeoff, and the
  sentinel / env-var toggles for measurement runs. Peers
  considering cold-start tuning find the integration shape
  here rather than having to read ``bootstrap.py`` source.
- New § 13 sub-section "Boot-trace instrumentation" listing
  the peer + daemon phase labels and warning maintainers not
  to filter ``[boot-trace-*]`` lines out of their logcat
  pipelines.
- New § 13 sub-section "Cold-start measurement harness"
  pointing at ``tests/integration/measure_boot.sh`` +
  ``tests/integration/README.md`` and giving a default
  threshold (`peer wait` > 5 s on the slow-tablet target) for
  when to wire ``prewarm()`` before tagging a peer release.
- Also: ``measure_boot.sh`` switched from ``monkey`` to
  ``am start -W`` for launching the peer (with monkey as a
  fallback). monkey reports nonzero exits in cases that are
  actually fine — its exit code reflects internal event
  counts, not just dispatch success — and the script's
  ``set -e`` was killing the run after iteration 1.
  ``am force-stop`` and ``adb logcat -c`` now also tolerate
  nonzero exits.

### azt_collabd 0.32.1 + azt_collab_client 0.32.1 — boot-trace instrumentation + prewarm hook + measurement harness
- **Daemon-side ``_boot_trace``** in ``server_apk/service.py``
  emits ``[boot-trace-daemon] phase=<label> t=<elapsed>`` at
  every cost-center: ``module_loaded``, ``main_entered``,
  ``before_import_azt_collabd`` /
  ``after_import_azt_collabd``, ``configured``,
  ``before_install_callbacks`` / ``after_install_callbacks``,
  ``before_reconcile`` / ``after_reconcile``,
  ``entering_idle_loop``. Cheap; safe to leave on (≈ 10 lines
  per cold start).
- **Peer-side ``_boot_trace``** in
  ``azt_collab_client/ui/bootstrap.py`` mirrors with
  ``[boot-trace-peer] phase=<label>``: ``bootstrap_called``,
  ``compat_probe attempt=N``, ``compat_ok``,
  ``bootstrap_done``, plus prewarm phases.
- **New ``azt_collab_client.ui.bootstrap.prewarm()`` hook**:
  peers call it from ``App.build()`` to fire a single
  ``check_server_compat`` on a background thread, overlapping
  daemon lazy-spawn with Kivy initialisation. Idempotent;
  no-op on non-Android. Toggleable for measurement via
  ``$AZT_HOME/_no_prewarm`` sentinel or ``AZT_BOOT_PREWARM=0``
  env var so the harness can compare scenarios on the same
  APK without rebuilding.
- **Measurement harness** at
  ``tests/integration/measure_boot.sh`` drives a real device
  through ``baseline``, ``doze``, ``prewarm``, and
  ``doze+prewarm`` scenarios, capturing logcat boot-trace
  lines and producing per-iteration summaries via
  ``tests/integration/parse_boot_traces.py``. Doze is forced
  via ``dumpsys deviceidle force-idle``; prewarm toggling via
  the sentinel file (peer must be debuggable for ``run-as``).
  README at ``tests/integration/README.md`` documents
  prerequisites + scenario semantics.
- **Plan-doc updated** (``docs/daemon_boot_plan.md``):
  open-questions Q2 (doze) and Q3 (prewarm) now have explicit
  measurement plans pointing at the harness; Q1 (loopback
  ``kind``) remains as a deferred Phase A loose end.

### azt_collab_client 0.32.0 — daemon-warmup Phase A: adaptive backoff, diagnostic surface, fail-fast on null bundle
- **Adaptive backoff** in ``bootstrap._check_server``'s warmup
  retry loop. Replaces the fixed 2s interval with a schedule
  that ramps short → long: 0.2s, 0.4s, 0.8s, 1.6s, then plateaus
  at 2.0s. Fast devices that have a daemon ready by attempt 2
  now land in <1s instead of paying 2s+; slow devices keep the
  same ~60s total budget.
- **Diagnostic surface in the connecting popup.** New detail
  line under the "Connecting to AZT Collaboration service…"
  header shows ``Attempt N of 30  ·  Xs elapsed  ·  <kind>``
  where ``<kind>`` is the transport's coarse failure category
  (``daemon_not_ready`` while Python boots, ``null_bundle`` on
  signature-grant denial, etc.). Updates each retry. The
  unresponsive popup also surfaces last-error kind, total wait,
  and ``PackageManager``-reported server APK versionName, so the
  user / maintainer-email loop has actionable detail without
  needing adb access.
- **Fail-fast on ``null_bundle``.** Previously every
  ``ServerUnavailable`` was retried for the full 60s budget,
  including ``ContentResolver.call`` returning ``null`` — which
  is structurally unrecoverable (signature mismatch, provider
  authority missing). After 3 consecutive ``null_bundle``
  responses (≈0.6s on the new schedule) we jump to the
  unresponsive popup so the user can act on the real problem.
  ``daemon_not_ready`` and any other progress-bearing kind reset
  the streak — those still get the full warmup.
- **``ServerUnavailable.kind``** added to
  ``azt_collab_client.transports``. Recognised values:
  ``daemon_not_ready``, ``null_bundle``,
  ``server_apk_not_installed``, ``http_5xx``, ``transport_error``,
  ``http`` (loopback), ``''`` (unspecified). Existing call sites
  that ``except ServerUnavailable`` keep working; new sites can
  read ``ex.kind`` for fail-fast vs keep-retrying decisions.
  ``check_server_compat`` threads it into the result dict
  (``compat['kind']``).
- **Phase B + C planned** in ``docs/daemon_boot_plan.md``:
  provider-state in 503 body, ``bindService`` for OOM priority,
  optional daemon-side lazy imports if the new diagnostics show
  ``import azt_collabd`` is the dominant cost on slow tablets.

### azt_collab_client 0.31.5 — reframe smooth-UI section as a principle, not a recipe
- Per maintainer ask: § "Smooth UI across reloads" in
  ``CLIENT_INTEGRATION.md`` was framed as a
  recorder-specific recipe. Rewritten to lead with the
  **principle** (peers across the suite, whatever their
  model layer): same context; visible changes evident
  (including real upstream deletions, which propagate
  normally — LIFT workflows rarely delete but the principle
  doesn't paper over it); no other navigation; **suspend
  client-side filters that would hide the current view**
  (the failure mode is e.g. a "don't show past data" toggle
  excluding an entry the user is mid-edit because the data
  clock advanced — drop the filter for this view rather
  than swap the entry out). Recorder-flavoured snippet
  retained as one concrete realisation, not the contract.

### azt_collabd 0.31.2 + azt_collab_client 0.31.4 — sync fast-forward writes working tree; clone URL prefilled
- **Bug.** User report: "collaboration between clients on a
  project on two different phones is not smooth — each phone
  tracks its own changes but is unaware of others, even
  apparently when the user clicks on sync."
- **Cause.** ``_sync_repo_locked``'s fast-forward branch
  updated ``repo.refs[branch_ref] = remote_sha`` but never
  materialised the new tree to the working directory. Phone B
  fetched Phone A's commits, fast-forwarded the branch ref,
  but the LIFT file on disk stayed at Phone B's pre-sync
  bytes. Peers reading via ``LiftHandle`` got stale content
  and the UI looked unchanged. The diverged-merge branch was
  fine (``_merge_diverged`` already writes blobs to the
  working tree); only the fast-forward branch was the
  silently-broken case.
- **Fix.** New helper
  ``azt_collabd.repo._apply_tree_to_workdir(repo, project_dir,
  old_sha, new_sha)`` walks the diff between the two trees,
  writes added/modified blobs to the working tree, removes
  files that are gone in the new tree, and resets the index
  via ``repo.reset_index(new_tree)`` (with a ``_stage_all``
  fallback for older dulwich). Called from the fast-forward
  branch after the ref update. Diff-driven so unrelated
  untracked files (audio recordings the user just made and
  hasn't committed yet) aren't disturbed.
- **Peer-side principle documented** (peer follow-up, not in
  this bump). When the on-disk bytes change underneath a
  peer (``S.PULLED`` after sync, future ``MERGED_REMOTE``,
  re-clone, etc.), the user's view refreshes *in place*:
  same screen / entry / scroll position, fresh content,
  nothing else moves. If the entry the user is viewing was
  deleted upstream, keep the in-memory copy visible with a
  non-blocking notice rather than yanking them to a blank
  state. Sync is a refresh, not a navigation event. Spelled
  out as a principle (not a recipe) in § "Smooth UI across
  reloads" of ``CLIENT_INTEGRATION.md`` so each peer
  implements it through whatever model layer it has.
- **Clone-URL popup pre-fill.** ``clone_url_popup``'s URL
  field is now pre-populated with ``https://github.com/`` so
  phone-keyboard users can paste / type just ``owner/repo``
  instead of the full URL. Cursor lands at the end on open
  via ``Clock.schedule_once``. Submit-time guards: refuse
  empty / prefix-only input; if the user pasted a full URL
  *after* the prefix without first overwriting (so the field
  reads ``https://github.com/https://github.com/owner/repo``),
  take the rightmost protocol marker as the real URL start.

### azt_collab_client 0.31.3 — document grant_collaborator in CLIENT_INTEGRATION.md
- New § 10 "Granting collaborator access" in
  ``azt_collab_client/CLIENT_INTEGRATION.md``: covers the peer
  integration pattern (per-project settings only, never global),
  the project-disambiguation guarantee (peers pass langcode, the
  daemon resolves the repo — peers must NOT pre-resolve URLs),
  the full Result-status code list, translation pointer, and
  v1 scope (GitHub-only, invite-only, default ``push``).
- ``grant_collaborator_popup`` added to the "What the suite does
  *for* you" reference list at the bottom of the contract.
- Recovery / Testing sections renumbered 11 / 12.

### azt_collabd 0.31.1 + azt_collab_client 0.31.2 — grant-collaborator endpoint + popup
- **New endpoint** ``POST /v1/projects/<lang>/collaborators`` —
  invites a GitHub user as a collaborator on the repo backing
  ``langcode``. Looks the repo up via the project's
  ``remote_url`` so peers only have to pass a langcode (project
  disambiguation guaranteed server-side; no chance of peer-side
  URL handling targeting the wrong repo). Body:
  ``{username, level='push'}``.
- **Refactored** ``auth.add_collaborator`` to return ``'invited'``
  / ``'already'`` and raise on real errors (was: silent print +
  swallowed). The internal caller in ``repo._publish_repo``
  already wraps in ``try/except`` so its fire-and-forget
  semantics are preserved.
- **Status codes added** in both ``azt_collabd/status.py`` and
  ``azt_collab_client/status.py``:
  ``COLLABORATOR_INVITED``, ``COLLABORATOR_ALREADY``,
  ``COLLABORATOR_INVITE_FAILED``, ``INVALID_USERNAME``,
  ``NOT_GITHUB_REMOTE``. Plus translations in
  ``azt_collab_client/translate.py``.
- **Client wrapper** ``azt_collab_client.grant_collaborator(
  langcode, username, level='push')`` returns a ``Result``;
  re-exported from ``__all__``.
- **Reusable popup** ``azt_collab_client.ui.grant_collaborator_popup(
  langcode, on_done=None, font_name=...)``. Opens a popup that
  displays the project's langcode + remote URL prominently
  (project disambiguation is the load-bearing UX guarantee
  here), takes a username, calls ``grant_collaborator``, and
  surfaces translated outcomes. Auto-dismisses 2 s after
  success / "already a collaborator"; stays up on failures so
  the user can retry.
- **Peer integration** is per-peer (recorder / viewer / future):
  add a button to the project-context settings surface that
  calls ``grant_collaborator_popup(langcode=<current>)``. The
  button belongs in *project* settings, not global settings —
  the operation is meaningless without a specific project.
- **v1 scope.** GitHub-only (GitLab has different invite
  semantics; can be added by extending
  ``_parse_github_owner_repo`` and the dispatch). Invite-only
  (no list-existing or revoke yet, but the popup screen leaves
  room for either if you want them later).

### azt_collab_client 0.31.1 — Android 15 process-freezer workaround
- **Symptom.** On a budget Android 15 tablet (R500_V_US),
  cold-start peers showed "Connecting to AZT Collaboration
  service…" for the full 60 s daemon-warm-up budget, then
  fell through to "AZT Collaboration not responding."
  Verified: with the server APK launcher activity in the
  foreground, the same peer reaches the daemon in 5–10 s.
- **Cause.** Android 15's app freezer keeps the server APK's
  ``:provider`` process frozen even after a peer's
  ``ContentResolver.call`` triggers lazy-spawn — Python
  callbacks never finish registering inside the warm-up
  budget. Yesterday-vs-today framing was inconclusive; this
  is plausibly always-broken on certain ROMs and only
  surfaced today.
- **Workaround.** ``install_server_apk_popup`` gains an
  ``on_open_app`` parameter; when set it adds an "Open AZT
  Collaboration" button. ``_prompt_server_unresponsive``
  wires this to a callback that fires
  ``PackageManager.getLaunchIntentForPackage`` for
  ``org.atoznback.aztcollab``, then re-enters ``_check_server``
  on a 2 s delay with a fresh retry budget + a re-shown
  connecting popup. The launcher activity foregrounding
  un-freezes the package's process group, so when the user
  switches back to the peer, the next compat probe lands.
  Cheaper recovery than reinstalling the server APK.
- **Real fix later.** Peers should ``bindService`` to
  ``AZTServiceProviderhost`` while foregrounded so OOM
  priority prevents freezer interference in the first place.
  That's a Java change; deferred.

### azt_collabd 0.31.0 + azt_collab_client 0.31.0 — minor bump for pre-distribution test
- Lock-step minor bump on both packages, with both floors moved
  to 0.31.0 (``MIN_CLIENT_VERSION`` on the daemon,
  ``MIN_SERVER_VERSION`` on the client). Folds in everything
  since 0.30.0: server-APK boot-on-lazy-spawn, sticky-bound
  ``:provider`` host, self-update auto-exit, GitHub-connect UX
  rewrite (state-machine + suspended-install detection +
  Verify-setup re-test affordance), pre-install APK validation
  (parse + signature + asset.digest cache freshness), bootstrap
  flow with mandatory vs voluntary distinction, server→peer
  language mirror, blocked-popup mailto link + Check-again,
  same-tag re-upload detection via ``last_seen_digest``, install
  poll lifecycle, suite-wide CONFIRMED-vs-CONNECTED gating, and
  the various translation additions and Kivy touch-routing fixes.
  Anything older talking to a 0.31 peer (or vice versa) gets a
  clean ``client_too_old`` / ``server_too_old`` and is routed
  through the bootstrap update flow.

### azt_collab_client 0.30.47 — mandatory-mode probe always forces prompt
- **Regression from 0.30.46.** Recording ``gh_digest`` on
  Update-tap fixed the voluntary loop, but introduced a hole on
  the mandatory path: if the install failed (user cancelled at
  the Android installer screen, signature mismatch, etc.), the
  next ``_probe`` saw ``last_seen == gh_digest`` →
  ``digest_changed=False`` → ``needs_update=False`` →
  ``_show_no_newer_release`` fired even though GitHub actually
  has the right bytes (we just failed to install them).
- **Fix.** Replaced ``legacy_mandatory_force`` (only handled
  the first-run unknown-baseline case) with ``mandatory_force``
  (always forces the prompt in mandatory mode when the release
  feed returned something to download). Digest comparison is for
  "is there something new to offer?" — irrelevant when the daemon
  has already declared the client too old.
- **Clarification.** The 0.30.46 changelog framing "stuck at
  old version with no prompt" was sloppy. Voluntary install fail
  is benign — the client was already compatible (otherwise the
  daemon would have returned ``client_too_old`` and the mandatory
  path would have been used). The user runs a working older
  build and can retry via the peer's in-app Update button.
  Mandatory path is enforced terminal — no chance of leaking the
  user into project loading without a working daemon.

### azt_collab_client 0.30.46 — record gh_digest as last_seen on Update tap
- **Bug.** User report: "I click update on a voluntary screen,
  and it doesn't download… because it seems to be running from
  cache, this window keeps coming up since the digest is still
  different from gh." Trace:
  ``last_seen='3687f3…' gh_digest='efb3ae…'
  version_newer=False digest_changed=True``.
  Cache check correctly skipped the download (bytes already at
  ``efb3ae…``), install fired, but ``_record_last_seen_digest``
  was never called outside the first-run baseline branch — so
  ``last_seen`` stayed at ``3687f3…`` forever. Next bootstrap
  saw ``digest_changed=True`` and re-prompted.
- **Fix.** ``_prompt_self_update`` now receives ``gh_digest``
  from ``_probe`` and records it as the new ``last_seen`` baseline
  on Update-button tap. Recording at tap time (vs. install-complete)
  is the practical compromise: same-tag re-uploads don't flip
  versionName, and self-installs kill our process during install,
  so we have no reliable in-process completion signal. If the
  install ultimately fails the user is stuck at the old version
  with no further prompt — recoverable via the in-settings
  Update button or a manual reinstall.

### azt_collabd 0.30.45 + azt_collab_client 0.30.45 — re-bump for mandatory-update test pass
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.45** to force the
  ``client_too_old`` path on any peer bundling an older client
  (continuing test pass for the digest-change decline fix).

### azt_collabd 0.30.44 + azt_collab_client 0.30.44 — decline-memory ignores same-tag re-uploads
- **Daemon ``MIN_CLIENT_VERSION`` bumped to 0.30.44** to force the
  ``client_too_old`` path on any peer bundling an older client
  (test pass for the digest-change decline fix).
- **Bug.** User report: "Check again doesn't currently show a new
  apk online, despite a different sha256." Trace confirmed the
  probe was correctly setting ``digest_changed=True``
  (``last_seen='3687f35493c3'`` ≠ ``gh_digest='cceb3fc2ba05'``),
  yet no prompt appeared. Cause: the decline-memory check in
  ``_peer_update_with_confirm._probe`` only compared the version
  tag — a previous "Not now" tap against ``1.37.24`` masked
  every subsequent re-upload at the same tag, regardless of
  digest. The original comment ("a re-upload of a declined
  version still plausibly came with the user's prior decline
  intact") turned out to be wrong: a re-upload IS a different
  build the user has not been asked about.
- **Fix.** Decline check now skips when ``digest_changed`` is
  True, treating the new bytes as a fresh release. Also
  belt-and-braces gates the check on ``not mandatory`` so the
  ``client_too_old`` path can't be silenced by a stray decline
  entry from an earlier voluntary cycle.

### azt_collabd 0.30.43 + azt_collab_client 0.30.43 — mirror daemon UI language to peer + clearer mandatory-update wording
- **Language sync server → peer.** User report: switching the
  server APK's settings UI to French didn't translate any
  bootstrap-side popups in the peer (recorder / viewer).
  Cause: ``$AZT_HOME/config.json::ui.language`` is the
  persistence path, but on Android ``$AZT_HOME`` is per-process
  private (server APK has its own filesDir, each peer has its
  own), so file-system writes from the server's settings UI
  never reached peer disk. Fix:
  - New daemon endpoint ``GET /v1/config/ui_language`` returns
    the server-side persisted language (handler
    ``_h_get_ui_language`` in ``azt_collabd/server.py``).
  - New client wrapper ``get_server_ui_language()`` in
    ``azt_collab_client/__init__.py``.
  - ``bootstrap._sync_ui_language_with_daemon()`` runs at
    ``_check_server`` entry (immediately after a successful
    ``check_server_compat``) and applies the daemon's language
    via ``i18n.set_language`` so all peer-side popups + status
    text track. Best-effort: silent on RPC failure, peer
    keeps its local pref.
- **Mandatory-update wording.** User report: viewer at 0.8.2
  saw "AZT Viewer 0.8.2 is required" — confusing when 0.8.2
  is the current version (same-tag re-upload case where
  digest changed). Now branches in
  ``_prompt_self_update``:
  - ``latest_version != peer_version`` (genuinely newer
    release): "{name} {peer_v} is too old for the AZT
    Collaboration service. Tap Update to install {name}
    {latest}, or Quit to close this app." Both versions named.
  - ``latest_version == peer_version`` (same-tag re-upload —
    digest_changed=True triggered the prompt): "A new build
    of {name} {version} is available. The current build is
    too old for the AZT Collaboration service. Tap Update to
    install the new build, or Quit to close this app." No
    longer phrases the same version as both "what you have"
    and "what's required".
- Email-link-in-the-update-popup ask is deferred —
  ``install_server_apk_popup``'s body Label doesn't have
  ``markup=True`` yet, and the change isn't a one-liner.
  Tracked separately.

### azt_collabd 0.30.42 + azt_collab_client 0.30.42 — bump MIN_CLIENT_VERSION to 0.30.42
- Final test-pass bump on the daemon (``MIN_CLIENT_VERSION =
  "0.30.42"``) to exercise the mandatory-self-update flow now
  that ``_prompt_self_update`` calls ``App.stop()`` on
  install completion. Server APK rebuild required; peer at
  0.30.41 trips ``client_too_old`` and gets the mandatory
  Update / Quit popup.
- Drop ``MIN_CLIENT_VERSION`` back to a real-world floor
  before any release that ships in the public update
  channel. Same goes for ``MIN_SERVER_VERSION`` on the client
  side.

### azt_collabd 0.30.41 + azt_collab_client 0.30.41 — close peer cleanly after a self-install
- User report: "When I clicked update, it downloaded and
  installed fine, but then I found myself back at the same
  popup. I closed and restarted fine, but users shouldn't have
  to do that." Android usually kills the running peer during a
  self-install, but not always — on some devices the peer
  survives, comes back to foreground after the system
  installer dismisses, and the popup is right where it was.
  No way to recover without a manual restart.
- Fix: ``_prompt_self_update`` now passes the peer's own
  package name as ``install_target_package`` (read from
  ``PythonActivity.mActivity.getPackageName()``) so
  ``install_apk_from_url`` runs the post-install poll loop
  on it, and ``on_install_complete`` calls
  ``App.get_running_app().stop()`` so the running peer exits
  the moment ``PackageManager`` reports the new versionName.
  Next user launch lands on the new APK; no manual restart.
- Safe both ways: if Android kills us during install (the
  common case), the poll thread dies with us — no leak. If
  Android doesn't kill us, the poll fires, we self-stop. The
  pre-0.30.41 comment about "polling our own package would
  block forever" was wrong: while the system installer is in
  foreground the Kivy Clock pauses, but it resumes when we
  come back, the poll detects the change, and we exit.

### azt_collabd 0.30.40 + azt_collab_client 0.30.40 — distinguish mandatory peer self-update from voluntary
- ``_prompt_self_update`` was using the same body + dismiss
  action for both the voluntary "newer version available" path
  and the mandatory ``client_too_old`` path. Declining a
  mandatory update via "Not now" silently fell through to
  ``on_done`` and dropped the user into the peer with a daemon
  that wouldn't talk to them.
- New ``mandatory`` parameter on ``_prompt_self_update``
  (forwarded from ``_check_self`` via
  ``_peer_update_with_confirm``):
  - **Voluntary** (``mandatory=False``, default): existing body
    "A newer version of this app ({version}) is available." —
    Update button + Not now (dismiss). Decline memory still
    applies so we don't re-prompt for the same version.
  - **Mandatory** (``mandatory=True``, the ``client_too_old`` +
    newer-version-exists path): body "{name} {version} is
    required to use the AZT Collaboration service. Tap Update
    to download and install it, or Quit to close this app." —
    Update button + Quit (action=``'quit'``, peer
    ``App.stop()``). No decline memory: a mandatory update
    can't usefully be remembered as "declined".
- Net effect: declining a mandatory update closes the app,
  matching the ``_show_release_too_old`` /
  ``_show_no_newer_release`` Quit semantics. Symmetric with
  ``_prompt_server_update``'s "Update" button + the existing
  install popup's Quit button on the server-side path.

### azt_collabd 0.30.39 + azt_collab_client 0.30.39 — bump MIN_CLIENT_VERSION to 0.30.39
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.39)
  so a peer at 0.30.38 paired with this server APK trips
  ``client_too_old``. Continues exercising the new
  digest-aware probe + version-anchor popup from 0.30.37/.38.
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.38 to actually trigger client_too_old.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.38 + azt_collab_client 0.30.38 — detect same-tag re-uploads via asset digest persistence
- Per maintainer ask: the version-tuple "any newer release"
  probe in ``_peer_update_with_confirm._probe`` couldn't see a
  re-uploaded asset on the same release tag. Common in dev
  iteration (push fix, keep tag), and unavoidable when a tag
  is hot-fixed in place. The new branch catches it via a
  digest-change check.
- ``_last_seen_digest(repo)`` and
  ``_record_last_seen_digest(repo, digest)`` persist the
  GitHub ``asset.digest`` per peer repo via the existing
  ``peer_pref`` / ``set_peer_pref`` store
  (``peer_prefs.last_seen_digests`` dict, keyed by
  ``owner/repo``).
- ``_probe`` now reads ``asset.digest`` from the latest
  release JSON, compares against the persisted last-seen, and
  treats EITHER a newer version tag OR a changed digest as
  "newer available". A trace line ``[bootstrap] _probe …
  version_newer=… digest_changed=…`` prints both signals so
  flaky cases are diagnosable from logcat.
- First-run baseline: if no digest is on file for ``repo``,
  the current GitHub digest gets recorded as the starting
  point so subsequent probes can detect change. Misses the
  perverse "re-uploaded between install and first launch"
  edge case but covers the dev-iteration use case cleanly.
- Storage scope: ``peer_prefs`` writes to whatever
  ``$AZT_HOME/config.json`` resolves to in the calling peer's
  process (peer-private on Android, shared on desktop), which
  is correct — each peer tracks its own repo independently.

### azt_collabd 0.30.37 + azt_collab_client 0.30.37 — fix client_too_old popup: version anchors + drop nonsensical pre-flight + Check-again tracing
- User report: "Recorder is too old" popup gave no version
  information either on screen or in the email body, and Check
  again seemed to do nothing.
- **Drop the pre-flight comparison in ``_check_self`` for the
  client_too_old path.** ``required_min`` from the daemon refers
  to the **client library** version (``azt_collab_client.
  __version__``), not the peer-app version. The recorder peer
  bumps its own version (1.34.0, …) independently of the client
  lib, so comparing recorder release tags to client-lib version
  numbers is meaningless — recorder 1.34.0 vs lib 0.30.36 has no
  defined order. The pre-flight either trivially passed or
  trivially failed depending on which way the major-version
  numbers happened to land. Replaced with the simpler "is there
  ANY newer peer release available" check that
  ``_peer_update_with_confirm`` already performs. We can't
  inspect a remote APK's bundled client-lib version without
  downloading it, so any-newer-version is the only honest signal.
- **Version anchors in the popup body and email.**
  ``_show_no_newer_release`` now takes ``required_client_lib``
  and ``bundled_client_lib`` and surfaces all four version
  values: peer name + peer version, bundled client lib (this
  build), and required client lib (from the daemon's compat
  handshake). Body reads e.g. "Recorder 1.34.0 is too old for
  the AZT Collaboration service. This build bundles client
  library 0.30.35; the service requires 0.30.36 or newer. No
  newer Recorder release is published yet." Email body lists
  the same anchors as labelled lines so the maintainer has the
  full mismatch in one read.
- **Check again tracing.** ``[bootstrap] Check again pressed —
  invalidating release cache + re-entering _check_server`` now
  prints when the button fires; ``_check_server`` is wrapped in
  try/except so any exception during the retry surfaces in
  logcat instead of silently dying in the worker thread. Lets
  us tell apart "Check again ran but result is the same"
  (visible re-render) from "Check again failed silently" (no
  trace lines after).

### azt_collabd 0.30.36 + azt_collab_client 0.30.36 — bump MIN_CLIENT_VERSION to 0.30.36
- ``MIN_CLIENT_VERSION`` raised to 0.30.36 (peer wandered to
  0.30.35, so we need to stay one ahead). Server APK rebuild
  required to pick up the new floor; peer at 0.30.35 trips
  ``client_too_old`` against this daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.35 + azt_collab_client 0.30.35 — bump MIN_CLIENT_VERSION to 0.30.35
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.35)
  so a peer at 0.30.34 paired with this server APK trips
  ``client_too_old``. Lets us exercise the new
  ``_show_no_newer_release`` popup (parity with
  ``_show_release_too_old``: Check again + mailto + Quit, no
  fall-through to ``on_done``).
- Server APK rebuild required to pick up the new floor; peer
  stays at 0.30.34 so the test triggers cleanly.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.34 + azt_collab_client 0.30.34 — Update-needed popup parity + don't drop into client on dismiss
- ``_show_info`` (the "Update needed" single-button popup that
  fired on the ``client_too_old`` + no-newer-release branch) had
  two problems:
  1. UI didn't match ``_show_release_too_old``: only an OK
     button, no mailto link, no Check-again.
  2. After the user tapped OK, ``_check_self`` fell through to
     ``_on_done_and_release`` — host loaded a project, daemon
     refused subsequent RPCs, user stuck in a half-broken UI.
- Refactor: extracted the popup body into
  ``_show_update_blocked_popup(ctx, body_text, mailto_subject,
  mailto_body)``. Two callers — the existing
  ``_show_release_too_old`` and a new
  ``_show_no_newer_release`` — share the same Check-again /
  Quit / ``[ref=email]`` mailto-link UI vocabulary. The "Update
  needed" / OK / fall-through popup is gone.
- ``_check_self``'s on_no_update force_prompt branch now
  surfaces ``_show_no_newer_release`` and **does not** call
  ``_on_done_and_release``. Popup is terminal — Quit stops the
  app via ``App.stop()`` (no half-broken UI), Check again
  invalidates the release cache + re-runs ``_check_server``.

### azt_collabd 0.30.33 + azt_collab_client 0.30.33 — bump MIN_CLIENT_VERSION to 0.30.33
- ``MIN_CLIENT_VERSION`` raised again on the daemon (now 0.30.33)
  so any peer at 0.30.32 or earlier paired with a 0.30.33 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Continues exercising the symmetric self-update flow now that
  the bootstrap install-cache fix is in.
- Server APK rebuild required to pick up the new floor; peer
  can stay at 0.30.32 (or older) to actually trigger
  client_too_old when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.32 + azt_collab_client 0.30.32 — install_apk_from_url: validate cache against GitHub digest
- User report: bootstrap reused a cached 0.30.28 server APK
  instead of downloading the published 0.30.29, then Android
  rejected the install with "package appears to be invalid"
  (downgrade attempt). Root cause: ``install_apk_from_url``
  is the direct-URL path (called from
  ``install_server_apk_popup`` which bootstrap dispatches),
  with no GitHub-API cross-check. ``_has_fresh_download`` ran
  in sidecar mode — cached file matched its sidecar SHA, so
  reused — but sidecar mode can't tell "intact bytes" from
  "intact-but-stale-version bytes". ``check_for_update`` got
  the digest-mode fix in 0.30.22; ``install_apk_from_url``
  was still on the old path.
- Fix: new optional ``repo`` parameter on
  ``install_apk_from_url``. When supplied, the worker fetches
  the GitHub release JSON, finds the matching asset's
  ``digest`` (sha256:hex), and threads it through to
  ``_has_fresh_download`` as ``expected_sha256``. Stale
  caches with the right SHA-vs-sidecar but wrong SHA-vs-
  GitHub now fall through to a fresh download. Failure of
  the metadata fetch (network glitch) falls back to sidecar
  mode rather than blocking the install.
- ``install_server_apk_popup`` accepts and forwards the
  ``repo`` parameter; bootstrap's four call sites
  (_prompt_server_install / _prompt_server_update /
  _prompt_server_unresponsive / _prompt_self_update) all
  pass ``ctx.server_repo`` or ``ctx.peer_repo`` as
  appropriate.
- Net: the same digest-driven cache-freshness story that
  applies to settings-screen "Update this app" now also
  applies to bootstrap's "Install / Update AZT
  Collaboration" popup and to peer self-update from the
  bootstrap path.

### azt_collabd 0.30.31 + azt_collab_client 0.30.31 — log SHA check result in _has_fresh_download
- ``_has_fresh_download`` ran the cache integrity check
  (digest-mode against GitHub's asset digest, sidecar mode
  otherwise) but did so silently — user reported "no SHA log
  line, is this a regression?" against 0.30.28. Test was
  running, just invisible.
- Added a single ``[update] _has_fresh_download: ...`` print
  per call that names the mode (digest / sidecar), shows the
  truncated file SHA + expected SHA, and the boolean match
  result. Also prints early-exit reasons (file missing, hash
  failed, sidecar missing, sidecar empty, sidecar read
  error) so a False return tells you *why*. No behavior
  change.

### azt_collabd 0.30.30 + azt_collab_client 0.30.30 — bump MIN_CLIENT_VERSION to 0.30.30
- ``MIN_CLIENT_VERSION`` raised to 0.30.30 on the daemon so any
  peer running 0.30.29 or earlier paired with a 0.30.30 server
  APK trips ``client_too_old`` in ``check_server_compat()``.
  Lets us exercise the symmetric ``_check_self`` /
  release-too-old / Check-again flow on the *peer* side
  (which previously we'd only proven for the server-too-old
  direction).
- Server APK rebuild required to pick up this floor change;
  peer can stay at 0.30.29 (or older) to actually trigger the
  client_too_old branch when it talks to the new daemon.
- Drop back to a real-world floor before any release that ships
  in the public update channel.

### azt_collabd 0.30.29 + azt_collab_client 0.30.29 — bump MIN_SERVER_VERSION to 0.30.28
- ``MIN_SERVER_VERSION`` raised to 0.30.28 so the latest peer
  build keeps triggering ``server_too_old`` against any server
  APK at 0.30.27 or earlier (continuing the testing thread for
  the release-too-old / Check-again flow).
- Drop back to a real-world floor before any release that
  ships in the public update channel.

### azt_collabd 0.30.28 + azt_collab_client 0.30.28 — debug rebuild
- No code change; bump for a fresh peer APK to retest the
  Check-again cache invalidation on the device.

### azt_collabd 0.30.27 + azt_collab_client 0.30.27 — invalidate release cache on Check again
- 0.30.26's Check again button re-ran ``_check_server`` but the
  per-process release-JSON cache (5-minute TTL on
  ``_release_cache``) returned the stale "too old" entry, so
  the popup re-rendered immediately against the same data and
  the user saw no change. Closing/reopening the peer wiped the
  cache and worked.
- Fix: new ``invalidate_release_cache(repo=None)`` helper in
  ``azt_collab_client/ui/update.py``. Bootstrap's Check-again
  handler drops both ``ctx.server_repo`` and ``ctx.peer_repo``
  entries before re-running ``_check_server`` so the next
  probe re-fetches GitHub.

### azt_collabd 0.30.26 + azt_collab_client 0.30.26 — release-too-old popup: mailto link + Check again
- "Or build from source" was unhelpful for the SIL field-
  linguist user base — they're not going to. Replaced with
  "or [send the developer an Email]" rendered as a Kivy-
  markup ``[ref=email]`` link styled with underline + accent
  blue. Tapping it opens the user's MUA via ``mailto:`` with
  a pre-filled subject ("{name}: required version not yet
  released") and body containing the version mismatch info.
  No-MUA degradation is graceful — Android shows the standard
  "no app to handle this" toast.
- New ``azt_collab_client.MAINTAINER_EMAIL`` constant
  (``kent_rasmussen@sil.org``) so forks can override in their
  own client build (no env-var hook yet — change at the
  source line, rebuild). Matches the address in
  ``SECURITY.md``.
- Added a "Check again" button alongside Quit. Dismisses the
  popup and re-enters ``_check_server`` on a worker thread —
  same code path as bootstrap's compat probe, so a
  freshly-published release that meets the floor flows
  through to the install popup without the user having to
  restart the peer.

### azt_collabd 0.30.25 + azt_collab_client 0.30.25 — pre-flight release version vs required minimum + clearer body text
- User reported: bootstrap got server_too_old, popped the
  "Update AZT Collaboration?" dialog, the user tapped Update,
  and the latest published release was *also* too old to
  satisfy ``MIN_SERVER_VERSION``. Result: download succeeds,
  install succeeds, peer hits ``server_too_old`` again. Wasted
  bandwidth + a confused user.
- Fix in ``azt_collab_client/ui/bootstrap.py``:
  - ``_release_meets_minimum(repo, required_min)`` fetches
    GitHub's latest tag for the given release feed and
    compares to the required floor. Returns ``(ok, latest,
    error)``.
  - ``_show_release_too_old(...)`` is a new one-button popup
    that surfaces "{name} {required} or newer is required, but
    the latest available release is {latest}. Wait for an
    update or build from source." with a Quit affordance.
    Replaces the install popup when the upstream release feed
    can't satisfy the floor.
  - ``_prompt_server_update`` now takes ``min_required``,
    pre-flights the server APK release feed, and surfaces the
    "release too old" popup instead of opening the install
    popup if upstream can't help. Body text on the install
    popup itself now reads "{name} {required} or newer is
    required (you have {current})" — replaces the redundant
    "AZT Collaboration service (AZT Collaboration)" wording
    the user flagged.
  - Symmetric pre-flight on the ``client_too_old`` path:
    ``_check_self`` accepts ``required_min`` (forwarded from
    the daemon's compat response) and runs the same release-
    feed check before going into ``_peer_update_with_confirm``.
    A peer running ahead of GitHub's latest publish gets the
    same "wait for an update" popup instead of a useless
    download.
- Client-only rebuild needed to exercise this — bootstrap
  runs in the peer's process. Server APK can stay at whatever
  older version is on the device (that's what triggers
  ``server_too_old`` in the first place).

### azt_collabd 0.30.24 + azt_collab_client 0.30.24 — debug rebuild + MIN_SERVER_VERSION = 0.30.24 to exercise old-server flow
- ``MIN_SERVER_VERSION`` bumped to 0.30.24 deliberately, so a
  peer carrying this client paired with a server APK at 0.30.23
  or earlier hits ``check_server_compat()`` →
  ``server_too_old``. Lets us walk the bootstrap "Update AZT
  Collaboration?" prompt end-to-end without backdating any real
  wire-format change. **Drop back to a real-world floor before
  any release that ships in the public update channel.**
- No other code change; daemon and client are otherwise identical
  to 0.30.23.

### azt_collabd 0.30.23 + azt_collab_client 0.30.23 — debug rebuild
- No code change; bump for a fresh APK to retest the
  ``asset.digest``-driven cache-freshness check on the device.

### azt_collabd 0.30.22 + azt_collab_client 0.30.22 — verify cached APK against GitHub's authoritative ``asset.digest``
- 0.30.21 caught stale-version caches via versionName parsing.
  Per maintainer suggestion: GitHub's REST release-asset
  metadata exposes a SHA-256 ``digest`` field
  (``"digest": "sha256:<hex>"``, added 2025-06-03), so we can
  do an authoritative cache-freshness check without paying for
  a re-download.
- ``_has_fresh_download`` now takes an optional
  ``expected_sha256`` arg. When supplied (asset has a digest),
  it's the strong check — file SHA must equal the GitHub
  digest to reuse. When not (legacy assets pre-2025-06), it
  falls back to the existing sidecar self-consistency check.
- ``_worker`` parses ``asset.digest`` (strips the ``sha256:``
  prefix) and threads it through. Catches three failure modes
  in one place:
  - same-version-different-bytes (re-uploaded asset),
  - corrupted on-disk cache,
  - stale-version cache from a previous Update cycle.
  The versionName check from 0.30.21 is kept as a belt-and-
  braces layer for legacy assets where the digest is null and
  the SHA fallback can't tell same-bytes-different-version
  from a normal cache hit.

### azt_collabd 0.30.21 + azt_collab_client 0.30.21 — invalidate cached APK when versionName ≠ latest
- User repro: tapped Update on 0.30.19 → cache reused → install
  ran but version stayed at 0.30.19 (the cached APK was from
  the previous update cycle). ``_has_fresh_download`` only
  validates that the cached file's bytes match the sidecar's
  SHA — i.e. on-disk integrity. It does NOT check that the
  cached file is the version we're now trying to install.
- Fix: in ``check_for_update``'s ``_worker``, after the
  fresh-download check passes, ``_apk_parse_info`` reads the
  cached APK's ``versionName`` and compares it to ``latest``.
  Mismatch → remove the cached file + sidecar so the
  download branch runs and we fetch the right version.
- Diagnostic line ``[update] cache stale: cached_version=...
  != latest=...; discarding ...`` prints when the discard
  fires, so future repros are visible in logcat.
- Why not also fix this in ``install_apk_from_url``: that
  path doesn't know the "expected version" (URL is
  redirect-style and opaque); peers using it typically run
  install-once via bootstrap, so cache staleness is much less
  of a problem.

### azt_collabd 0.30.20 + azt_collab_client 0.30.20 — log parse_info + signature_matches result
- 0.30.18's pre-install validation didn't tell us which check
  outcome we got — user reports "same invalid response" against
  0.30.18 + 0.30.19. Did the parse pass? Did signature compare
  match? Returned None? We can't tell from logcat without an
  explicit print at each point.
- Added two trace lines:
  - ``[update] parse_info: ok pkg=... versionName=... path=...``
    (or ``parse_info: None path=...``) right after
    ``_apk_parse_info`` runs. Surfaces the APK's actual package
    name and version so we can spot manifest-level mismatches
    (e.g. wrong ``packageName``).
  - ``[update] signature_matches_installed: True/False/None``
    right after ``_signature_matches_installed`` runs. ``True``
    = match (install should succeed signature-wise),
    ``False`` = mismatch (we'd surface our error and abort),
    ``None`` = couldn't determine (off Android, app not
    installed, jnius unavailable, exception path).
- No behavior change.

### azt_collabd 0.30.19 + azt_collab_client 0.30.19 — debug rebuild
- No code change; bump for a fresh APK to retest 0.30.18's
  pre-install validation + suspended-messaging on the device.

### azt_collabd 0.30.18 + azt_collab_client 0.30.18 — pre-install validation + step-by-step suspended messaging
- **Pre-install APK validation in ``check_for_update`` and
  ``install_apk_from_url``.** Before firing the install Intent
  we now run two checks via ``PackageManager``:
  - ``getPackageArchiveInfo(dest, GET_SIGNATURES)`` to confirm
    the downloaded APK is parseable. A null result means the
    download was truncated / corrupted; the cached file +
    sidecar are removed and the user gets a "could not be
    parsed; try again to re-download" error rather than the
    bare Android "package appears to be invalid" complaint
    after dispatching the Intent.
  - ``signature_matches_installed(dest, package)`` —
    compares the APK's signing certificate against the
    currently-installed app's certificate. On mismatch we
    surface "Downloaded APK is signed with a different key
    than the installed app... Uninstall first, then tap
    Update again — or rebuild from source with the matching
    keystore." That replaces the cryptic "App not installed
    as package appears to be invalid" Android error and
    points the user at both fixes.
  - Helpers (``_apk_parse_info``,
    ``_signature_matches_installed``,
    ``_installed_version_name``,
    ``_android_package_manager``) live alongside the existing
    install-Intent code in ``update.py``. All three return
    ``None`` off Android / when pyjnius is unavailable so the
    desktop / non-Android paths short-circuit cleanly.
- **Diagnostic line** at the start of ``_install_on_ui``:
  ``[update] pre-install check: pkg=... installed_version=...
  running_version=... latest=...``. Tells us whether the
  device-installed version diverges from the running code's
  ``__version__``. They should match in normal use; diverging
  values are a hot-patch / dev workflow signal.
- **Suspended-state messaging.** Old "Resume it at {url}"
  was unhelpful — gave a URL but no idea what to do on the
  page. ``GitHubConnectScreen`` now stashes the
  ``installation_id`` from the Verify-setup probe and:
  - ``install_app`` opens
    ``settings/installations/<installation_id>`` (the
    install's configure page directly) instead of the
    generic install URL.
  - The accompanying message reads "Tap 'Install GitHub App'
    below to open the install's configure page on GitHub,
    then scroll to the bottom and tap 'Unsuspend'." —
    walks the user through the actual GitHub UI flow.
  - ``_render_message`` and ``_test_done`` both branch on
    ``_suspended_installation_id`` so the message survives
    re-renders (language change, screen re-entry).
- ``S.APP_SUSPENDED`` translation (used by sync 403 path,
  which doesn't have the in-screen "Install GitHub App
  below" affordance) updated to the same self-contained
  step-by-step shape: "GitHub App installation is suspended
  at {url}. Open it, scroll to the bottom, and tap
  'Unsuspend'."

### azt_collabd 0.30.17 + azt_collab_client 0.30.17 — restore Share icon
- 0.30.11's Share/Update simplification dropped the
  share-icon Image. Restoring per user preference: the
  ``SHARE_ICON`` KV macro and ``icon_path('share_dark')``
  format-arg are back, with the Image positioned as a
  left-overlay (``x: self.parent.x + dp(12)``) inside the
  half-width Share button. Text "Share" stays centered; no
  ``padding: [dp(52), 0]`` needed since a single-word label
  doesn't collide with the icon.

### azt_collabd 0.30.16 + azt_collab_client 0.30.16 — Android back button pops sub-screens to settings
- ``CollabUIApp`` (the standalone server APK settings host)
  didn't bind Android's hardware back button, so a back-press
  from GitHubConnectScreen / GitLabFormScreen fell through to
  ``App.stop`` and closed the app — losing the user's settings
  session mid-setup. Picker_app already had this hook;
  CollabUIApp didn't.
- Added ``CollabUIApp.on_start`` to bind
  ``Window.on_keyboard`` and ``_on_back_button`` to consume
  key 27 by popping ``sm.current = 'settings'`` from any
  sub-screen. Settings-screen back returns False to let Kivy
  / Android close the app the normal way.

### azt_collabd 0.30.15 + azt_collab_client 0.30.15 — swap primary button to "Verify setup" after install_app
- User report: at step 2, ``install_app`` opens the browser
  with a message that says "return here and tap Verify
  setup", but the primary button still reads "Install GitHub
  App" — there's no Verify setup button to tap. The user
  comes back from GitHub, reads the instruction, can't find
  the button it names, gets stuck.
- Fix: ``install_app()`` now flips ``gh_primary_btn``'s
  ``text`` to "Verify setup" and ``_action`` to ``verify``
  right after opening the browser, so the affordance the
  message promises actually exists. If the user returns
  without installing (cancelled, navigated away),
  ``test_github_credentials`` reports
  ``app_installed=False`` and ``_test_done`` re-runs
  ``on_pre_enter`` which puts the button back to "Install
  GitHub App" — they can retry without screen drift.
- Auto-polling for install completion was considered and
  rejected for v1: poll cadence vs. GitHub-API quota is a
  real trade-off, and the swap-and-tap workflow is bounded
  by user attention anyway. Easy to add later if needed.

### azt_collabd 0.30.14 + azt_collab_client 0.30.14 — log account.login on /user/installations + per-account install matching
- 0.30.13's trace showed three azt-collaboration installs in
  ``/user/installations`` while the user said they only ever
  saw one at ``github.com/settings/installations``. We don't
  yet know who owns the other two — could be orgs the user
  belongs to, could be stale state, could be something else
  GitHub is doing. ``check_app_installed`` now logs
  ``account.login`` alongside the existing fields so the next
  probe answers "whose installs are these?" directly.
- ``check_app_installed`` gained an optional
  ``account_login`` parameter that narrows the match to the
  installation whose ``account.login`` matches (case-
  insensitive). When omitted, the legacy "first match by
  app_slug" behavior is preserved.
- ``test_github_credentials`` now passes
  ``server_username`` (the user's own GitHub login) as
  ``account_login`` so Verify setup checks for the install
  on the user's own account, not "any install we can see."
  This fixes a real bug observed: user uninstalled their
  personal install but was still a member of orgs that had
  ``azt-collaboration`` installed; the unscoped match
  reported ``installed=True`` against an org install and
  the screen continued to show "Setup complete." With this
  change, Verify setup correctly returns
  ``installed=False`` once the personal install is gone,
  and the screen regresses to step 2.
- ``diagnose_403`` (sync 403 path) does NOT yet take the
  repo's owner into account; that's the next change once we
  see the per-account data and confirm the matching strategy
  is right. Currently still uses the legacy unscoped
  ``check_app_installed``, so this release narrows just the
  Verify-setup path.

### azt_collabd 0.30.13 + azt_collab_client 0.30.13 — log raw /user/installations to diagnose stuck suspended state
- 0.30.12 still showed "Setup Complete" against a suspended
  install on the user's device, even though both server APK
  and client are 0.30.12. So the suspended-detection code is
  running but either ``inst.suspended_at`` isn't being set on
  the install we're looking at, or the slug match isn't
  hitting the entry. Need data to disambiguate.
- ``check_app_installed`` now logs:
  - All ``(app_slug, id, suspended_at)`` tuples returned by
    ``/user/installations`` so we can see whether the user's
    install is in the list at all and what GitHub is reporting
    for ``suspended_at``.
  - The matched entry (if any) with its suspended_at and
    repository_selection.
  - The final ``result`` dict.
  - HTTPError / general Exception (was silently caught).
- ``_h_test_github`` now logs the test_github_credentials
  return value (valid / app_installed / app_suspended /
  installation_id) plus what it actually wrote to the store.
- Pure tracing — no behavior change. Build, retry Verify
  setup against the suspended install, and the next logcat
  will tell us whether the suspended detection is firing or
  whether GitHub is reporting the install as not-suspended
  for some reason.

### azt_collabd 0.30.12 + azt_collab_client 0.30.12 — fix suspended-state message overwrite race
- 0.30.11 set the suspended-message ``gh_message.text``
  immediately in ``_test_done``, but ``self.on_pre_enter()``
  on the line above schedules ``_refresh_state`` for the next
  frame, and ``_render_message`` there overwrites the field
  with the step-N default ("Setup complete..." / "Now install
  the GitHub App..."). User report: Verify setup against a
  suspended install kept showing "Setup Complete, connected
  as ...".
- Same defer-past-render dance the AuthError handler uses.
  Suspended message now goes through a second
  ``Clock.schedule_once`` so it lands after ``_refresh_state``
  completes.
- Note: this fix requires the server APK to ALSO be running
  0.30.11+ (or this 0.30.12). Older daemons' ``check_app_installed``
  matches solely on ``app_slug`` and reports
  ``installed=True`` for any installation, so a suspended
  install never gets the ``app_suspended=True`` flag and the
  client never enters the suspended-message branch. If you
  see "Setup Complete" after rebuilding the recorder but not
  the server APK, that's why — rebuild + reinstall the server
  APK and Verify setup again.

### azt_collabd 0.30.11 + azt_collab_client 0.30.11 — detect suspended GitHub App installs + simpler Share/Update buttons
- **Suspended-install detection.** User repro: paused the
  azt-collaboration App installation in their GitHub settings,
  the connect screen still showed "Setup complete (App
  installed)", and sync silently failed with codes
  ``['NOTHING_TO_COMMIT', 'REPO_NOT_AUTHORIZED']`` because the
  daemon's ``check_app_installed`` only matched on
  ``app_slug`` and reported ``installed=True`` for any
  installation including suspended ones. Fix:
  ``check_app_installed`` now reads ``inst.suspended_at`` —
  ``installed=True`` requires the install to be active, and a
  new ``suspended=True`` field is set when the App is on file
  but paused. ``installation_id`` is also returned in the
  suspended case so the UI can construct the resume URL
  (``settings/installations/<id>``) instead of the generic
  install page.
- **New status code ``S.APP_SUSPENDED``** plus translation:
  "GitHub App installation is suspended. Resume it at {url}."
  ``diagnose_403`` returns this for sync 403s when the install
  is suspended; ``test_github_credentials`` exposes
  ``app_suspended`` + ``installation_id`` alongside
  ``app_installed`` so the connect screen's Verify setup path
  surfaces a precise message and link instead of regressing
  silently to the step-2 "Install" prompt.
- **Connect screen ``_test_done`` handles suspended.** When
  Verify setup runs against a suspended install, the message
  becomes "GitHub App installation is suspended. Resume it at
  github.com/settings/installations/<id>." — the user can tap
  the URL or open it manually, resume on GitHub, and re-run
  Verify setup to confirm.
- **Wire format change.**
  ``POST /v1/credentials/github/test`` response now carries
  ``app_suspended`` and ``installation_id`` alongside the
  existing ``app_installed`` field. Older clients ignore the
  extras; newer clients against older daemons see ``False`` /
  ``None`` defaults (which is the correct
  "no-suspended-state-known" reading).
- **Settings: simpler Share / Update.** Two stacked
  full-width "Share this app" / "Update this app" buttons
  collapsed into one half-width row labelled just "Share" and
  "Update". Drops the share icon (one less asset to ship and
  less visual noise; the action is obvious from the label).
  ``SHARE_ICON`` KV macro and the ``share_icon=icon_path(...)``
  format-arg removed; ``icon_path`` import dropped.

### azt_collabd 0.30.10 + azt_collab_client 0.30.10 — keep "Verify setup" available after setup completes
- Once the user reached step 4 (everything verified),
  ``_render_primary`` was hiding ``gh_primary_btn`` entirely —
  forcing them to Re-authenticate (which means re-typing the
  8-field code) just to confirm the connection is still
  healthy. User asked for a non-destructive "test settings"
  affordance from the verified state.
- Fix: ``_render_primary`` now keeps the button visible at step
  4 with the same ``Verify setup`` label and ``verify`` action
  that step 3 uses. ``test()`` is idempotent (just hits
  ``api.github.com/user`` with the saved token); a successful
  re-test stays at step 4, a failure surfaces by regressing the
  screen state to step 2 / step 1 / "Token rejected" — a single
  tap that doubles as a diagnostic.
- Step-4 message updated to "Setup complete. Connected as
  {username}. Tap Verify setup any time to re-test." so users
  notice the affordance is intentional.

### azt_collabd 0.30.9 + azt_collab_client 0.30.9 — detach publish_row children to free the GitLab button
- User report: "GitLab button still doesn't respond until 10-12
  clicks" while the adjacent GitHub button works fine. Same root
  cause as the earlier ``gh_primary_btn`` issue: ``publish_row``
  is the next BoxLayout below ``gl_action_btn`` and stays at
  ``height=0`` for users with no project. BoxLayout's
  ``_do_layout`` still positions ``publish_btn`` (a RecBtn with
  ``on_press``) at its explicit ``dp(52)`` height under the
  collapsed parent, and Kivy's dispatch loop visits every child
  regardless. The combination intermittently swallows touches
  near gl_action_btn. (Why "intermittent" rather than "always":
  the touch points hover near the bottom edge of gl_action_btn /
  spacing area, where Kivy's hit-test math is sensitive to the
  exact tap coordinate.)
- Fix: ``SettingsScreen._refresh_publish_row`` now detaches
  ``publish_row``'s children when hiding the row (via
  ``_detach_publish_children`` / ``_reattach_publish_children``,
  matching the pattern used in ``GitHubConnectScreen`` for
  ``gh_manage_box`` / ``gh_device_flow_box``). A parent with no
  children cannot dispatch on_touch_down to anything, so the
  hidden publish_btn can no longer eat taps meant for the
  GitLab button. Idempotent on both sides.
- The detach is keyed off the same condition that hides the
  row, so users with an active publishable project (the
  "should-show-publish" case) keep the row's full functionality.

### azt_collabd 0.30.8 + azt_collab_client 0.30.8 — Connect-button gating + Disconnect inside settings + web-flow plan
- **Settings GitHub/GitLab buttons gated on ``confirmed``, not
  ``connected``.** User reported the canonical footgun: install
  failed midway, gh.connected stayed True (token was saved),
  refresh() flipped the button to "Disconnect GitHub", and
  the only tap available was the one that wiped the partial
  work. ``refresh()`` now reads ``gh.confirmed`` /
  ``gl.confirmed`` and renders:
  - Not verified → ``Connect to GitHub`` / ``Connect to GitLab``
    in green; tap navigates to the connect screen which
    auto-resumes from the user's current step (server state is
    the source of truth).
  - Verified → ``GitHub Settings`` / ``GitLab Settings`` in the
    neutral surface color; tap opens the same screen, now
    showing the manage view.
- **Disconnect moved inside each screen.** GitHubConnectScreen
  already had Disconnect in its manage box;
  ``GitLabFormScreen`` gained one in this release (visible via
  ``gl_manage_box`` only when a token is on file). Rationale: a
  fat-finger Disconnect from the main settings has a real cost —
  re-auth on GitHub means re-typing the 8-field code, re-auth
  on GitLab means re-pasting a PAT. Audit doc #6 + #7 updated
  to reflect this.
- **Removed** ``SettingsScreen.gh_action`` /
  ``connect_github`` / ``disconnect_github`` /
  ``disconnect_gitlab``. The KV buttons call ``app.go(...)``
  directly; the disconnect helpers live on each respective
  screen instead.
- **Web-flow migration plan** drafted at
  ``docs/web_flow_migration_plan.md``. Research finding: GitHub
  Apps' OAuth web flow accepts PKCE but still requires
  ``client_secret`` on the token exchange (per
  github.blog/changelog 2025-07-14 + community/discussions
  #15752), so a pure-PKCE mobile-safe flow is not legal. The
  plan documents (a) a Phase-1 ``tests/probe_pkce.py`` that
  validates this finding against the live API, (b) a web-flow
  architecture using embedded ``client_secret`` in the server
  APK only, with PKCE as defense-in-depth and device flow as
  the universal fallback, and (c) the open decision points
  (embed-secret tradeoff, fork story, sunset window for device
  flow). Not yet approved for implementation.
- **PKCE probe script.** ``tests/probe_pkce.py`` (intentionally
  not auto-collected by pytest — no ``test_`` prefix). Walks
  the user through up to three browser authorizations and
  validates the four cases laid out in the plan: PKCE param
  acceptance, PKCE-no-secret rejection, PKCE-with-secret
  success, secret-only-no-PKCE success. Exits non-zero on any
  deviation, so it doubles as a regression check if GitHub
  later changes its stance.

### azt_collabd 0.30.7 + azt_collab_client 0.30.7 — revert bogus URL prefill (audit doc #1 was based on a false premise)
- Audit doc #1 assumed GitHub's OAuth Device Flow returns
  ``verification_uri_complete`` (RFC 8628 §3.2) or at least
  honors ``https://github.com/login/device?user_code=ABCD-1234``
  to prefill the code field. After actually researching this:
  - GitHub's documented response is exactly
    ``device_code, user_code, verification_uri, expires_in,
    interval`` — ``verification_uri_complete`` is OPTIONAL per
    the spec and GitHub omits it. The canonical ``cli/oauth``
    Go reference impl and ``octokit/auth-oauth-device.js``
    both parse the field defensively for spec compliance but
    receive empty strings against github.com.
  - GitHub's ``/login/device`` page silently ignores the
    ``?user_code=...`` query parameter. No prefill happens.
  - A Jan-2024 GitHub change adds a "select Continue on an
    account" confirmation step in front of the code form
    unconditionally, even for single-account users. The user
    confirmed seeing this with one account.
- Reverted 0.30.5's URL-suffix construction. The fallback chain
  is now defensive only: use ``verification_uri_complete`` if a
  future GitHub change starts returning it, otherwise the bare
  ``verification_uri``. No more constructed query strings.
- ``docs/github_connect_ux_audit.md`` #1 updated with the
  research finding and links to the canonical references so the
  next person doesn't rediscover the false premise.
- The user-visible flow against the current GitHub: the user
  taps Begin → user_code displayed in our app + auto-copied to
  clipboard → bare ``/login/device`` URL opens in browser →
  GitHub's account-confirmation step → 8-field code form (no
  paste support either) → user types each digit → authorize.
  Polling worker picks up the resulting authorization within
  ~5s. Not a great UX, but it's the only path GitHub provides.

### azt_collabd 0.30.6 + azt_collab_client 0.30.6 — worker tracing + fix message overwrite
- Detach fix from 0.30.4 worked: ``gh_primary_btn`` now receives
  touches and ``primary_action: action='begin'`` fires correctly.
- New diagnostics in ``_worker``: trace device_flow_start,
  device_flow_poll completion, save_github_tokens success,
  app_installed probe result, _done firing, and AuthError /
  Exception paths. Intent: pin down why the screen doesn't
  advance to step 2 after the user authorizes — currently we
  can't tell if polling stalled, save failed silently, or _done
  ran but credentials_status didn't reflect.
- Bug fix: error handlers (AuthError / generic Exception) called
  ``self.on_pre_enter()`` (which schedules ``_refresh_state``)
  *then* set ``gh_message.text = 'Failed: ...'``. The deferred
  ``_refresh_state`` ran on the next frame and overwrote the
  message with step-1's "Tap Begin..." default. Now the
  Failed message is set via a second ``Clock.schedule_once``
  so it lands after ``_refresh_state`` and survives. Implication
  for the user: when polling actually times out, they'll
  finally see why instead of silently bouncing to step 1.

### azt_collabd 0.30.5 + azt_collab_client 0.30.5 — build the prefilled device-flow URL ourselves
- Audit doc #1 (the "Manual code-copy step in browser" win) was
  betting on GitHub returning ``verification_uri_complete`` in the
  device-flow response. Per RFC 8628 that field is OPTIONAL and
  GitHub elects to omit it for OAuth Device Flow — confirmed
  by the user landing on the bare code-entry page after Begin.
- Fix: when ``verification_uri_complete`` isn't in the response,
  build the prefilled URL ourselves by appending
  ``?user_code=<user_code>`` to the bare URL. GitHub's
  ``/login/device`` page reads the query parameter and prefills
  the code field, so the user still lands on "Authorize?"
  directly. If a future GitHub change starts returning
  ``verification_uri_complete`` we use it as-is and skip the
  suffix.

### azt_collabd 0.30.4 + azt_collab_client 0.30.4 — detach hidden box children so they can't intercept touches
- 0.30.3's diagnostics confirmed it: when the user taps inside
  ``gh_primary_btn``'s content y-range (Window y=1013-1070
  mapped to content y=305-362, well inside the button's
  pos=275 / top=405 range), the Window touch fires but
  ``gh_primary_btn``'s ``on_touch_down`` probe never does. So a
  sibling earlier in dispatch order is silently consuming the
  touch before the Begin button gets a chance — even though
  ``Widget.on_touch_down`` should have short-circuited via
  ``self.disabled and self.collide_point(...)`` on the hidden
  ``gh_manage_box`` / ``gh_device_flow_box``.
- Suspect: ``BoxLayout._do_layout`` keeps positioning children at
  their explicit heights even when the parent's ``height=0``, so
  Re-auth NavBtn (in the hidden manage box) lives at content
  y=85-205 with disabled=True. The disabled-eats-touch contract
  is supposed to handle this, but in this layout it didn't —
  some children were intercepting touches and others weren't,
  inconsistently. The mechanism's failure mode is opaque enough
  that fighting it with more flags (``disabled``, ``opacity``,
  ``height``) just shifts which configurations break.
- Fix: hide-by-detach. ``_hide_device_flow`` / ``_hide_manage``
  now call ``remove_widget`` on each child of the box; show
  re-adds them in original order from a per-box snapshot. A
  parent with no children cannot dispatch ``on_touch_down`` to
  anything, so there's no way for a hidden manage/device-flow
  child to intercept touches that should reach Begin /
  Install GitHub App / Verify setup. The snapshot stays
  strong-ref'd while detached so widgets don't GC.
- Idempotent: re-detach is a no-op if already detached;
  re-attach is a no-op if the snapshot is empty.

### azt_collabd 0.30.3 + azt_collab_client 0.30.3 — deeper Begin button diagnostics
- 0.30.2 told us the button is at ``pos=[50, 275]
  size=[980, 130] disabled=False opacity=1.0`` — sane — but
  ``state`` never flips on tap, so touch_down isn't reaching the
  button. Sibling NavBtns (Back, Create-account) in the same
  ScrollView work fine, so this isn't a global ScrollView /
  ButtonBehavior thing.
- Added two more probes:
  - ``btn.bind(on_touch_down=...)`` on the primary button so we
    log whether the button receives the dispatched event from
    its parent BoxLayout (with ``inside=collide_point(touch)``).
  - ``Window.bind(on_touch_down=...)`` so we log every raw touch
    Kivy receives, with the touch's reported position. Lets us
    correlate "where the user actually touched" against the
    button's pos and confirm whether the touch even arrives at
    the Window level.
- Expected next-run output on a tap:
  ``WINDOW touch_down: pos=(X, Y) inside_primary_btn=True/False``
  followed (or not) by ``gh_primary_btn on_touch_down: ...
  inside=...``. The combination tells us whether the touch
  arrives at all, whether it's at the right position, and
  whether the parent dispatches it to the button.

### azt_collabd 0.30.2 + azt_collab_client 0.30.2 — Begin button diagnostics
- 0.30.1's ``on_press`` switch did not help the Begin button:
  user reports ``_refresh_state`` still fires but neither
  ``primary_action`` nor any state log appears on tap, while the
  sibling Back / Create-account buttons (also inside the same
  ScrollView) work fine. So it isn't ScrollView vs. on_release —
  it's something specific to ``gh_primary_btn``.
- Added two diagnostics that will fire on the next attempt:
  - ``_render_primary`` logs the button's resolved ``pos`` /
    ``size`` / ``disabled`` / ``opacity`` after the render
    finishes. We need this to confirm the button is at the
    coordinates the user is tapping.
  - One-shot bind on the button's ``state`` property so every
    'normal' → 'down' transition surfaces in logcat. Button's
    state machine flips on touch_down regardless of whether the
    on_press / on_release events fire — so this lets us tell
    apart the two failure modes:
      * State changes but no ``primary_action`` log → event
        dispatch broken (binding lost?).
      * State never changes → touches aren't reaching the button
        (layout / hit-test problem; e.g. a hidden sibling box
        overlaps the touch zone).

### azt_collabd 0.30.1 + azt_collab_client 0.30.1 — switch settings/connect-screen action buttons to on_press
- User-reported: Begin (device-flow start) "still does nothing"
  even after the layout / id-resolution fixes in 0.29.1. The
  ``[github-connect] _refresh_state`` trace fires on screen
  entry, confirming the button is rendered with ``_action='begin'``
  and ``disabled=False``, but no ``primary_action`` log appears
  on tap.
- Diagnosis: classic Kivy ScrollView vs. Button issue. ScrollView
  records every touch_down for scroll-distance evaluation; if the
  user's finger jiggles even ~dp(20) during the press (easy on a
  touchscreen), ScrollView grabs the touch on touch_up and the
  child Button's state machine never fires ``on_release``. The
  user's previous complaint that the GitLab settings button
  "resists pressing in most cases. if I hit it randomly for
  awhile, it does eventually activate" is the same root cause —
  the rare clean tap was the one without enough movement.
- Fix: switch every action button inside a scrolled region from
  ``on_release`` to ``on_press`` so the dispatch fires on
  touch_down, before ScrollView decides whether to claim the
  touch. Affected sites: SettingsScreen Back / Share / Update /
  Publish / GitHub action / GitLab action / Refresh / Debug-503;
  GitHubConnectScreen primary / signup / Copy / Re-authenticate /
  Disconnect / Back; GitLabFormScreen Verify / Back; the dynamic
  language-selector buttons. Trade-off: actions can no longer be
  cancelled by sliding off the button before lifting the finger —
  fine for these recoverable flows (every button either
  navigates, opens the browser, or kicks off an RPC the daemon
  treats idempotently). Popups and modals are not affected;
  they're not inside a ScrollView.

### azt_collabd 0.30.0 + azt_collab_client 0.30.0 — boot Python on ContentProvider lazy-spawn; auto-exit on self-update
- **Server APK couldn't be reached after fresh install unless the
  user opened it manually.** ``AZTCollabProvider.onCreate()`` was a
  one-line ``return true`` — Android's ContentProvider lazy-spawn
  brought up the ``:provider`` host process and instantiated the
  Provider, but did not start any Service in that process, so
  Python never booted, ``install_callbacks()`` never ran, and the
  ``sDispatch`` slot stayed null. Every ``call()`` then fell
  through to the existing ``daemon_not_ready`` 503 fallback. The
  user's repro: install server APK → open recorder → recorder
  can't reach daemon → user opens server APK launcher → daemon
  finally boots → recorder works on the next attempt.
  ``onCreate()`` now calls ``AZTServiceProviderhost.start(ctx,
  "")`` so PythonService is started alongside the Provider on the
  same lazy-spawn. The boot is async (Python service thread
  spawns separately); the very first peer call may still race
  the boot and surface ``daemon_not_ready``, but ``rpc.call``'s
  existing transport-level retry sees the populated callbacks on
  the second attempt — the user no longer has to babysit the
  install.
- **Auto-exit on self-update.**
  ``azt_collabd.android_cp.service`` now snapshots the package's
  ``PackageManager.lastUpdateTime`` at ``install_callbacks()``
  time and re-reads it after every dispatch. If it has advanced,
  we schedule an ``os._exit(0)`` 500 ms later, so the in-flight
  binder reply has time to land and the next peer
  ContentResolver call lazy-spawns the freshly-installed code.
  Belt-and-braces: Android's package installer normally kills
  the upgraded process for us, but custom-ROM battery savers and
  ``adb pm install -r`` can leave the old daemon running with
  stale code while the new APK is on disk — that produced the
  "after updating, peer connects to old daemon until I
  force-stop the server APK" symptom. Adds one PackageManager
  call per dispatch (cheap; cached by Android in the same
  process).
- Lock-step minor bump because the Provider Java change requires
  a full server-APK rebuild — peers that update without the
  matching server-APK rebuild will still hit the daemon-boot
  race on lazy-spawn and have to open the server APK manually.

### azt_collabd 0.29.2 + azt_collab_client 0.29.2 — fix server-APK crash on KV format
- ``register_kv`` was raising ``KeyError: 'uri'`` from
  ``KV_TEMPLATE.format(font_name=..., share_icon=...)`` because a
  comment I added in 0.29.1 quoted ``"Opening {uri}\n..."`` as
  prose inside the KV string. Python's ``str.format`` reads
  ``{uri}`` as a substitution placeholder regardless of whether
  it sits inside a KV comment. The peer's "Could not open
  project picker: unexpected_cancel" + ``KeyError: 'uri'`` in
  ``register_kv`` traceback is the symptom — the server APK
  crashes during start-up, the picker activity returns
  RESULT_CANCELED with data, peer treats it as the picker
  anomaly retry path and gives up.
- Comment now spells the placeholder as "URL" without braces; the
  same risk applies to any ``{name}``-shaped token in KV
  comments, which is documented in the comment for the next
  editor.

### azt_collabd 0.29.1 + azt_collab_client 0.29.1 — fix unresponsive Begin / GitLab buttons after 0.29.0 restructure
- **GitHubConnectScreen "Begin" did nothing.** Two compounding
  causes, both shipped fixes:
  1. ``primary_action`` was re-fetching credentials_status to pick
     a step every tap. If the freshly-rendered button label said
     "Begin" but the daemon's status said the user was already at
     step 4 (stale from a prior session), the dispatcher fell
     through every elif and silently no-op'd. ``_render_primary``
     now stamps an ``_action`` attribute (``begin`` / ``install`` /
     ``verify``) on the button each time it renders; the
     dispatcher uses that, with a label-based fallback, and
     finally defaults to ``begin()`` so a button labelled "Begin"
     always begins.
  2. ``on_pre_enter`` accessed ``self.ids.gh_user_code`` and
     similar directly. On Kivy ≥ 2.3 the rule's nested children
     can lag a frame after the screen is added, so the early
     accesses raised an ``ObservableDict`` AttributeError mid-
     setup, leaving the screen in KV-default state with no
     ``_action`` tagged. ``on_pre_enter`` now defers via
     ``Clock.schedule_once`` (matches what ``SettingsScreen`` was
     already doing); every helper uses ``self.ids.get(...)`` so a
     genuinely-missing widget no longer takes the whole pass
     down.
- **GitLab/GitHub action buttons "resisted pressing"** in the
  settings screen: tapping the sibling RecBtn fired
  ``contributor_input.on_focus``, which called
  ``save_contributor()`` synchronously — the RPC blocked the UI
  thread for a few hundred ms during which Kivy still received
  the touch but couldn't dispatch the on_release until the RPC
  returned. ``save_contributor`` now runs the ``set_contributor``
  call on a worker thread; the "Saved." flash flips through
  ``Clock.schedule_once`` on the UI thread.
- **Layout-shift fix.** The new ``gh_preflight`` and ``gh_message``
  ``BodyLabel`` widgets used the
  ``height: self.texture_size[1] + dp(8)`` growing pattern; on first
  paint the texture is computed against width=0, so the label
  starts ~30 dp tall and grows as the layout settles, pushing
  every button below it down. Replaced with explicit
  ``height: dp(80)`` so the BoxLayout's ``minimum_height`` is
  stable from frame 0 — no more "tap where the button used to
  be" misses.
- **Tracing.** Added ``[github-connect]`` print lines on
  ``primary_action`` / ``begin`` / ``_refresh_state`` so a flaky
  field repro can be diagnosed from logcat without rebuilding
  with extra logging. Cheap.

### azt_collabd 0.29.0 + azt_collab_client 0.29.0 — GitHub connect-flow UX restructure (audit doc #1–#7)
- **#1 ``verification_uri_complete``**:
  ``GitHubConnectScreen._worker`` now prefers GitHub's
  ``verification_uri_complete`` (URL with the user_code prefilled)
  over the bare ``verification_uri``. Users land on GitHub's
  Authorize? page directly instead of the code-entry detour.
  Falls back to ``verification_uri`` then the bare URL if
  GitHub's response shape ever changes.
- **#2 + #3 step-indicator + pre-flight + no auto-fire**: the
  GitHub connect screen is now organised as three explicit
  stages — *1. Authorize this device* → *2. Install GitHub App*
  → *3. Verify setup* — rendered as a colour/bold-coded
  indicator. A single state-aware "primary" button presents
  only the next required action; ``on_pre_enter`` derives the
  current step from server flags
  (``connected`` / ``app_installed`` / ``confirmed``), so a
  partial setup that picks back up later resumes from where it
  stopped (lost network, browser bail-out, app close all
  recoverable). Pre-flight body text explains what GitHub is and
  that a free account is required; the device flow is never
  auto-fired — the user always taps *Begin* / *Install GitHub
  App* / *Verify setup* explicitly. ``_render_message`` /
  ``_render_steps`` / ``_render_primary`` / ``_render_manage``
  handle the four screen shapes.
- **#4 "Verify setup" rename**: both GitHub and GitLab
  "Test connection" buttons are relabelled "Verify setup" — the
  old label sounded like an optional diagnostic but is actually
  the gate that flips ``confirmed=True``. Status messages
  referencing "Test connection" are updated to match.
- **#5 create-account link**: a "Create a GitHub account
  (free)" NavBtn just below the pre-flight panel opens
  ``https://github.com/signup`` in the user's browser. Pre-flight
  body text also names the account-required precondition so the
  user isn't surprised by GitHub's sign-in/up page.
- **#6 simplified host buttons**: ``SettingsScreen`` now shows a
  single state-aware GitHub button (label flips Connect ↔
  Disconnect from ``credentials_status``) instead of two
  parallel buttons, and a single ``GitLab`` button that opens
  the GitLab settings form. Connection details for both hosts
  remain in the Status block below.
- **#7 declined**: no "Are you sure?" disconnect popup —
  per-maintainer preference; with #1 landed an accidental
  Disconnect costs one tap to redo, and the GitHub App on the
  GitHub account is untouched, so re-Authorize is the only step
  needed. Audit doc records the rationale.
- French .po updated with the new strings; old "Test
  connection" / "Tap Test connection" entries left in place
  (translation-coverage drift detector only flags missing
  msgids, not orphans).
- Lock-step minor bump because the connect-flow restructure
  changes a daemon-side UI subprocess that peers spawn through
  ``open_server_ui()`` — version-string display in the settings
  footer flags the cut.

### azt_collabd 0.28.27 + azt_collab_client 0.28.27 — 60s warmup budget + "Try again" affordance
- ``_DAEMON_WARMUP_RETRIES`` raised again, 15 → 30 (30s → 60s).
  User-reported: 30s wasn't enough on their device — "next boot
  fails, the following one succeeds" — confirming Java-side cold
  spawn time can exceed 30s after ``pm clear`` or fresh install.
- New ``on_retry`` parameter on
  ``install_server_apk_popup``; when supplied, adds a "Try again"
  button to the popup. Bootstrap's
  ``_prompt_server_unresponsive`` now passes
  ``on_retry=_post_install_continuation`` so users on truly slow
  hardware can keep waiting past the 60s budget without having
  to download fresh (Install) or close the app (Quit). Tap →
  popup dismisses → 2s warm-up wait → fresh compat probe.
- Side effect: layouts the popup with up to 4 buttons in the
  action row (Quit | Try again | Open install page | Install).
  Each is text-wrap-bound so labels fit even on narrow screens.

### azt_collabd 0.28.26 + azt_collab_client 0.28.26 — SHA-256 reuse check, drop the time window
- Replaced 0.28.25's mtime-window heuristic with definitive
  SHA-256 verification. After a successful download, ``update.py``
  writes the file's SHA-256 to a sidecar
  ``<asset>.sha256``. ``_has_fresh_download(path)`` now reuses
  the staged file iff (a) the file exists, (b) the sidecar
  exists, (c) recomputing the file's SHA matches the sidecar.
- Eliminates the "10 minutes might not be enough for everyone"
  concern — reuse works regardless of how long the user spent in
  the "Install unknown apps" Settings detour, and regardless of
  device speed.
- Cost: SHA-256 of a typical APK takes ~1–3 seconds on phone
  hardware. Negligible compared to the 10–30 seconds of
  re-download it replaces, especially on slow connections.
- Side benefit: catches APK corruption between download and
  install. If the file gets damaged somehow (rare, but possible
  on flaky storage), the sidecar mismatch forces a re-download
  rather than dispatching a corrupted install Intent.
- ``_save_download_sha(path)`` is non-fatal on failure: a
  missing sidecar just means the next reuse check returns False
  and we redownload, same as before this change.

### azt_collabd 0.28.25 + azt_collab_client 0.28.25 — reuse a recent download instead of re-fetching
- User reported the install flow was downloading the APK twice
  when Android required "Install unknown apps" permission: first
  download → permission detour → user grants → re-tap Install →
  re-download (10–30s wasted).
- New ``_has_fresh_download(path)`` helper checks whether the
  staged file at ``$AZT_HOME/updates/<asset>`` was last modified
  within ``_REUSE_DOWNLOAD_AGE_S`` (10 minutes). Both
  ``install_apk_from_url`` and ``check_for_update``'s download
  paths now skip the download when the file is fresh enough,
  surfacing ``Using already-downloaded file…`` status in place
  of the percentage progress.
- 10-minute window is conservative: long enough to cover the
  typical "user popped to Settings and came back" duration even
  on slow devices; short enough that a stale APK from a
  previous session (yesterday's launch, etc.) won't be
  installed when there's a newer release available.
- ``_download`` writes to ``<path>.part`` and only renames on
  success, so a present ``<path>`` is always a complete
  download — the freshness check doesn't need to validate
  partial-file recovery.

### azt_collabd 0.28.24 + azt_collab_client 0.28.24 — extend daemon-warm-up budget for cold starts
- ``_DAEMON_WARMUP_RETRIES`` raised from 5 to 15 (10s → 30s
  budget). The 503 ``daemon_not_ready`` response that fires
  during cold starts comes from ``AZTCollabProvider.java``'s
  ``sDispatch == null`` check, NOT from my Python sentinel
  hook — it's the genuine "Python interpreter not yet loaded"
  state. After ``pm clear`` or a fresh install the cold start
  can run 15–25 seconds because the dex cache needs rebuilding,
  and the previous 10s budget was tripping users into the
  "AZT Collaboration not responding" popup unnecessarily.
- Warm-cache normal launches still exit the retry loop on the
  first compat-probe success (1–3s typical), so the longer
  budget isn't user-visible in steady state. Cold-start users
  see up to 30s of "Connecting to AZT Collaboration service…"
  popup before falling through.
- Diagnostic clarification noted in the comment block: the 503
  has two possible sources (the test sentinel + the Java
  provider's startup gate); when troubleshooting, check that
  the daemon process is actually alive
  (``adb shell ps -A | grep aztcollab``) — if not, it's the
  Java side and the only fix is waiting longer or warming up
  the dex cache.

### azt_collabd 0.28.23 + azt_collab_client 0.28.23 — visible "Connecting…" popup during retries
- 0.28.19 added the daemon-warm-up retry loop, but the
  ``Connecting to AZT Collaboration service…`` status only flowed
  to ``ctx.on_status`` (host's status sink, often invisible).
  Result: 10s of empty-state peer UI with no feedback while the
  retries ran.
- New modal ``_show_connecting_popup`` opens on the first retry
  with a centred "Connecting to AZT Collaboration service…"
  message. ``auto_dismiss=False`` so the user can't tap past it.
  Dismissed on every terminal branch — ``compat ok``,
  ``server_too_old``, ``client_too_old``, retries-exhausted, or
  raised exception — before the next branch's UI fires (so the
  unresponsive popup doesn't stack on top of the connecting one).
- Mutable ``connecting_popup`` slot added to ``_Ctx`` so the
  show / dismiss helpers can find each other across the worker-
  thread boundary without a module-level dict. Idempotent: if a
  popup is already up, ``_show_connecting_popup`` is a no-op.

### azt_collabd 0.28.22 + azt_collab_client 0.28.22 — toggle the debug-503 sentinel from settings UI
- 0.28.21's sentinel could only be created via
  ``adb shell run-as``, which fails on release-signed APKs
  (``run-as`` requires the package to be debuggable).
- New "Debug (testing)" section in the daemon settings UI
  (``SettingsScreen``) with a toggle button that creates / removes
  ``$AZT_HOME/_debug_force_503``. State indicator below shows
  whether ``/v1/health`` is currently forced to 503 or responding
  normally. Always visible — production users tapping it just see
  "service unavailable" until they tap again.
- Test workflow: tap the server APK launcher icon (it's an
  installed app with its own icon) → settings UI opens directly
  (bypasses bootstrap; settings calls don't go through
  ``/v1/health``) → toggle Debug → close → launch peer →
  bootstrap retries 5×2s → unresponsive popup fires. Re-open
  server APK to toggle off when done.

### azt_collabd 0.28.21 + azt_collab_client 0.28.21 — debug hook to test the "not responding" popup
- Daemon's ``/v1/health`` (the compat handshake endpoint) now
  returns ``503 daemon_not_ready`` when
  ``$AZT_HOME/_debug_force_503`` exists. Toggle without restarting
  the daemon — the file presence is checked per-request. Create
  via ``adb shell run-as org.atoznback.aztcollab touch
  files/azt/_debug_force_503``; remove with the equivalent ``rm``.
- Lets the bootstrap workflow's daemon-warm-up retry path
  (``_DAEMON_WARMUP_RETRIES = 5`` × 2s = 10s) exhaust deterministically,
  exercising the "AZT Collaboration not responding" popup added
  in 0.28.20. Without this, manually triggering the unresponsive
  state required either killing the daemon mid-spawn (race-prone)
  or breaking the install (signature mismatch — heavy).

### azt_collabd 0.28.20 + azt_collab_client 0.28.20 — modal recovery popup when daemon stays unresponsive
- 0.28.19's retry-with-backoff fixed the common case (daemon
  warming up settles within 1–3s) but still fell through to
  ``_check_self`` → ``on_done`` after the retry budget exhausted.
  Result: the same bouncing-out behaviour the user originally
  reported, just delayed by 10s. ``on_done`` fired, peer's
  startup tried daemon RPCs, hit ``ServerUnavailable``, picker
  fired and failed, picker emitted CANCEL, peer closed via the
  picker-cancel rule.
- **Real fix**: when the warm-up retries exhaust without daemon
  response, bootstrap now shows a modal popup
  (``_prompt_server_unresponsive``) — same canonical
  ``install_server_apk_popup`` as the missing-server case, with
  a body reading "AZT Collaboration is installed but did not
  respond. It may still be starting up; wait a moment, then tap
  Install to reinstall it, or Quit to close this app and try
  again later." Title: "AZT Collaboration not responding".
- The popup gives the user three explicit recovery options
  (Reinstall, Open install page, Quit) instead of silently
  bouncing them out of the app. Modal blocking means the peer
  stays in the foreground until the user makes a choice, and
  ``on_done`` is not fired (so the peer doesn't run its
  post-bootstrap startup against a daemon that isn't there).
- Reinstall path: standard download+install via
  ``install_apk_from_url``; on completion the post-install
  continuation re-runs ``_check_server`` and on_done fires from
  the healthy path. Quit path: closes the peer cleanly so the
  next launch starts fresh.

### azt_collabd 0.28.19 + azt_collab_client 0.28.19 — retry on daemon-warm-up race at startup
- **Symptom user reported:** opening peer A, log shows
  ``[bootstrap] AZT Collaboration installed but unreachable.
  Continuing offline.`` followed by ``[recent] last_project:
  ServerUnavailable: provider HTTP 503: daemon_not_ready``, then
  Android brings the previously-foregrounded peer B to the
  front. Sequence: bootstrap fires ``on_done`` thinking everything
  is fine because the server APK is installed; peer's normal
  startup tries ``last_project()`` while the daemon is still
  warming up; the daemon's ContentProvider returns 503; peer's
  picker logic kicks in, fails with the same 503; picker emits
  ``RESULT_CANCELED``; the picker-cancel rule from
  ``CLIENT_INTEGRATION.md`` § 5 closes the peer; Android brings
  the most-recent task forward.
- **Fix:** ``_check_server`` now retries the compat probe with
  backoff (``_DAEMON_WARMUP_RETRIES=5``,
  ``_DAEMON_WARMUP_INTERVAL_S=2.0`` → 10s budget total) when the
  server APK is installed but unreachable. Status flips to
  ``Connecting to AZT Collaboration service…`` during retries.
  Android lazy-spawns the server APK's Python interpreter on the
  first ContentResolver call; this typically settles within
  1–3 seconds, well under the budget.
- If the retries exhaust, we still fall through to
  ``Continuing offline.`` and ``_check_self`` (which fires
  ``on_done``). At that point the daemon is genuinely unreachable
  (crashed, hardware glitch, signature mismatch denied us access)
  and bootstrap can't fix it; the peer's host code is responsible
  for handling ``ServerUnavailable`` on its post-on_done RPCs.
  Recommend defensive try/except around the first 1–2 RPCs the
  host makes after ``on_done`` — already in
  ``CLIENT_INTEGRATION.md`` § 4 ("log the failure and continue,
  not pop their own dialog").

### azt_collabd 0.28.18 + azt_collab_client 0.28.18 — move CLIENT_INTEGRATION.md into the symlinked package
- ``CLIENT_INTEGRATION.md`` moved from ``docs/`` (canonical-repo
  only) to ``azt_collab_client/`` (symlinked into every peer).
  Peers now see the integration contract through their existing
  symlink without needing a separate ``azt-collab/`` checkout.
  Old ``docs/CLIENT_INTEGRATION.md`` is reduced to a one-line
  redirect for anyone with the old path bookmarked.
- Added missing section on ``on_done`` semantics (introduced in
  0.28.5 but the doc never reflected it). Renumbered subsections
  to fix a duplicate ``## 6`` heading. Added the
  ``install_apk_from_url`` entry to the "what the suite does for
  you" list (added in 0.28.10, doc never updated).
- ``azt_collab_client/CLAUDE.md`` updated to point at the new
  in-package location.

### azt_collabd 0.28.17 + azt_collab_client 0.28.17 — peer self-update gets the same progress UI as server install
- **Peer self-update now uses the same popup as the server case**
  with progress visible in the body. Previously
  ``_prompt_self_update`` showed a Yes/No popup that dismissed on
  Update tap, then ran ``install_apk_from_url`` "in the background"
  with status flowing only to the host's ``on_status`` sink — which
  meant the user saw nothing until the install finished. Now
  bootstrap calls ``install_server_apk_popup`` (parameterized for
  the peer's own APK) so the same body-label progress (downloading
  %, retrying status, "Installing…", "Installed.") is on screen
  through the entire flow. Closes the user-reported "looks like
  it's stuck — Update just means OK" symptom.
- **``install_server_apk_popup`` is now a generic install/update
  popup**. New parameters:
  - ``direct_url`` — overrides the composed download URL.
  - ``asset_filename`` — overrides the on-disk staging name +
    MediaStore display name.
  - ``open_page_url`` — overrides the "Open install page" target
    so self-update points at the peer's release page instead of
    the server's.
  - ``dismiss_label`` — overrides the dismiss button label.
  - ``dismiss_action`` — ``'quit'`` (default; closes app) for the
    server case; ``'dismiss'`` for self-update where declining
    means "stick with current version, peer keeps running".
  - ``install_target_package=''`` — explicit "no polling" sentinel
    for self-update where the install kills the running peer
    process.
- **Self-update decline records the version only on Not-now tap.**
  Previously the decline was recorded synchronously in
  ``_prompt_self_update``'s decline handler. The new popup-based
  flow records on dismiss, but only when no install was started
  — if the user tapped Update and the install kicked off, no
  decline gets recorded (the next launch will detect the new
  version naturally instead).
- ``_yes_no`` helper removed; no callers left after the refactor.
- Lock-step bump 0.28.16 → 0.28.17 with ``MIN_SERVER_VERSION``
  raised to match (continues the user's "test the server-update
  path" workflow — every iteration of the bump fires the
  too-old-server prompt for a peer that bundles the new client).

### azt_collabd 0.28.16 + azt_collab_client 0.28.16 — bump MIN_SERVER_VERSION to test server-update path
- Lock-step debug bump 0.28.15 → 0.28.16 across both packages.
- ``azt_collab_client.MIN_SERVER_VERSION`` raised 0.27.0 → 0.28.16.
  Forces a rebuilt peer (which bundles client 0.28.16) to refuse
  any server APK older than 0.28.16. Test path: install peer with
  bundled 0.28.16, leave the older server APK (0.28.15 or earlier)
  on the device, launch peer → ``check_server_compat`` returns
  ``server_too_old`` → bootstrap fires
  ``_prompt_server_update`` → install popup shows the
  "Update AZT Collaboration?" body with the "Update" button label
  and pre-filled current_server_version (so the daemon doesn't
  redownload an identical release if there isn't actually a
  newer one published).
- ``MIN_CLIENT_VERSION`` (in azt_collabd) stays at 0.27.0 — this
  bump is for testing the server-too-old path, not client-too-old.

### azt_collabd 0.28.15 + azt_collab_client 0.28.15 — fix Install button stuck after "unknown apps" detour
- **"Tap Update again" message corrected** to use the actual
  install-button label. The popup's button is "Install" (or
  whatever ``install_label`` the caller passed), not "Update", so
  the previous message ("…then tap Update again") was wrong for
  every caller except the settings-screen Update buttons. Now uses
  ``{label}`` substitution and the popup passes its own button
  text.
- **New ``on_user_action_needed`` callback** in both
  ``install_apk_from_url`` and ``check_for_update``. Fires when
  the install path stalls because Android needs the user to flip
  "Install unknown apps" for this peer in Settings. Without this,
  the popup's Install button stayed disabled forever after we
  routed the user to settings — only Quit was active. The popup
  now wires this callback to re-enable Install + Open install
  page so the user can come back from Settings and retry.
- ``install_label`` parameter added to both functions so callers
  can override the label used in the "tap {label} again"
  message. Defaults to "Install" for ``install_apk_from_url``,
  "Update" for ``check_for_update`` (matching their
  conventional UX context).

### azt_collabd 0.28.14 + azt_collab_client 0.28.14 — fix language-toggle inertness + URL overflow in error
- **Language toggle in `install_server_apk_popup` now actually
  switches.** The handler called ``popup.dismiss()`` then
  ``install_server_apk_popup(...)`` synchronously from inside a
  Button.on_release — Kivy silently no-ops that re-entrance in
  some versions because the original popup is mid-dismiss. Fix:
  defer dismiss + relaunch via ``Clock.schedule_once(..., 0)`` so
  the touch handler returns first. Also added stderr logging at
  every step (``[install_popup] language switch: fr``,
  ``[install_popup] dismiss raised:`` …) so any future failure is
  diagnosable via ``adb logcat``.
- **Long URLs in error messages now wrap inside the popup.**
  ``_download``'s 404-with-URL surface for a 60-character GitHub
  asset URL was running off the body label because Kivy Labels
  only break at whitespace and URLs have none. New
  ``_wrappable_url(url)`` helper in ``update.py`` inserts a real
  ``\n`` after each ``/`` (only when the URL is over 50 chars) so
  the URL renders across multiple lines inside the popup body.
  Display is uglier but legible — readable URLs trump pretty
  ones for diagnosis.

### azt_collabd 0.28.13 + azt_collab_client 0.28.13 — diagnostic logging + browser-like headers in download
- ``_download`` now logs to stderr (visible in ``adb logcat``) at
  every meaningful step: the URL it's about to GET, the redirect
  target if any, the HTTP status, and the URL the server actually
  served the 404 from (``HTTPError.url``). Disambiguates
  "github.com returned 404" from "github.com 302'd to the CDN, CDN
  returned 404" — different diagnoses (asset truly missing vs.
  bot-pattern rejection on the CDN edge or expired-token edge
  case).
- Added ``Accept: */*`` and updated the User-Agent string to
  ``'azt-collab-updater/1 (+curl-compat)'``. Some GitHub CDN edges
  return 404 to bare-pattern UAs; mimicking curl removes that
  variable.
- Diagnostic-only round; the underlying 404 puzzle (browser works,
  ``gh release view`` confirms the asset, but Python urllib gets
  404 three times) is still being investigated. The new logging
  should make the next reproduction definitively diagnosable.

### azt_collabd 0.28.12 + azt_collab_client 0.28.12 — popup polish: language toggle, version footer, URL in error
- **Discrete language toggle** at the top of
  ``install_server_apk_popup``. First-install users whose device
  locale is French (or any non-English) had no way to switch
  language since the popup blocks the settings UI; now there's a
  small row of language buttons (current bolded, others tappable)
  that dismisses + re-opens the popup with the chosen language.
  Only shown when ``i18n.available_languages()`` returns more than
  one — desktop hosts running an English-only build won't see it.
- **Version footer** at the bottom of the popup
  (``client X.Y.Z``). Subtle / dim, mirrors the version strip
  pattern from the daemon settings UI. Helps diagnose which
  client build is actually live when reproducing UI bugs across
  versions.
- **Download error includes the URL we tried.** When the asset
  download fails (404 from the well-known direct URL, or any
  other transport error), the surfaced message now appends the
  URL on a new line. Lets the user eyeball the URL against what
  their browser successfully fetches — most 404s on this path
  come from an asset-name mismatch on the GitHub release, not a
  transport bug.

### azt_collabd 0.28.11 + azt_collab_client 0.28.11 — popup button wrapping, URL composition cleanup
- **Install popup button text wraps now.** Previously "Open install
  page" and "Quit AZT Recorder" / "Quit AZT Viewer" got clipped on
  narrower screens because Kivy Buttons don't wrap by default.
  ``text_size`` is now bound to button size on all three buttons
  (``halign='center'`` + ``valign='middle'``), and the button row
  height bumped to dp(60) to allow two-line wraps. Popup overall
  height also bumped from dp(280) to dp(300) to compensate.
- **Direct-URL composition consolidated** into the install popup's
  ``_do_install``. The hardcoded ``_DIRECT_DOWNLOAD_URL`` constant
  in ``update.py`` is gone; the popup now composes
  ``f'https://github.com/{_SERVER_REPO_DEFAULT}/releases/latest/download/{_SERVER_ASSET_DEFAULT}'``
  from the same constants the package-presence probe uses
  (``bootstrap._SERVER_REPO_DEFAULT``,
  ``bootstrap._SERVER_ASSET_DEFAULT``). Single source of truth: a
  fork that wants to point its server-install at a different
  release feed only edits the bootstrap constants.

### azt_collabd 0.28.10 + azt_collab_client 0.28.10 — direct-URL install for popup + peer self-update
- **New ``install_apk_from_url(url, asset_filename, ...)``** in
  ``azt_collab_client.ui.update``. Direct-URL alternative to
  ``check_for_update``: GETs the URL, streams to disk, dispatches
  Android's installer, optionally polls for completion via change-
  detection. No GitHub API call, no JSON parsing, no asset name
  matching, no listing-endpoint quirks. For when the caller has a
  stable redirect URL like
  ``releases/latest/download/<asset>`` and doesn't need version
  comparison.
- **`install_server_apk_popup`** now uses ``install_apk_from_url``
  for the Install button. Closes the user-reported "install
  comes back 404" symptom — the API path's ``_pick_asset`` step
  was failing on edge cases (asset-name mismatch, listing
  endpoint quirks). Direct URL bypasses the entire failure
  surface.
- **`bootstrap._do_self_install`** also migrated. Composes
  ``f'https://github.com/{peer_repo}/releases/latest/download/{peer_asset_filename}'``
  from the args bootstrap already takes. Version comparison
  still happens earlier in ``_peer_update_with_confirm`` (which
  needs the API for the small tag-name lookup); by the time we
  reach the install action, the user has confirmed the prompt
  and we just need to install whatever's at the URL. No
  ``install_target_package`` because self-install replaces the
  running peer process.
- **`_start_install_poll`** now supports two modes — pinned-
  version (used by ``check_for_update`` when it knows what
  version it just downloaded) and change-detection (used by
  ``install_apk_from_url`` which doesn't have version metadata).
  Change-detection snapshots the current installed versionName at
  start, then polls for any difference; trivially handles the
  uninstalled→installed case.
- **`_download`** now reads ``Content-Length`` from the response
  headers when the caller doesn't pre-supply ``total_bytes``, so
  progress percentages work for the direct-URL path too.
- **Settings-screen "Update this app" buttons** (CollabUIApp +
  PickerApp) stay on ``check_for_update`` because they want the
  "Up to date" message when the user taps without a newer version
  available — that's the value of the API path there.

### azt_collabd 0.28.9 + azt_collab_client 0.28.9 — restore "Open install page" semantics
- ``SERVER_APK_INSTALL_URL`` reverts to the **release page** URL
  (``https://github.com/kent-rasmussen/azt-collab/releases/latest``),
  not the direct-download asset URL. The popup's "Open install
  page" button is for users who want to read release notes or
  browse the project before installing — the page is what serves
  that purpose. The "Install" button in the same popup is the
  one-tap-to-install path; it discovers the asset URL via the
  GitHub API at runtime (asset['browser_download_url']) rather
  than from this constant.
- Effectively reverts the 0.28.3 URL-direction change. Sole
  consumer of ``SERVER_APK_INSTALL_URL`` is
  ``install_server_apk_popup._open_page``; everything else (the
  bootstrap workflow, ``check_for_update``) computes the
  direct-download URL itself.

### azt_collabd 0.28.8 + azt_collab_client 0.28.8 — fix SSL on Android urlopen
- p4a doesn't ship system CA certs into the Android Python
  runtime, so the new client-side ``urllib.request.urlopen`` calls
  in ``azt_collab_client.ui.update`` (release listing, asset
  download) fail with "unable to get local issuer certificate".
  The daemon side has had ``azt_collabd/net.py:_ensure_ssl()`` for
  this since forever, but the client can't import it (Hard rule 3:
  no daemon import; the two run in different processes on Android
  anyway).
- New ``azt_collab_client/net.py`` mirrors the daemon's SSL patch,
  slimmed for the client (no urllib3.PoolManager surface — the
  client doesn't speak dulwich). ``_ensure_ssl()`` is idempotent
  and called at the top of every urlopen site in ``update.py``.
  Finds certifi's bundle (preferred), falls back to extracting it
  from the bundled zip into ``$ANDROID_PRIVATE/cacert.pem``, then
  to common system locations, then to a verification-disabled
  context as a last resort.
- Symptom this fixes: post-popup-open SSL error on the bootstrap
  install flow's GitHub release probe — the "is a newer release
  available" call (``_fetch_latest``) and the asset-binary stream
  (``_download``) both bypassed SSL setup before this fix.

### azt_collabd 0.28.7 + azt_collab_client 0.28.7 — fix ModuleNotFoundError on popup open
- One-character relative-import bug introduced in the 0.28.4 popup
  refactor: ``azt_collab_client/ui/popups.py:369`` had
  ``from ..bootstrap import …`` (resolves to
  ``azt_collab_client.bootstrap`` — doesn't exist) instead of
  ``from .bootstrap import …`` (the correct
  ``azt_collab_client.ui.bootstrap``). The error fired the moment
  ``install_server_apk_popup`` was opened on a peer with no server
  installed, which raised inside Kivy's main loop and took the
  peer down — visible as "presplash, brief app screen, close".
  The user-reported symptom from 0.28.4 onward; the 0.28.5 / 0.28.6
  bootstrap fixes never had a chance to run because the popup
  itself couldn't load.

### azt_collabd 0.28.6 + azt_collab_client 0.28.6 — auto-resume after server install
- **Post-install continuation.** Once the install-completion poll
  watchdog confirms the new server APK is live, the popup auto-
  dismisses (after a 1-second visual confirmation showing
  "Installed.") and bootstrap re-enters its compat check. Daemon
  is now reachable, so the healthy path takes over and on_done
  fires, letting the host continue normal startup. No manual
  Quit + relaunch needed.
- New ``check_for_update(on_install_complete=...)`` parameter,
  threaded through ``_start_install_poll``. Fires only on
  confirmed completion (versionName flipped), not on the
  watchdog timeout — that branch still leaves the user with the
  "Install pending" message and the popup up.
- New ``install_server_apk_popup(on_install_complete=...)``
  parameter wires the upstream callback to (a) dismiss the popup
  after a 1s delay and (b) call the host's continuation. Bootstrap
  passes a continuation that schedules a 2-second daemon-warm-up
  pause (Android lazy-spawns the ContentProvider host on first
  call) before re-running ``_check_server``.
- If Android kills the peer process during install (memory
  pressure, system installer dominating), the popup + its
  continuation chain are gone too. Re-launch triggers a fresh
  bootstrap, which finds the daemon reachable and flows through
  the healthy path — same outcome, just one extra user action.

### azt_collabd 0.28.5 + azt_collab_client 0.28.5 — fix flash-then-die regression in 0.28.4
- **Bootstrap no longer fires on_done before the no-server popup
  opens.** In 0.28.4 the prompt branches called
  ``_on_done_and_release(ctx)`` immediately, then opened the popup
  on the next UI tick. Hosts whose ``on_done`` is wired to
  "continue normal startup" then attempted RPCs against a daemon
  that wasn't there yet, the failure cascaded into App.stop() (or
  similar in the host's error handling), and the popup that was
  about to open was killed alongside it — visible as a screen flash
  then peer shutdown.
- New ``_release_running()`` helper splits the guard release from
  the on_done notification. ``_on_done_and_release`` keeps both
  for the healthy terminal paths (server compat OK, no self-
  update needed). The two no-server branches —
  ``_prompt_server_install`` and ``_prompt_server_update`` —
  release the guard but don't fire on_done, so the host stays
  parked at whatever screen was up when ``bootstrap()`` was
  scheduled (typically a splash). Once the user installs the
  server APK and the peer relaunches, bootstrap re-fires from a
  fresh process, finds the daemon reachable, and on_done flows
  through ``_check_self`` along the healthy path.
- The already-declined-this-version branch in
  ``_prompt_server_update`` does still fire on_done (with the
  caveat that the host's first RPC will surface the daemon's
  compat error). The user explicitly chose this state by
  declining earlier; the host should handle it gracefully.

### azt_collabd 0.28.4 + azt_collab_client 0.28.4 — single canonical "no server" popup, modal blocking, Quit button, doc consolidation
- **Single popup for "no server" / "server too old" cases.** Bootstrap's
  `_prompt_server_install` and `_prompt_server_update` now both
  delegate to `install_server_apk_popup` (instead of the older
  generic Yes/No `_yes_no` helper). Result: one visual surface,
  one set of buttons, one progress sink, one decline path. Closes
  the user-reported bug where two popups stacked on first launch
  ("Could not open project picker: server_apk_not_installed" + the
  bootstrap Yes/No, OR the bootstrap Yes/No + the older
  "AZT collaboration service required" widget).
- **Popup is now modal-blocking** (`auto_dismiss=False`). The user
  can't tap past it to reach a settings screen or picker that
  would itself fail with "server_apk_not_installed". Resolves the
  user-reported "in the client settings page, asking to Select
  Project resulted in widget 1" — once bootstrap fires, settings is
  unreachable until the user installs or quits.
- **Quit button replaces "Dismiss".** Label is "Quit {App.title}"
  (e.g. "Quit AZT Recorder") — falls back to plain "Quit" if the
  host hasn't set a title. Tapping it dismisses the popup AND
  calls `App.get_running_app().stop()`. Without the server APK
  the peer can't function, so leaving it running was the wrong UX.
- **Install button shows live progress in the popup body.** While
  `check_for_update` runs, the body label updates with
  "Downloading 45%…", "Release in progress — retrying in 5s…",
  "Installing…", "Installed." (or "Install pending. Reopen this
  app when finished." on the polling-timeout branch). The popup
  stays open through the whole flow — no more "I tapped Install
  and nothing happened" because the popup dismisses-and-routes-
  elsewhere. Buttons disable while the worker runs to prevent
  double-taps.
- **`install_server_apk_popup` gained context parameters**
  (`body_message`, `current_server_version`, `install_target_package`,
  `install_label`, `title`) so the same popup serves both the
  missing-server case (default) and the too-old-server case
  (passed by `_prompt_server_update`). Different body text and
  Install-button label, same machinery.
- **Bootstrap dead code pruned.** `_do_server_install` and
  `_quit_app` removed — both were one-call helpers absorbed into
  the popup refactor. `_yes_no` survives for the self-update
  prompt (different decision: peer can keep running on decline).
- **`docs/CLIENT_INTEGRATION.md`** added — the canonical "what every
  client must do" checklist. Sections: symlinks, buildozer.spec
  permissions / signing / manifest extras, **bootstrap wiring +
  the four caller invariants**, **don't roll your own server-missing
  UI** (the source of the user-reported bugs), translation chain,
  `App.title` for the Quit button, LIFT / audio / image access,
  recovery, testing. `azt_collab_client/CLAUDE.md` now points to
  it as the canonical reference.
- Translations (fr): "Quit", "Quit {app}", and the longer
  bootstrap-prompt body text used by the popup
  (`This app needs the AZT Collaboration service ({name}) to sync
  your data. Tap Install to download and install it. Android will
  ask you to confirm before the install starts.`).

### azt_collabd 0.28.3 + azt_collab_client 0.28.3 — install-popup auto-download, asset filename fix, install-completion polling, release cache, bookkeeping
- **Asset filename fix.** Every codebase reference to the server APK
  asset was ``azt_collab.apk`` (with underscore), but the Android
  ``package.name = aztcollab`` in server_apk/buildozer.spec.tmpl
  drops separators per the suite naming table — actual published
  asset is ``aztcollab.apk``. The 5 hardcoded references —
  ``bootstrap._SERVER_ASSET_DEFAULT``, ``CollabUIApp.share_apk`` /
  ``update_app``, ``PickerApp.share_apk`` / ``update_app`` — all
  fixed. The bootstrap workflow's GitHub-API asset lookup was
  returning "no aztcollab.apk in release" 404s because of this; now
  matches the actual release feed.
- **`SERVER_APK_INSTALL_URL` is now a direct-download URL**
  pointing at
  ``https://github.com/kent-rasmussen/azt-collab/releases/latest/download/aztcollab.apk``.
  GitHub's stable redirect serves the most recent matching asset,
  so the URL doesn't need updating per release.
- **`install_server_apk_popup` triggers auto-install** instead of
  only opening the browser. Tap Install → ``check_for_update``
  fetches the latest ``aztcollab.apk`` asset, streams it to
  ``$AZT_HOME/updates/``, and dispatches Android's system
  installer. The popup's "Open install page" affordance is
  retained as a fallback for users whose Android can't trigger the
  install intent. Progress strings flow back through the popup
  body and the host's ``on_status`` sink.
- **Install-completion polling** for cross-package installs
  (server-from-peer). New ``check_for_update(install_target_package=...)``
  parameter. After dispatching the install intent, the helper
  polls ``PackageManager.getPackageInfo`` every 5s for up to 5min,
  fires ``on_status('Installed.')`` when the version flips to the
  freshly-downloaded one, and ``on_status('Install pending.
  Reopen this app when finished.')`` on timeout. Closes the
  long-standing UX wart where status hung at "Installing…" forever
  after the user backed out of the system installer or the install
  finished out-of-foreground. Self-installs (peer-from-peer) skip
  polling — the install replaces the running peer, so polling
  would block forever. Bootstrap passes the server's package name
  on its server-install path; the same path is taken when the
  ``install_server_apk_popup`` Install button fires.
- **Per-process release cache** for ``_fetch_latest``. 5-minute TTL,
  keyed by repo slug. Closes the rate-limit hazard where a
  bootstrap launch + a settings-screen Update tap + multiple peers
  behind one NAT could collectively drain the GitHub anonymous
  60/hour budget; subsequent calls within the TTL hit the cache.
- **Caller invariants** — the four contracts the bootstrap caller
  must honor (asset name match, parseable tag, prerelease flag,
  ``REQUEST_INSTALL_PACKAGES`` permission) are now consolidated as
  a top-level "Caller invariants" section in
  ``azt_collab_client/ui/bootstrap.py``. Each was scattered across
  the function docstring + the client CLAUDE.md recipe + update.py
  comments before; now a single canonical list.
- **`p4a.sign = True` removed** from the suite's
  ``server_apk/buildozer.spec.tmpl`` (separately from the earlier
  ``android.signing.*`` cleanup). Confirmed empirically: this spec
  key is also dead config; signing depends solely on the
  ``P4A_RELEASE_KEYSTORE`` env vars. Memory feedback updated.
- **Daemon version bump 0.27.0 → 0.28.3** (lock-step with client)
  to signal that this round touched both packages. No wire-format
  change; cross-floors (``MIN_CLIENT_VERSION`` /
  ``MIN_SERVER_VERSION``) stay at 0.27.0 — older clients/servers
  still talk to this daemon/client without issue.
- **`docs/p4a_hook_picker_intent.md` path-leak scrub.**
  ``/home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py`` →
  ``$P4A_HOOK`` (the env-var-resolved path) for public-repo
  consumption.
- **"Not now" on server install closes the peer app.** Without
  the server APK the peer can't function (no daemon → no sync, no
  project picker), so dropping the user into a broken state is
  worse than asking them to relaunch. Server-*update* decline
  doesn't quit (peer can still work against the older server,
  bound by ``MIN_SERVER_VERSION``); self-update decline doesn't
  quit either (peer is fine at current version). New
  ``_quit_app`` helper in bootstrap.py.
- **Download retry on transient HTTP statuses** (``404, 429, 500,
  502, 503, 504``). Load-bearing case: GitHub publishes the release
  JSON before the asset binary finishes uploading, so
  ``browser_download_url`` briefly 404s. Three attempts with linear
  backoff (5s, 10s, 15s ≈ 30s total). Between attempts, the user
  sees translated "Release in progress — retrying in Ns…" so a
  hung worker thread is no longer a confusion. New
  ``on_status`` parameter on ``_download``.
- **Translations (fr)** for the new state strings: "Installed.",
  "Install pending. Reopen this app when finished.",
  "Release in progress — retrying in {s}s…", and
  "Tap Install to download and install it. Android will ask you to
  confirm before the install starts."

### azt_collab_client 0.28.1 — bootstrap hardening + first automated tests
- **Filter prereleases** from the latest-release probe in
  `update.py:_fetch_latest`. Walks `/releases?per_page=20` for the
  first stable entry; falls back to `/releases/latest` if every
  recent release is a prerelease or the listing endpoint refused.
  Closes the v0.28.0 bug where a project pushing a `vN-rc` tag would
  silently auto-install onto every peer.
- **bootstrap idempotence guard.** A second `bootstrap()` call within
  the same process now no-ops. Prevents double-prompting when an
  on_start hook fires twice during a Kivy reload or two startup
  hooks both wire the helper.
- **Decline memory.** When the user taps "Not now" on a prompt, the
  declined version is persisted to
  `$AZT_HOME/config.json :: bootstrap.declined.<repo>=<version>`.
  Subsequent launches skip the prompt for that exact version; a
  new upstream release moves us off the recorded value
  automatically (string compare, not semver).
- **Disambiguate "server APK absent" from "daemon unreachable"** by
  probing `PackageManager.getPackageInfo('org.atoznback.aztcollab')`
  before issuing the install prompt. If the package is installed but
  the daemon happens to be down (no network, OOM-killed mid-call),
  the helper now skips the install prompt and continues to the
  self-check instead of asking the user to install something that's
  already there. New status string
  "AZT Collaboration installed but unreachable. Continuing offline."
- **First automated test scaffold.** New `azt-collab/tests/` directory
  with pytest fixtures (per-test `$AZT_HOME` redirection, jnius stub,
  Kivy headless flags, platform monkeypatch) and five test modules
  covering version-tuple corner cases, GitHub-API mocks for
  `check_for_update`, bootstrap dispatch + idempotence + decline
  memory + package-presence disambiguation, the `github.confirmed`
  store lifecycle, and a translation-coverage drift detector.
  Run with `pytest tests/ -q`. CLAUDE.md updated to retire the
  "no automated test suite anywhere in the suite" claim.
- **`docs/research_notes_2026-05.md`** captures the state of the
  art for the technologies we depend on (Android 16, the March-2026
  sideloading lockdown, ACTION_VIEW deprecation in favor of
  PackageInstaller, buildozer/Kivy versions, GitHub API behavior).
  Action items are owned in the file. Refresh before each major
  release.
- **`docs/test_plan.md`** is the canonical failure-mode matrix. Every
  bug found in the bootstrap workflow lands here as a row in §10
  before it gets fixed.
- One new translation: "AZT Collaboration installed but unreachable.
  Continuing offline."

### azt_collab_client 0.28.0 — bootstrap() one-call peer entry point
- New `azt_collab_client.ui.bootstrap(...)` helper. Peers call it
  once on `App.on_start` and the helper handles, in this order:
  1. `check_server_compat()`. On `server_unreachable` →
     "Install AZT Collaboration?" Yes/No popup → `check_for_update`
     against `kent-rasmussen/azt-collab` → Android system installer.
     `server_too_old` runs the analogous "Update AZT Collaboration?"
     prompt. `client_too_old` jumps to step 2.
  2. Probe peer's own latest release on GitHub. If newer →
     "Update <peer>?" Yes/No → download+install the peer's APK.
  3. `on_done` — every up-to-date / declined / completed-install
     branch lands here so the host's normal startup always
     resumes.
  Suite UX rule encoded by this helper: **the user installs one
  APK** (the peer they opened); the standalone server APK and all
  subsequent updates are provisioned by the peer itself on first
  run. Spawns a worker thread for the version probes so first
  paint isn't blocked; popups marshal back to the Kivy UI thread.
- Android-only effects. Desktop hosts call `on_done` immediately.
- Buildozer requirement documented (`REQUEST_INSTALL_PACKAGES` in
  the peer's `android.permissions`); without it the install intent
  silently no-ops.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  the recorder, viewer, and any future peer can wire one
  ten-line `App.on_start` call and let the helper take it from
  there.
- New translations (fr): "AZT Collaboration", "Checking
  installation…", "Install AZT Collaboration?", "Update AZT
  Collaboration?", "Update {name}?", body strings for each prompt,
  "Install" / "Update" / "Not now" buttons, "Update needed" info
  popup, "Updating {name}…", "AZT Collaboration is up to date.",
  and the rare client-too-old-no-newer-release fallback message.

### azt_collabd 0.27.0 + azt_collab_client 0.27.0 — symmetric host credential flow
- **`github.confirmed` is now a stored flag**, not derived. Mirrors
  the existing GitLab semantics: set true by a successful live test,
  reset to false on any settings change (token save, app-install
  flag flip, disconnect). Per-host shape is now uniform — both
  GitHub and GitLab expose `connected` ("settings present") and
  `confirmed` ("tested OK against the host's API").
- New endpoint `POST /v1/credentials/github/test` (handler
  `_h_test_github`) and matching client wrapper
  `azt_collab_client.test_github_credentials()`. Hits
  `api.github.com/user` with the stored access token; on success
  also probes `api.github.com/user/installations` so the same Test
  button refreshes both `confirmed` and `app_installed` in one
  user gesture, matching the GitLab Test pattern.
- Auth helper `azt_collabd.auth.test_github_credentials(token)`
  added alongside the existing `test_gitlab_credentials` —
  consistent shape, same return dict (`{valid, server_username,
  app_installed, error}`).
- **`GitHubConnectScreen` is now state-aware.** Three shapes picked
  in `on_pre_enter` from `credentials_status['github']`:
  * not connected → device-flow box visible, manage hidden,
    `begin()` auto-fires.
  * connected, not confirmed → manage view (Test + Install GitHub
    App if not installed + Re-authenticate + Disconnect); device
    flow hidden; nothing auto-fires.
  * connected, confirmed → same controls plus a "(verified)"
    badge in the status line.
  Show/hide uses the Kivy hide/show pattern (height: 0, opacity: 0)
  per `~/.claude-sil/CLAUDE.md`. The screen is fully self-contained:
  Disconnect / Re-authenticate / Install-app no longer require the
  user to bounce back to settings.
- `SettingsScreen.connect_github()` reduced to a one-liner navigate.
  Auto-firing `begin()` on every entry to the screen is gone — the
  user with a token already on file isn't re-prompted for device
  flow; they get the manage view and pick Test or Re-authenticate
  themselves.
- Lock-step bump to 0.27.0 with cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.27.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.27.0. New wire endpoint
  + bundled-client peer APKs need the floor bump or version
  mismatches stay silent (ref. memory note on
  MIN_CLIENT_VERSION discipline).
- Translations (fr) for the new state-aware strings:
  "Re-authenticate", "Disconnect", "Install GitHub App",
  "Connected as {username} (verified).", the not-yet-tested and
  app-not-installed variants, "Token rejected by GitHub. Tap
  Re-authenticate.", "Could not open install page: {error}", and
  the "Opening {uri}\nWhen you finish on GitHub..." prompt.

### azt_collabd 0.26.0 + azt_collab_client 0.26.0 — in-app self-update
- New `azt_collab_client.ui.check_for_update(repo, current_version,
  asset_filename, on_status, ...)` reusable updater. Spawns a worker
  thread, polls `GET /repos/{repo}/releases/latest`, compares the
  release tag to the caller's `__version__` as a semver tuple, and on
  a newer release downloads the matching asset and dispatches
  `Intent.ACTION_VIEW` with the APK MIME type so Android's system
  installer takes over. All callbacks marshal back to the Kivy UI
  thread; non-Android hosts get a translated
  "APK install is only available on Android." through `on_error`.
- `REQUEST_INSTALL_PACKAGES` added to
  `server_apk/buildozer.spec → android.permissions`. The helper
  detects Android 8+ "Install unknown apps" gating via
  `PackageManager.canRequestPackageInstalls()` and routes the user to
  `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` on first use.
- `azt_collabd.configure(update_repo=...)` + `AZT_UPDATE_REPO` env var
  + `azt_collabd.config.update_repo()` accessor; default is
  `kent-rasmussen/azt-collab` (the canonical release feed at
  https://github.com/kent-rasmussen/azt-collab/releases/latest).
- "Update this app" button + status `BodyLabel` added to
  `<SettingsScreen>` directly under the existing "Share this app"
  row. Hosted by both `CollabUIApp.update_app` (standalone settings
  subprocess) and `PickerApp.update_app` (in-process settings reached
  from the picker's gear). Same KV `app.update_app()` resolves on
  whichever App owns the screen.
- `azt_collab_client/CLAUDE.md` documents the integration recipe so
  peers (recorder targeting `kent-rasmussen/azt-recorder`, future
  viewer, …) can wire the same button into their own settings screens
  by passing their own `repo` / `__version__` / `asset_filename`.
- Translations added (fr): "Update this app", "Up to date.",
  "Checking for updates…", "Downloading {pct}%…",
  "Preparing install…", "Installing…",
  "APK install is only available on Android.", and the failure
  variants ("Update check failed: {error}", missing-tag /
  missing-asset / missing-URL detail strings, "Download failed",
  "Install failed", "Could not create download dir", and the
  Install-unknown-apps prompt).
- Lock-step bump to 0.26.0 across `azt_collabd` and `azt_collab_client`
  (no wire-format change; just keeping the cross-floors aligned now
  that the client gained shared UI surface peers will rely on).

### azt_collabd 0.25.2 — "Share this app" on the settings screen
- Added a Share-this-app row to `<SettingsScreen>` (`azt_collabd/ui/app.py`),
  positioned right under the Back NavBtn so it leads the scrollable
  body the same way the recorder's settings screen does. Hands the
  running server APK to Android's share sheet via the existing
  `azt_collab_client.ui.share_running_apk` helper — useful for
  onboarding teammates to the collab service. Hosted by both the
  standalone `python -m azt_collabd ui` (`CollabUIApp.share_apk`) and
  the in-process settings reached from the picker's gear
  (`PickerApp.share_apk`); the KV's `app.share_apk()` resolves on
  whichever App owns the screen at runtime.
- Icon (`share_dark.png`) sourced via `azt_collab_client.ui.icon_path`
  and threaded into the KV through `register_kv` next to the existing
  font-name substitution. Desktop hosts get the button too; tapping
  it surfaces the translated "APK sharing is only available on
  Android." message via the helper's `on_error` callback.
- French translations added for `Share this app`, `Share app`, `Error`,
  and the three `share_running_apk` failure messages
  (`APK sharing is only available on Android.`, the MediaStore-insert
  failure, the generic `Could not share APK:` wrapper).

### azt_collab_client 0.25.2 — public `ensure_mo` for peers
- `azt_collab_client.i18n.ensure_mo(locale_dir, domain, lang)` exposes
  the lazy `.po` → `.mo` compile path peers were previously missing.
  Peer i18n modules call it before `gettext.translation(...)` so they
  can ship `.po`-only and skip the external `msgfmt` build step the
  same way the client does. Writes the `.mo` next to the `.po`; on
  Android that's inside the APK's private filesDir, which is
  writable. See the *Internationalization (i18n)* section of
  `azt_collab_client/CLAUDE.md` for the integration recipe.
- `_ensure_mo(lang)` is now a thin wrapper around `ensure_mo` for the
  client's own domain — no behaviour change for the client itself.

### azt_collabd 0.25.1 + azt_collab_client 0.25.1 — French catalog catch-up
- Added 28 missing French translations to
  `azt_collab_client/locales/fr/LC_MESSAGES/azt_collab_client.po`
  covering the popup confirm-langcode flow, the clone-URL popup's
  inline `code: ` / `change code` / `OK` affordances, the daemon
  settings UI's publish row + GitLab Test screen, and the picker's
  empty-result fallback dialog. Catalog Project-Id-Version follows
  the package bump.
- Wrapped a stray ``'Open settings'`` literal in
  `azt_collabd/ui/picker_app.py:852` (clone-failure auth modal) with
  `_tr` so the translation already in the catalog actually fires.

### azt_collabd 0.25.0 + azt_collab_client 0.25.0 — synchronized release
- Lock-step version bump of both packages plus their cross-floors:
  `azt_collabd.MIN_CLIENT_VERSION` → 0.25.0,
  `azt_collab_client.MIN_SERVER_VERSION` → 0.25.0. Intended to flush
  every peer APK through a rebuild so the cumulative work since the
  prior synchronization point lands in lockstep across the suite.
  The user-visible content of this release is the union of every
  entry below, plus the four-month gap of additive changes that
  preceded them. After this synchronization any peer running an
  older bundled client will surface `client_too_old` from
  `check_server_compat()` and any client talking to an older daemon
  will surface `server_too_old`, prompting an update on either side
  rather than silently degrading.
- Notable behaviour requiring lock-step:
  - `last_project` server-tracked via `/v1/recent/last_project`
    (older clients keep using their own sandbox, breaking on
    Android).
  - GitLab Test button drives `_h_test_gitlab` and the new per-host
    `confirmed` flag.
  - `Project.last_commit`, `ProjectStatus.commits_ahead` exposed on
    the wire; `_h_init_project` writes `last_sync` / `last_commit`
    back to projects.json on a successful publish.
  - `_resolve_path` reads `working_dir` from the registry; URIs
    decouple from on-disk dir naming.
  - `:provider` stdio bridge so daemon `print(..., file=sys.stderr)`
    actually reaches logcat.
  - dulwich `repo.refs[name]` (with `KeyError`) replaces the
    incorrect `.get()` call that was failing every sync silently;
    post-push remote-mirror update fixes `(+N)` indicator stickiness.

### azt_collabd 0.21.4 + azt_collab_client 0.24.0 — back from picker also returns to last project
- 0.21.3 only special-cased the BCP-47 langpicker; back from the
  *project picker screen itself* still hit the `if self.sm.current
  == 'picker': return False` early exit and let Android close the
  Activity with `RESULT_CANCELED`. The user's case from logcat —
  picker screen opens, back press, no `[picker_app]` trace at all,
  recorder receives an empty cancel — is exactly that path. New
  `_exit_to_last_project_or_cancel` helper centralises the
  emit-resumable-or-cancel shape, called from both the `picker` and
  `langpicker` branches of `_navigate_back`. Either exits the
  picker subprocess in one back-press, with the recorder receiving
  either the resumable project or a clean cancel.

### azt_collabd 0.21.3 + azt_collab_client 0.24.0 — back-from-langpicker always exits subprocess
- 0.21.1 added the langpicker→last_project special case but only
  when `last_project()` resolved; otherwise it fell through to the
  default `'picker'` target, which left the user on the project
  picker screen requiring a second back-press to actually exit.
  Now the langpicker back-press always exits the picker subprocess
  in one step: emit `last_project` if it resolves (recorder
  auto-resumes), or emit a clean cancel if it doesn't (recorder's
  `_handle_pick` silently returns to whatever it was showing).
  Either way, the user lands on the recorder, never on the project
  picker. New `_emit_cancel_and_quit` helper factors the
  `setResult(RESULT_CANCELED) + finish` shape out of
  `on_request_close` so it's reachable from anywhere.

### azt_collabd 0.21.2 + azt_collab_client 0.24.0 — URI resolution decoupled from on-disk dir name
- **Cloned project's LIFT URI returned `FileNotFoundException`.**
  `_resolve_path` in the ContentProvider was building
  `$AZT_HOME/projects/<langcode>/...` from the URI's first segment,
  but the picker_app's clone worker uses the URL's repo basename for
  `dest_dir` (e.g. `en_Demo.git` → `projects/en_Demo/`), while the
  URI handed back to peers uses the user-chosen langcode (e.g.
  `content://.../en/SILCAWL.lift`). Mismatch → resolver looked under
  the wrong directory → file-not-found → recorder's "lift namespace
  scan failed: forbidden: /en/SILCAWL.lift" log line. `_resolve_path`
  now consults `projects.get(langcode).working_dir` so URI resolution
  is independent of how the on-disk directory was named at clone
  time. Falls back to the legacy `<projects>/<langcode>` path when
  the project isn't in the registry, preserving pre-registry URIs.

### azt_collabd 0.21.1 + azt_collab_client 0.24.0 — back-from-langpicker resumes last project
- `_navigate_back` in `picker_app.py` now special-cases the
  `langpicker` screen: a user who reached Start-New and then changed
  their mind almost always wants to return to whatever project they
  had open before, not be re-parked at the project list. When
  `last_project()` resolves to a live registered project, back from
  `langpicker` emits that project's path and exits the picker
  subprocess (recorder auto-resumes). Cold-start fallback (no
  recorded last project) still goes to the picker screen so the
  user has somewhere to land.

### azt_collabd 0.21.0 + azt_collab_client 0.24.0 — sync was failing every cycle; remote-mirror not bumped after push
- **Sync was raising `AttributeError` on every cycle.** Pre-0.21.0
  `_sync_repo_locked` did `repo.refs.get(branch_ref) or repo.head()`,
  but `dulwich.refs.DiskRefsContainer` has no `.get()` method — only
  `__getitem__` (raises `KeyError`) and `read_ref()`. The call
  raised `AttributeError` post-fetch on every sync, propagated to
  `scheduler._fire`'s catch-all, and the job was marked
  `PUSH_FAILED`. Symptom: commits piled up locally but never
  pushed; "I see Audio recordings by Recorder commits showing up on
  GitHub minutes apart" was the queue draining whenever a build
  with the diagnostic try/except (added in 0.20.9) happened to be
  running. Replaced with a small `_read_ref(name)` helper that
  uses `__getitem__` + `KeyError`. Both `branch_ref` and
  `remote_ref` reads, plus the retry-loop's post-fetch read, now
  use it.
- **`commits_ahead` stuck at `(+N)` after a successful push.**
  Symptom: `[sync] done: codes=['NOTHING_TO_COMMIT', 'PUSHED']` ran
  cleanly but the recorder's indicator kept showing `(+10)`. Cause:
  `porcelain.push` advances the remote on GitHub but doesn't update
  the *local mirror* `refs/remotes/origin/<branch>` —
  `_count_commits_ahead` then compares the just-pushed `local_sha`
  against the pre-push mirror and reports the count of just-pushed
  commits as still-pending. Bumping the mirror explicitly after a
  successful push (`repo.refs[remote_ref] = local_sha`) reflects
  what CLI `git push` does and lets `commits_ahead` read 0 on the
  next status poll.

### azt_collabd 0.20.9 + azt_collab_client 0.24.0 — sync-hang trace, post-fetch ref reads
- The 0.20.8 trace narrowed the hang to the `local_sha = repo.refs.get(...)
  or repo.head()` line, so 0.20.9 splits that into separate
  `[sync-trace]` prints around `repo.refs.get(branch_ref)`,
  `repo.head()`, and `repo.refs.get(remote_ref)`. Whichever call
  doesn't produce its trailing print is where dulwich is wedging.
  Each is wrapped in try/except to surface a raise vs a hang
  cleanly.

### azt_collabd 0.20.8 + azt_collab_client 0.24.0 — sync-hang trace
- `[sync-trace]` prints between every step in `_sync_repo_locked`
  after the fetch (`fetch begin/done`, `local_sha`, `remote_sha`,
  `needs_merge`, `fast-forward`/`local ahead`/`merge_diverged
  begin/done`, `push loop begin`, `push attempt`, `push done`/`push
  raised`). Pinpoints exactly which step wedges between fetch
  returning and the function exiting — your latest log shows
  `fetch` returning HTTP 200 followed by silence until the next
  status-poll, so the hang is somewhere between `local_sha = ...`
  and the `push` HTTPS call.

### azt_collabd 0.20.7 + azt_collab_client 0.24.0 — last_sync on publish + `:provider` stdio bridge
- **Publish stamps `last_sync` / `last_commit`.** `_h_init_project`
  ran the publish (commit + push) and updated `remote_url` in
  `projects.json`, but did **not** stamp `last_sync` from the result
  codes. Sister handlers in `scheduler._run_sync` and
  `_h_project_sync` already did. Symptom: a successful publish left
  `Project.last_sync == 0`, the recorder's UI read that as "never
  synced" and kept showing the "data isn't being backed up" warning
  forever — even though the repo was sitting on github.com fully
  pushed. After this fix, a publish that returns `PUSHED` (or
  `COMMITTED_AND_PUSHED`) immediately stamps `last_sync` so the
  indicator flips on the next refresh.
- **Daemon prints now reach logcat.** `server_apk/service.py` calls
  a new `_bridge_stdio_to_logcat()` at module top that pipes
  `sys.stdout` / `sys.stderr` through `android.util.Log` under tag
  `python`. p4a auto-redirects stdio for `PythonActivity` (the
  Activity process where the picker / settings UI / recorder Kivy
  apps run), but not for `PythonService` — and the daemon
  (server.py, scheduler.py, repo.py) lives in the `:provider`
  process driven by PythonService. Pre-0.20.6 every `print` from
  daemon code went to a black hole, so functionally correct sync
  flows (RPC returns a job_id, the timer fires, the repo gets
  pushed) appeared in logcat as if nothing happened. After this fix
  the `[sync-async]` / `[sync-debounce]` / `[sync-fire]` / `[sync]`
  traces actually show up.

### azt_collabd 0.20.5 + azt_collab_client 0.24.0 — picker failure → notify + exit
- After `_pick_project_android`'s one-retry loop exhausts on
  `unexpected_cancel`, the client now schedules a Kivy modal on the
  UI thread: "The project picker failed to return a result. The app
  will now close — please reopen it." with a single OK button that
  calls `App.get_running_app().stop()` (and `os._exit(0)` as a belt-
  and-braces). Lives in the client so every peer using
  `pick_project()` gets the same fallback without per-host wiring;
  documents in the function docstring that `unexpected_cancel` is
  now a terminal status from the caller's perspective (the user
  will see the modal and exit before the caller observes the
  return value). Triggered by the user reporting empty-recorder
  windows three times in one debug session.

### azt_collabd 0.20.5 + azt_collab_client 0.23.6 — client-side request_sync trace
- `[sync-client] request_sync(<lang>, ...)` printed at every entry,
  with success/failure/transport branches. Closes the visibility gap
  between "the recorder called the RPC" and "the daemon received it"
  — if the client log is silent, the recorder never tried; if the
  client log shows a send but the daemon's `[sync-async]` is silent,
  the RPC is being dropped at the transport layer.

### azt_collabd 0.20.5 + azt_collab_client 0.23.5 — picker auto-retry on RESULT_CANCELED-with-data
- `_pick_project_android` now wraps the launch+wait in a one-retry
  loop. The picker contract is RESULT_OK→data /
  RESULT_CANCELED→no-data; the combo "non-OK + data attached"
  shouldn't be reachable normally (Android can synthesize it on
  back-press during `setResult`, or with OEM launcher tampering).
  Pre-0.23.5 we'd silently swallow that case as `'cancelled'` and
  drop the user on a recorder window with no project. Now the
  client classifies it as `'unexpected_cancel'` and re-launches the
  picker once, so the user gets another shot at choosing.
- The retry-loop refactor extracts the inner launch/wait logic into
  `_pick_project_android_once`; `attempt=` is included in the
  `[pick_project] _on_result: ...` trace so each invocation is
  identifiable in logcat.

### azt_collabd 0.20.5 + azt_collab_client 0.23.4 — full sync chain trace
- Three new diagnostics so a request → debounce → fire → run path is
  visible end-to-end in logcat: `[sync-async] <lang>` at RPC arrival
  in `_h_project_sync_async`, `[sync-debounce] <lang>` from
  `scheduler.request_sync` (so we see whether the recorder's
  `_auto_commit_sync` reached the queue), and `[sync-fire] <lang>`
  from `_fire` (so we see whether the debounce timer actually fired
  before the daemon process got recycled).

### azt_collabd 0.20.4 + azt_collab_client 0.23.4 — empty-registry disk scan
- When `_h_list_projects` returns zero entries, also print what
  `$AZT_HOME/projects/` actually contains on disk. Distinguishes
  "registry wiped but working trees survived" (recoverable: a
  future endpoint can scan + auto-register) from "filesDir gone"
  (server APK clean-installed; nothing to recover).

### azt_collabd 0.20.3 + azt_collab_client 0.23.4 — recent-state stderr trace
- `azt_collab_client/CLAUDE.md` rule #2 expanded to cover the
  *gating* failure mode (peers silently skip auto-sync on Android
  because their local filesystem check returns False) and to
  include the verbatim fix-shape snippet for `_project_has_remote`,
  so future Claude sessions touching a peer don't have to re-derive
  the daemon-served replacement.

### azt_collabd 0.20.3 + azt_collab_client 0.23.3 — recent-state stderr trace
- Diagnostic prints around every read/write of `last_langcode`:
  daemon-side `[recent] _touch_project(...) → /path/to/config.json`,
  `[recent] GET /v1/recent/last_project → 'lang' (from /path/...)`,
  and matching `POST` line; client-side
  `[recent] last_project → 'lang'` / `set_last_project(...) sent` /
  `ServerUnavailable: ...`. Pairs the path being written with the
  path being read so a divergent-`$AZT_HOME` bug shows up in logcat
  side-by-side instead of having to be inferred. No behaviour change.

### azt_collabd 0.20.2 + azt_collab_client 0.23.2 — MIN_CLIENT_VERSION floor for the recent.py RPC migration
- **`MIN_CLIENT_VERSION` raised to 0.23.0.** Pre-0.23 clients keep
  reading `$AZT_HOME/config.json::recent.last_langcode` from their
  own package's filesDir, which on Android sits in a different
  sandbox from the daemon's. The daemon stamps last_project on
  every langcode-bound RPC, but the peer's bundled client never sees
  it — recorder auto-resume falls through to the picker on every
  restart. Bumping the floor makes `check_server_compat()` return
  `client_too_old` instead of silently degrading, so the recorder
  surfaces the "please update" warning. Saved-memory note
  `feedback_min_client_version.md` documents this exact failure mode.

### azt_collabd 0.20.1 + azt_collab_client 0.23.2 — publish-row sticking after success
- **`_h_init_project` writes remote_url back to `projects.json`.**
  Symptom: a publish that returned `PUSHED` left the project's
  `Project.remote_url` empty in the registry, so the settings UI
  immediately re-rendered the publish row asking the user to publish
  again. `_init_repo` updates the *local* git config but the
  registry is a separate datastore; the back-write was missing. Now
  the daemon walks `projects.list_all()`, finds the entry whose
  `working_dir` matches, and writes the URL via
  `projects.set_remote_url`.
- **`_pick_publish_candidate` consults the live git remote.** Even
  with the back-write fix, projects published before 0.20.1 carry
  an empty cached `remote_url`. The settings UI now also reads the
  authoritative value via `project_status(langcode).remote_url`
  (which checks `.git/config`); the row hides if either source
  reports a remote. Defensive belt-and-braces so existing published
  repos behave correctly without a manual reconcile.

### azt_collabd 0.20.0 + azt_collab_client 0.23.1 — `commits_ahead` on ProjectStatus
- **`commits_ahead: int` on `ProjectStatus`.** Filed by recorder
  1.37.6 in `NOTES_TO_DAEMON.md`: the recorder's sync indicator
  needs the count of local commits not yet pushed to the remote so
  it can render `(+n)` instead of an opaque `*` marker.
  `repo_status_summary` now returns a 4-tuple
  `(branch, remote_url, n_changes, commits_ahead)`; `_h_project_status`
  forwards `commits_ahead` on the wire. Computed locally from
  `refs/heads/<branch>` vs. `refs/remotes/origin/<branch>` (no
  network round-trip), so a stale cache may under-report — the
  recorder's UX contract is "OK on uncertainty," so under-reporting
  is the right failure mode. Returns 0 whenever the local cache
  doesn't have a remote ref to compare against (no remote
  configured / never pushed). Client dataclass already had the
  field with `default 0` for forward-compat. NOTES_TO_DAEMON entry
  cleared.

### azt_collabd 0.19.1 + azt_collab_client 0.23.1 — server-canonical recent state + last_commit
- **`azt_collab_client/CLAUDE.md` rule #2 added** — "no reading
  project state from the local filesystem either," with the
  recorder's `_project_has_remote()` (dulwich.Repo on the working
  dir) called out as the canonical anti-pattern. Reads silently work
  on desktop and silently fail on Android because the daemon's
  working_dir lives in the server APK's private filesDir. Future
  peers must use `project_status(langcode)` for state-shaped checks.
- **Publish outcome message no longer clobbered by refresh.**
  `_publish_done` was setting the message *then* calling `refresh()`,
  which started by clearing `msg.text`, so the user only saw
  "Publishing..." and never the result. Reorder: refresh first, then
  set the outcome. Re-enables the button on failure.
- **`[publish]` stderr trace** in `_publish_worker`: prints the
  arguments going into `init_project` and the resulting `Result.codes()`
  (or the exception). Pairs with the `[sync-rpc]` / `[sync]` traces
  added in 0.18.2 so a publish failure has a logcat trail.

### azt_collabd 0.19.0 + azt_collab_client 0.23.0 — server-canonical recent state + last_commit
- **`last_project` is now server-tracked.** Was: each peer wrote
  `$AZT_HOME/config.json::recent.last_langcode` directly, which broke
  on Android where every peer's sandbox holds its own config.json
  (the recorder's write and the settings-UI subprocess's read landed
  in different files), and broke on desktop whenever a load path
  forgot to call `set_last_project`. Now: every langcode-bound RPC
  (`open_project`, `project_status`, `sync`, `sync_async`, `register`,
  `init`, `clone`, `from_template`, `rename`) auto-stamps via the new
  `server._touch_project` helper, and `last_project()` /
  `set_last_project()` are thin wrappers around new endpoints
  `GET`/`POST /v1/recent/last_project`. Single source of truth across
  peers and platforms; peers don't have to remember to call
  `set_last_project` from any specific load path.
- **Publish picker simplified.** With server-canonical
  `last_project`, the unpublished-projects-preference fallback in
  `SettingsScreen._pick_publish_candidate` (added in 0.18.1 to work
  around stale recorder-written state) is gone. The settings UI now
  resolves `last_project()` straight to the candidate Project; if
  that doesn't return a live project, the publish row hides — which
  is the correct UX, because nothing has been touched.
- **`Project.last_commit` field, separate from `last_sync`.** Filed
  by the recorder team in `azt_collab_client/NOTES_TO_DAEMON.md`:
  peer sync indicators couldn't distinguish "committed locally but
  not pushed" from "silently broken" because `last_sync` only
  stamped on `PUSHED` / `PULLED` / `COMMITTED_AND_PUSHED`. Daemon
  now also stamps `last_commit` on `COMMITTED_LOCAL` /
  `COMMITTED_NO_REMOTE` / `COMMITTED_AND_PUSHED` (any path where a
  commit object hit the working tree). Both fields ride on
  `Project` and `ProjectStatus`; pre-0.19 daemons that don't emit
  `last_commit` get a 0.0 default in the client dataclass for
  forward-compat. `NOTES_TO_DAEMON.md` entry deleted per its own
  instructions.
- **Sync trace lines** retained from 0.18.2: `[sync]` lines from
  `scheduler._run_sync` and `[sync-rpc]` from `_h_project_sync` so
  successful syncs show up in `adb logcat -s python`.

### azt_collabd 0.18.2 + azt_collab_client 0.22.1 — settings UX cleanup
- Sync trace lines on stderr (visible via `adb logcat -s python` on
  Android): `[sync] <lang> ... starting` / `... done: codes=[...]` from
  `scheduler._run_sync` and `[sync-rpc] ...` from `_h_project_sync`.
  Previously a successful sync emitted nothing — the structured
  `Result` carried the outcome but there was no trail in logcat to
  confirm the daemon had even seen the request.
- Publish-candidate fallback also prefers unpublished projects (the
  filtered `list_projects()` search would otherwise pick a
  more-recently-synced sibling that was already published, hiding
  the publish row even though a sibling project still needed
  publishing).
- Publish candidate falls back from `last_project()` to the
  highest-`last_sync` entry in `list_projects()` when the suite-wide
  "last opened" key is empty (older recorder load paths don't always
  write it). Diagnostic stderr lines from `_pick_publish_candidate`
  surface why the row stayed hidden.


- **GitLab "Connect" + Test button.** The settings screen's GitLab
  affordance is now labelled "Connect to GitLab" (was "Set GitLab
  credentials") to match GitHub's wording. The form screen replaces
  the bare "Save" button with a single "Test connection" button: the
  daemon-side `_h_test_gitlab` endpoint runs a live check against
  `gitlab.com/api/v4/user`, and only on success does it persist the
  credentials and stamp `gitlab.confirmed=True` in the store, so the
  user can't end up with a stored bad token. New endpoint
  `POST /v1/credentials/gitlab/test` (falls back to stored creds if
  body fields are empty) and client wrapper
  `test_gitlab_credentials(username, token)`.
- **Per-host `confirmed` flag.** `get_credentials_status()` now
  reports `github.confirmed` (derived: `connected AND app_installed`)
  and `gitlab.confirmed` (persisted; cleared on save, set on a
  successful Test). There is no longer a single "active host" — both
  hosts can be confirmed independently, and consumers (publish flow
  below, future sync flows) pick one based on context.
- **"Publish &lt;langcode&gt; data" button on the settings screen.**
  Visible only when `last_project()` resolves to a langcode whose
  project doesn't already have a remote; enabled when at least one
  host is `confirmed`. On click, single-confirmed hosts publish
  directly via `init_project(working_dir, remote_url, ...)`; both
  confirmed surfaces a small overlay so the user picks GitHub or
  GitLab. Mirrors the recorder's `do_publish` flow but moves it into
  the daemon UI, so any peer that exposes the gear can publish
  without owning the publish UI.
- **GitHub device flow no longer auto-fires on screen rebuild.**
  `GitHubConnectScreen.on_pre_enter` previously kicked the device
  flow on every entry, which meant a language-change rebuild — which
  clears + re-adds every screen — re-launched device flow even though
  the user was nowhere near the GitHub screen. The auto-start now
  lives on the explicit "Connect to GitHub" button via the new
  `SettingsScreen.connect_github()`, so language changes (and any
  other rebuild) leave the GitHub screen quiet.

### azt_collabd 0.16.0 + azt_collab_client 0.20.0 — sticky-bound server APK service + persistent scheduler jobs
- **Server APK lifetime fix.** The picker Activity now leaves the
  Python process running on Android instead of calling `App.stop()` /
  `sys.exit()`. A new sticky-bound service
  (`AZTServiceProviderhost`, `android/src/main/java/.../`) pins the
  host so `AZTCollabProvider.openFileDescriptor` can still serve the
  URI grant the picker just emitted. Pre-0.16.0 the server APK
  process exited as soon as the picker Activity finished, taking the
  provider with it and triggering Android's "depends on provider in
  dying proc" cascade SIGKILL of any peer that had received a
  `content://` URI from the picker.
- Service is sticky-bound (no foreground notification): peers get
  the bind-priority OOM hint while they're using the provider, and
  `START_STICKY` asks Android to recreate the service after a
  memory-pressure kill. Idle-stop policy (5 min of zero peers bound
  + zero provider activity) tears the service down so the design's
  "transient when idle, pinned while in use" intent is preserved.
  Manifest entry injected by `_inject_aztcollab_service` in
  `p4a_hook.py`, gated on `dist_name == 'aztcollab'`.
- **Scheduler jobs persisted to `$AZT_HOME/jobs.json`** so peer
  `poll_job(job_id)` calls survive a daemon respawn. `_store_job` and
  `_fire` write atomically on every state transition. New
  `scheduler.reconcile_on_startup()` runs from the loopback HTTP
  daemon entry (`server.run`) and the Android service entry
  (`server_apk/service.py`); marks any `PENDING` / `RUNNING` jobs
  found at startup as `DONE` + `JOB_INTERRUPTED` because their
  worker threads died with the previous process. Old `DONE`
  entries are GC'd past 1h at the same pass.
- New status code `JOB_INTERRUPTED` (`azt_collabd/status.py` and
  `azt_collab_client/status.py`, mirror) plus translation in
  `azt_collab_client/translate.py`. Peers should treat it identically
  to `SERVER_UNAVAILABLE`: transient, retryable.
- `MIN_CLIENT_VERSION` bumped to 0.20.0 — pre-0.20 clients don't have
  the `JOB_INTERRUPTED` translation and would surface the raw
  uppercase code in their UI.
- `MIN_SERVER_VERSION` bumped to 0.16.0 — pre-0.16 daemons don't
  persist jobs.json, so `poll_job` returns None for any job_id whose
  daemon has been respawned, indistinguishable from "never existed."
- Activity tracking added to `azt_collabd/android_cp/service.py`:
  `touch()`, `seconds_since_last_touch()`, `bound_client_count()`
  used by the service idle-stop loop. Every dispatch / openFile call
  bumps the touch timestamp.
- Picker app gains `on_pause` returning True so Kivy doesn't fight
  the missing GL surface after the Activity finishes.
- New `server_apk/test_install.py` (sibling of the existing adb-driven
  `test_install.sh`): 8-section desktop integration test for the
  kill-recovery flow — auto-spawn detection, jobs.json persistence,
  reconcile_on_startup, JOB_INTERRUPTED end-to-end. Run from the
  azt-collab repo root: `python server_apk/test_install.py`.

### azt_collab_client 0.19.2 — pick_project unbinds its activity-result handler after each call
- ``pick_project()`` registered a closure on
  ``android_activity.bind(on_activity_result=…)`` and never
  unbound it, so each invocation in a host session left a dangling
  handler that fired on every subsequent activity result. Logs
  showed N copies of ``[pick_project] _on_result …`` after N
  picks. Each closure wrote to its own long-since-stale
  ``holder`` so behaviour was correct for the most recent caller,
  but the JNI cost grew linearly with picks.
- New ``_unbind_handler`` helper called from inside ``_on_result``
  after ``done.set()`` (single-shot pattern) and from the timeout
  path so a much-later activity result for our request code can't
  write to a stale holder. Tracks ``bind_state['bound']`` to avoid
  unbinding a never-bound handler when ``_setup_on_ui`` failed
  early. Tolerates older Kivy / python-for-android versions that
  exposed ``bind`` without ``unbind``.

### azt_collab_client 0.19.1 — fix vanishing project list: defer ProjectPickerScreen.on_enter populate by one frame
- ``projects.json`` had the cloned project, the daemon's
  ``_h_list_projects`` would have returned it — but the picker
  never asked. ``ProjectPickerScreen.on_enter`` called
  ``_populate_projects`` synchronously, and Kivy >= 2.3 fires
  ``on_enter`` before KV-defined ids attach on the first screen
  entry. So ``self.ids.get('project_list')`` returned None, the
  populate function bailed silently, and the existing-projects
  list rendered empty — "cloned projects don't show up on next
  open". Same race the settings UI already worked around with
  ``Clock.schedule_once``; applied the same fix here.
- Added two diagnostic prints inside ``_populate_projects`` so
  any future bail (still-no-id even after the defer, or missing
  host ``list_projects`` method) surfaces in logcat instead of
  manifesting as a silent empty list.

### azt_collab_client 0.19.0 — suite-wide last-opened-project state (`recent.last_project`)
- New ``azt_collab_client.recent`` module with ``last_project()`` and
  ``set_last_project(langcode)``, persisted to
  ``$AZT_HOME/config.json`` under ``recent.last_langcode``. Re-exported
  from the package root.
- Same store as ``i18n``'s ``ui.language`` — no daemon RPC, just a
  file the client reads/writes; peers converge through the shared
  config without an explicit coordination channel. Recorder writes
  the langcode after every successful pick; the next peer launch
  (recorder, viewer, future apps) reads it at startup and lands on
  the same project.
- Resolve langcode → current path/URI via the existing
  ``open_project(langcode)`` (returns the daemon's authoritative
  ``Project`` record). ``recent.last_project()`` deliberately returns
  just the langcode, not a path — paths/URIs can shift across syncs;
  the langcode is stable.
- The "one store — suite-wide prefs AND state" rule generalises: the
  recorder's ``prefs['last_lift']`` was the second peer-private
  cache to fall under the rule (after ``prefs['ui_language']`` in
  0.16.0). Future cross-peer signals (last entry within project,
  contributor name) follow the same model.

### azt_collabd 0.14.5 — list_projects path/count diagnostic
- ``_h_list_projects`` now prints the resolved
  ``projects.json`` path it just read from plus the count and
  langcodes returned. Combined with the 0.14.4
  ``clone registered langcode=… → <path>`` print on the write
  side, the two log lines pin down whether the
  vanishing-projects bug is a write-vs-read path mismatch
  (different ``$AZT_HOME`` resolution between the two call
  sites) or a write-failed-silently issue. The actual on-disk
  content can be verified with ``adb shell run-as
  org.atoznback.aztcollab cat files/azt/projects.json``.

### azt_collabd 0.14.4 — inline langcode preview in clone popup; diagnostic prints for missing-after-relaunch projects
- Clone-URL popup gained an inline ``code: <derived>`` readout
  with an inline **change code** button right above the URL
  field. The readout updates live as the user types the URL;
  no separate confirmation step. Tapping **change code** swaps
  the readout for a small editable field — once the user takes
  control there's no auto-revert.
- Open-file flow uses the LIFT-filename-stem-derived langcode
  silently (no popup); user can rename later through whatever
  rename affordance lands.
- Diagnostic prints added in two spots so the user-reported
  "previously cloned projects don't show up on next open"
  actually points somewhere:
  - ``_clone_worker`` after ``projects.register``, prints the
    langcode and the resolved ``projects.json`` path. If the
    print appears, the registry write thinks it succeeded.
  - ``picker_app.list_projects`` (host method) prints how many
    projects came back from the daemon and which langcodes.
    If this prints 0 right after a successful clone, persistence
    or path-resolution is the issue (likely an ``$AZT_HOME``
    that differs between the clone-time process and the picker
    relaunch).

### azt_collabd 0.14.3 — clone accepts user-chosen langcode on input; rename_project endpoint
- ``POST /v1/projects/clone`` body gains optional ``langcode``;
  the picker collects an explicit value via the
  ``confirm_langcode_popup`` (client 0.18.2) before kicking the
  clone, so the project lands in ``projects.json`` keyed on the
  user's choice from the moment the daemon first sees it. No
  rename-after-the-fact in the registry. Empty ``langcode`` falls
  back to the daemon's auto-derivation from the LIFT filename /
  repo URL — matches the legacy desktop / scripted-call shape.
- ``_clone_worker`` gained an ``override_langcode`` kwarg; the
  registration step prefers it over ``derive_langcode``.
- New ``POST /v1/projects/<langcode>/rename`` endpoint
  (``_h_rename_project``) and ``projects.rename(old, new)``
  helper. Not used by the picker (which sets-on-create instead),
  but exposed for future flows that might let the user re-key a
  project after the fact (e.g., a settings-screen "rename
  project" affordance).

### azt_collabd 0.14.2 — clone job response carries canonical langcode
- Closes the azt-viewer 0.5.1 TODO ("picker should emit canonical
  langcode, not just leave the URI to be parsed"). The clone job
  response now includes ``langcode`` alongside ``lift_path`` —
  the same value the daemon just keyed the projects.json entry
  with on auto-register. ``_clone_worker`` captures it, the
  ``DONE`` job-state stash records it, ``_h_clone_status``
  passes it through.
- Source-of-truth chain (none of these need to derive from the
  URI on the peer side any more):
  - clone → daemon ``projects.derive_langcode`` → clone job →
    client ``clone_project`` returns ``langcode`` → picker stamps
    Intent extra.
  - open-file → ``register_project`` returns ``Project.langcode``
    → picker stamps Intent extra.
  - existing-project tap → ``picker.py`` populates the button
    with ``btn.langcode = projects_list_entry.langcode`` →
    ``load_lift(path, langcode)`` → picker stamps Intent extra.
  - template flow → user-typed BCP-47 (``_pending_vernlang``)
    already stamps the Intent extra.
- Backward-compatible. Old clients ignore the extra ``langcode``
  field on ``_h_clone_status`` responses; new clients hitting old
  daemons see ``langcode == ''`` and the peer's URI-parse
  fallback (defence-in-depth) still kicks in.

### azt_collab_client 0.18.3 — clone popup: only one input field active at a time
- ``clone_url_popup`` reworked so the URL field and the code field
  are mutually exclusive — only one is enabled at any moment, so
  the on-screen keyboard never argues with itself between two
  text inputs.
  - Mode A (default): code-preview row shows ``code: <derived> [change code]``;
    URL field is the active input.
  - Mode B (after tapping **change code**): code-preview row swaps
    in an editable code field with an ``[OK]`` button; the URL
    field is set to ``disabled=True`` (grays out, displays the
    typed URL, no input focus).
  - Tapping **OK** commits the typed code, re-enables the URL
    field, and swaps Mode A back in. Empty typed code clears the
    override so URL→code syncing resumes; non-empty pins the
    user's value through subsequent URL edits.
- Submit (Clone) works in either mode: takes the live code input
  in Mode B, the saved override in Mode A (post-OK), or the
  current URL-derived value otherwise. URL still required.

### azt_collab_client 0.18.2 — confirm-langcode popup: set on creation, not afterwards
- New ``ui.popups.confirm_langcode_popup(initial, on_submit)``:
  shows the auto-derived langcode in an editable field, asks the
  user to confirm or correct it. ``on_submit(chosen)`` fires on
  Confirm (and on Cancel with the original ``initial``, so the
  flow always resolves). Re-exported from ``azt_collab_client.ui``.
- ``picker_app.clone_dialog`` now derives a tentative langcode
  from the URL repo basename, runs ``confirm_langcode_popup``
  immediately after the URL submit, and only kicks the clone
  once the user confirms — passes the chosen value to
  ``clone_project(url, dest, langcode=chosen)``. The daemon
  registers under that exact key (no post-hoc rename). Helper
  ``_tentative_langcode_from_url`` mirrors the daemon's
  derivation order without requiring a filesystem path.
- ``picker_app.open_file._on_chosen`` runs
  ``confirm_langcode_popup`` after the file is picked but
  before ``register_project``; the chosen value goes to the
  registration call directly. Helper
  ``_tentative_langcode_from_lift`` strips the ``.lift``
  extension off the basename for the prefill.
- ``clone_project()`` / ``clone_project_start()`` accept an
  optional ``langcode=''`` kwarg routed into the request body;
  empty preserves the legacy auto-derivation behaviour for
  desktop scripted callers.
- New ``rename_project(old_langcode, new_langcode)`` wrapper for
  the daemon's ``/v1/projects/<langcode>/rename`` endpoint.
  Currently unused by the picker (set-on-create supersedes it),
  but exposed in ``__all__`` for peer apps that want to surface
  a "rename this project" affordance later.

### azt_collab_client 0.18.1 — clone_project carries langcode; project-list buttons stash langcode for load_lift
- ``clone_project()`` now returns ``langcode`` alongside
  ``lift_path`` / ``result`` / ``error`` on the success branch
  (DONE state). Daemon-side companion in 0.14.2.
- ``picker.py`` existing-project list now stores the project's
  canonical langcode on each button at populate time (the
  ``name`` half of the host's ``list_projects()`` tuple is already
  the langcode by contract) and passes it through on tap:
  ``app.load_lift(b.lift_path, getattr(b, 'langcode', ''))``. Host
  ``load_lift`` signature gains an optional ``langcode``
  parameter; default keeps existing single-arg callers working.

### azt_collab_client 0.18.0 — MediaHandle + audio_uri_for / image_uri_for
- New ``audio_uri_for(lift_path_or_uri, basename)`` and
  ``image_uri_for(lift_path_or_uri, basename)`` composer helpers.
  Given the picker-emitted LIFT path/URI plus a basename, return
  the sibling resource's URI (on Android-content URIs) or
  filesystem path (desktop) — so callers stay agnostic about the
  path/URI distinction. URI form composes
  ``content://<auth>/<lang>/{audio|images}/<basename>``, mirroring
  the daemon's ``_resolve_path`` whitelist. Filesystem form is
  ``os.path.join(os.path.dirname(lift_path), {audio|images}, basename)``.
- New ``MediaHandle(path_or_uri, kind='audio'|'image')`` —
  ``LiftHandle`` subclass with a ``kind`` for log lines / error
  messages, and a write-mode gate: ``open_write()`` on
  ``kind='image'`` raises ``PermissionError`` (images are
  read-only from peers; the daemon owns image additions).
- Re-exported from the package root: ``from azt_collab_client
  import MediaHandle, audio_uri_for, image_uri_for``.
- Together with ``LiftHandle`` (0.17.0), this is the full Tier 3
  cross-package toolkit the recorder migration documented in
  ``CLAUDE.md`` needs to land audio recording and image rendering
  on the new Android server-APK model.

### azt_collab_client 0.17.1 — client-first asset model: new icon_path helper, gear bundled
- New ``azt_collab_client.ui.icons`` module with public
  ``icon_path(name)`` — returns the absolute path to a bundled icon
  under ``azt_collab_client/ui/assets/icons/<name>.png`` (canonical
  location), falling back to ``assets/<name>.png`` for the legacy
  flat layout where ``gear.png`` currently lives. Returns ``''`` if
  the asset isn't bundled. Re-exported from
  ``azt_collab_client.ui.icon_path``.
- ``picker.py`` now resolves its default gear icon through
  ``icon_path('gear')`` (was a private ``_BUNDLED_GEAR`` constant
  with the same effect) so the discovery seam is reused for the
  next batch of shared icons.
- ``CLAUDE.md`` UI section documents the **client-first asset model**:
  shared-shape assets (gear, sync, share, future back/close glyphs)
  default to ``azt_collab_client/ui/assets/icons/`` so sister apps
  inherit them for free; peer-specific icons (recorder microphone /
  redo / app-icon variants) stay in the peer; existing recorder KV
  references with relative ``icons/<name>.png`` paths still work in
  the recorder's own cwd and don't need migrating.
- **Asset migration done:** ``sync_dark`` / ``sync_light`` /
  ``share_dark`` / ``share_light`` / ``gear_dark`` / ``gear_light``
  copied from ``azt_recorder/icons/`` into
  ``azt_collab_client/ui/assets/icons/``. ``icon_path('sync_dark')``
  etc. now resolve to the bundled package paths; the next sister-app
  peer (viewer, future clients) gets them with zero per-app work.
  The recorder's existing relative ``icons/<name>.png`` references
  still resolve in its own cwd, so this change is non-breaking;
  recorder-side migration to ``icon_path()`` can be opportunistic.

### azt_collab_client 0.17.0 — LiftHandle: cross-package LIFT-file access for peer apps
- New ``azt_collab_client.lift_io`` module exporting ``LiftHandle``
  and ``is_content_uri``. ``LiftHandle(path_or_uri).open_read()`` /
  ``.open_write()`` returns a binary file-like usable with
  ``ElementTree.parse`` / ``ElementTree.write`` regardless of
  whether the picker's emitted ``path`` is a filesystem path
  (desktop) or a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI (Android, new model). On the URI branch, opens via
  ``ContentResolver.openFileDescriptor`` and ``os.fdopen`` on the
  detached FD; the file owns the FD lifetime (close-on-exit
  through the context-manager protocol).
- Re-exported from the package root: ``from azt_collab_client import
  LiftHandle, is_content_uri``. Added to ``__all__``.
- **No caching layer** — every read/write hits the daemon's
  canonical copy through the provider. Lost-update protection
  relies on the daemon's serialization. The new
  ``azt_collab_client/CLAUDE.md`` "LIFT-file access" section
  spells out the migration checklist for peers (recorder first,
  viewer next): replace every ``open(lift_path)`` with
  ``LiftHandle(p).open_read() / .open_write()``; do not introduce
  a peer-side cache; do not compute sibling paths via
  ``os.path.dirname`` on a URI. Also documents the patterns NOT
  to use.

### azt_collab_client 0.16.0 — single source of truth for UI language; new public display_name + scan_catalog_languages
- ``set_language(lang)`` no longer takes a ``persist`` keyword argument.
  There is no transient mode any more — one preference, one store
  (``$AZT_HOME/config.json :: ui.language``), sticks everywhere until
  the next change. Internal apply-without-persist behaviour is now a
  private ``_apply(lang)`` used only by the auto-init-on-import path.
  Breaking change for callers that passed ``persist=False`` to apply a
  preference without rewriting the file; the new pattern is just
  ``set_language(language_pref())`` (idempotent re-write).
- New public ``i18n.display_name(code)`` — single source of truth for
  the language-code → human-name table. Peers that previously kept a
  parallel ``_DISPLAY_NAMES`` dict (the recorder's ``i18n.py`` did)
  now import this instead, eliminating the drift risk.
- New public ``i18n.scan_catalog_languages(locale_dir, domain)`` —
  walks ``<locale_dir>/<lang>/LC_MESSAGES/<domain>.{po,mo}`` and
  returns ``[(code, display_name), ...]``. Both the client's own
  ``available_languages()`` and the recorder's wrapper now share this
  shape, so peer catalogs and the client catalog enumerate
  identically.
- Updated callers: ``azt_collabd/ui/picker_app.py`` (build-time apply
  + mtime watcher) drop ``persist=False``; both calls just write the
  same value back, harmless because the mtime watcher's
  ``persisted == current_language()`` short-circuit prevents loops.

### azt_collabd 0.14.0 — content:// URIs across the picker boundary; clone auto-registers; MIN_CLIENT_VERSION → 0.17.0
- The picker (when running in the standalone server APK on Android)
  now emits a ``content://org.atoznback.aztcollab/<lang>/<file>.lift``
  URI from ``_emit_and_quit``, instead of an absolute filesystem
  path inside the server APK's private ``filesDir``. The Intent
  carries the URI on its ``data`` field and adds
  ``FLAG_GRANT_READ_URI_PERMISSION | FLAG_GRANT_WRITE_URI_PERMISSION``
  so the calling peer can open the URI via
  ``ContentResolver.openFileDescriptor`` for the result delivery's
  lifetime.
- This removes a cross-package access bug (recorder's
  ``ElementTree.parse(path)`` raised ``[Errno 2]`` on an absolute
  path inside ``/data/user/0/org.atoznback.aztcollab/files/``,
  which the recorder's UID can't read). The provider's existing
  ``openFile`` callback (``_resolve_path`` under
  ``$AZT_HOME/projects/``) handles the URIs without changes —
  except for a leading-slash strip on ``Uri.getPath()`` so the
  path composes correctly. The single canonical copy in the
  daemon's ``$AZT_HOME`` stays the source of truth; peers don't
  cache.
- Successful clones now auto-register via
  ``projects.register(langcode, dest_dir, lift_path, remote_url)``.
  Previously a clone left the working tree on disk but no entry in
  ``projects.json``, so subsequent ``list_projects()`` /
  ``sync_project(langcode, …)`` calls couldn't find it. Failure
  is logged but doesn't fail the clone job — caller can re-register
  explicitly.
- ``MIN_CLIENT_VERSION`` raised to ``0.17.0`` because the URI shape
  is a hard contract: a peer bundling a pre-LiftHandle client would
  try to ``open()`` the URI as a path and crash on the spot. Old
  peers now get ``client_too_old`` from ``check_server_compat()``
  at startup with a clear "update this app" prompt.

### azt_collabd 0.13.21 — Bundle-based result extras to fix cross-package no_path loss
- Logcat showed the picker emitting a real lift_path
  (``/data/user/0/.../foo.lift``) via ``_emit_and_quit``, but the
  calling recorder still reported ``no_path`` — meaning the
  Intent's ``path`` extra was being lost across Android's IPC
  delivery to the peer process.
- Two likely culprits, both addressed by the same patch:
  1. ``Intent()`` with no action has been observed to drop extras
     on cross-package result delivery in some Android versions.
     The result Intent now carries the same action the recorder
     used for the request (``org.atoznback.aztcollab.PICK_PROJECT``).
  2. ``Intent.putExtra(String, String)`` is one of ~15 overloads;
     jnius's overload resolution can silently bind to a non-String
     overload when both args are CPython strings, leaving
     ``getStringExtra`` returning null on the peer side. Switched
     to the explicit, single-signature
     ``Bundle().putString('path', ...)`` →
     ``Intent.putExtras(Bundle)``.
- Added a diagnostic round-trip: the picker now reads the path
  back out of the result Intent (via ``getStringExtra``) right
  before ``setResult`` and prints it to logcat. If the verify
  print shows the path correctly but the recorder still gets
  ``no_path``, the loss is in Android's binder layer (genuinely
  rare); if the verify print is empty, the typed-Bundle approach
  also failed and we know to look further upstream.

### azt_collabd 0.13.20 — clone-flow diagnostic prints
- Added prints (to stderr → logcat ``python`` tag) at every step
  of the clone flow so the next ``no_path`` reproduction tells us
  exactly where the empty-path emission originates: ``clone worker
  starting``, ``clone returned`` (with ok / lift_path / error), the
  exception path, ``_after_clone_ok``, ``_after_clone_fail`` (with
  result codes), and ``load_lift`` (existing-project tap).
- Fixed a duplicate ``load_lift`` definition: a diagnostic-printing
  version was added without removing the original, and Python's
  last-def-wins on class bodies meant the diagnostic version was
  silently shadowed. Removed the second definition.

### azt_collabd 0.13.19 — debug bump for no_path triage
- Version-only bump so the user can verify on the picker's bottom
  strip (``server 0.13.19``) that the deployed build includes the
  ``_emit_and_quit`` empty-path guard from 0.13.15 and the
  Connect/Disconnect colour reactivity from 0.13.18.

### azt_collabd 0.13.18 — Connect/Disconnect button colour tracks connection state
- The four host action buttons (Connect / Disconnect GitHub,
  Set GitLab credentials / Disconnect GitLab) used to be statically
  green for Connect and dim for Disconnect. The visually-prominent
  button now matches the user's likely next action: Connect is
  green when not connected, Disconnect is green when connected.
  Dim button stays clickable so reconnect-to-refresh-tokens and
  similar flows still work — colour is a hint, not a gate.
- The colour swap is driven by ``refresh()`` reading
  ``credentials_status``, so the same round-trip that updates the
  status block also updates these buttons. Re-renders every time
  the screen is entered or the user taps Refresh Status.

### azt_collabd 0.13.17 — Refresh button rename + reposition
- ``Refresh`` button renamed to ``Refresh Status`` (more honest
  about what it does — it only re-pulls the credential / online
  read-out, doesn't do a sync) and moved to sit directly under the
  status block. Affordance for "I changed something in another
  window, pull the updated state" is now immediately adjacent to
  the data it refreshes, with no spacer in between.

### azt_collabd 0.13.16 — settings layout: status to the bottom, host rows compacted
- ``SettingsScreen`` reorder: actionable rows (interface language,
  contributor name, GitHub/GitLab connect+disconnect) at the top;
  the read-only ``Status`` block moved to the bottom. Users land
  here to do something, not to inspect — surfacing the controls
  first matches the visit pattern.
- GitHub and GitLab each collapsed from "section header + Connect
  row + Disconnect row" (3 rows of vertical real estate) into a
  single row with ``Connect…`` and ``Disconnect`` side-by-side.
  Brand name is implicit in the button text. About 100dp of
  vertical space recovered.

### azt_collabd 0.13.15 — server-owned contributor; auto-start device flow; loading-overlay wrap; empty-path guard
- New server-owned contributor field. ``store.get_contributor()`` /
  ``set_contributor(name)`` persist a display name to
  ``$AZT_HOME/config.json :: collab.contributor`` (sibling to
  ``ui.language`` — config, not credentials). New endpoints
  ``GET /v1/config/contributor`` and ``POST /v1/config/contributor``;
  client wrappers ``azt_collab_client.get_contributor`` and
  ``set_contributor``. ``store.get_status()`` now includes
  ``contributor`` so the settings UI gets it on the existing
  credentials-status round-trip. ``_h_project_sync``,
  ``_h_init_project``, ``_h_project_sync_async``, and
  ``scheduler._run_sync`` all route through new
  ``store.resolve_contributor(passed)`` which prefers the caller's
  explicit value, then the stored display name, then the
  ``'Recorder'`` fallback. Peers can stop carrying their own "Your
  name" preference; the suite has one source of truth on the server.
- ``SettingsScreen`` got a "Your name (appears in commits)"
  ``ThemedInput`` field with a transient "Saved." confirmation; the
  field auto-saves on focus loss. Refresh repopulates it from the
  server only when the user isn't actively editing.
- ``GitHubConnectScreen`` auto-starts the device flow on screen
  entry — no more "tap Begin to start" friction. The Begin button
  stays around as a Retry surface, re-enabled by the worker's
  failure path.
- ``picker_app._show_loading_overlay`` Label now wraps on width
  (same fix as ``_show_error``); long ``Cloning <url>...``
  messages no longer overflow both edges.
- ``picker_app._emit_and_quit`` refuses to emit an empty path. On
  Android an empty path lands at the peer's
  ``pick_project_android`` handler as ``RESULT_OK`` with no extra
  and surfaces as ``no_path``. If anything upstream tries it now,
  we log a stack trace to logcat and show an "Internal error:
  tried to return an empty path" modal so the user can pick again
  instead of bouncing back to the recorder with a cryptic failure.

### azt_collabd 0.13.14 — error-modal text wraps; auto-copy GitHub user_code
- ``picker_app._show_error`` Label was constructed with
  ``text_size=(None, None)``, which disables wrapping — long
  messages overflowed both edges of the modal. Now binds
  ``text_size`` to the Label's width so text wraps inside the
  modal panel; height stays free so the texture grows vertically
  as needed (modal's fixed height clips the bottom for very long
  messages, acceptable for typical 2–3-line errors).
- GitHubConnectScreen used to auto-copy the device-flow
  ``user_code`` to the clipboard so users could paste it into the
  GitHub device page without an extra tap; this regressed during
  the settings UI restyle. Restored the auto-copy and append the
  existing ``(code copied)`` translated suffix to the on-screen
  message when the copy succeeds (silently no-ops if Clipboard is
  unavailable, e.g. on a headless device).

### azt_collabd 0.13.13 — Android-aware ``azt_home()`` (and azt_collab_client mirror)
- ``[Errno 13] Permission denied: '/data/.local/share/azt/...'`` on
  every file op (template download, sync, etc.). p4a does not set
  ``$HOME``, so ``os.path.expanduser('~')`` resolved to ``/data`` —
  the Android system-data root, owned by ``root``, not writable by
  the app's UID. ``azt_home()`` then composed a path no app can
  write to.
- ``paths.py`` (both ``azt_collabd/`` and ``azt_collab_client/`` —
  duplicated by design) gained a ``_android_files_dir()`` helper
  that calls ``PythonActivity.mActivity.getFilesDir()`` via jnius
  and returns the app's private writable filesDir
  (``/data/user/0/<pkg>/files``). ``azt_home()`` checks that first
  on Android (after ``$AZT_HOME``, before XDG fallbacks). Desktop
  unchanged. The ``$AZT_HOME`` env-var override still wins for
  test rigs.

### azt_collabd 0.13.12 — settings Back button uses a glyph CharisSIL has
- ``SettingsScreen``'s "← Back" button used U+2190 (LEFTWARDS
  ARROW) which isn't in the CharisSIL glyph table — rendered as
  tofu under the project's default linguistic font. Swapped for
  ``«`` (U+00AB, left guillemet): present in every Latin font,
  reads as a back-pointer, and is the natural French equivalent
  too.

### azt_collabd 0.13.11 — preserve ``back_to`` across language-toggle screen rebuild
- ``_set_ui_language`` (settings UI) and ``_check_language_change``
  (picker subprocess) rebuilt the ScreenManager by recreating each
  screen with ``cls(name=name)``. That recipe loses any property
  the parent KV rule set on the *instance* (not the class) —
  notably ``back_to: 'picker'`` on ``SettingsScreen`` in
  ``picker_app._PickerRoot``. Symptom: the in-screen "← Back"
  button vanished the first time the user toggled language and
  didn't come back when toggling to English.
- Both rebuild loops now capture ``back_to`` per-screen before the
  ``clear_widgets`` and re-apply after instantiation. Generic
  enough to extend to other instance-level properties later.

### azt_collabd 0.13.10 — Android back button on picker subscreens
- Hardware back / gesture on Android does not flow through
  ``App.on_request_close`` (which only fires for the desktop X
  button). It surfaces as ``key 27`` on ``Window.on_keyboard``.
  Without an explicit binding, Kivy's default for an unhandled key
  is ``App.stop`` — so back from settings / github / gitlab /
  langpicker was closing the picker activity entirely.
- ``PickerApp.on_start`` now binds ``Window.on_keyboard`` to a new
  ``_on_back_button`` handler that delegates to ``_navigate_back``
  (extracted from the existing ``on_request_close`` logic). On a
  non-picker screen, back navigates to the screen's ``back_to``
  property (or ``'picker'`` by default); on the picker itself, back
  falls through to the normal ``RESULT_CANCELED + finish()`` exit.
  Same screen-pop dance the recorder uses.

### azt_collabd 0.13.9 — full traceback on template-download failures
- ``_h_create_project_from_template`` was masking the original
  failure type by catching ``Exception`` and returning only
  ``str(ex)``. On the device a ``PermissionError`` surfaced as
  ``provider HTTP 500: [Errno 13] Permission denied`` with no path
  or call site. Now logs the full traceback to stderr/logcat
  (``adb logcat | grep -i from_template``) and includes
  ``traceback`` and ``ExceptionType`` in the response body so the
  caller (picker / recorder) can surface them.
- Confirms in code comments that the template download is an
  anonymous public HTTPS GET — no GitHub credentials consulted.

### azt_collabd 0.13.8 — drop "Active host" toggle from settings UI
- ``SettingsScreen`` no longer renders the "Active host" SectionLabel
  + GitHub/GitLab two-button row. URL-based credential routing
  (``store.get_sync_credentials(url)`` → ``host_for_url(url)``)
  has handled every common case since 0.12.0; the toggle was
  vestigial. ``choose_host`` method dropped from ``SettingsScreen``.
  ``set_collab_host`` server endpoint and client wrapper stay around
  for wire compat (peers still calling them are safe; the value
  silently affects only the self-hosted/unknown-URL fallback path
  through ``get_collab_host()``).
- The eventual "Publish" flow for new local-only projects will
  pick credentials by inspecting which hosts have stored creds,
  prompting the user only when more than one is configured —
  rather than reading a global "active host" preference. Captured
  here so future-me doesn't reintroduce the toggle.

### azt_collabd 0.13.7 — locale files packaged in server APK
- ``server_apk/buildozer.spec`` ``source.include_exts`` was
  ``py,xml,gz,png``, silently dropping the ``.po``/``.mo`` files
  under ``azt_collab_client/locales/`` at packaging time. On the
  device, ``available_languages()`` walked an empty locale tree and
  the settings UI's language toggle only offered English regardless
  of which catalogs lived in the source tree. Added ``po,mo`` to
  the extension list. Pre-compile any language's ``.mo`` before
  rebuilding so the catalog ships pre-baked (faster first paint;
  also dodges any APK-readonly issue with the runtime
  ``_ensure_mo``):
  ``python -c "from azt_collab_client.i18n import _ensure_mo;
  _ensure_mo('fr')"``.

### azt_collabd 0.13.6 — typing_extensions in APK requirements; BodyLabel recursion fixed at class level
- Added ``typing_extensions`` to the server APK's ``requirements``
  in ``server_apk/buildozer.spec``. dulwich (and a few of its
  transitive imports) reach for ``typing_extensions`` at runtime;
  on Android it isn't pulled in by default. Previously a clone
  attempt would fail with ``ImportError: no module named
  typing_extensions`` at the moment dulwich tried to do its first
  network operation. Adding the recipe to requirements puts it on
  the APK's PYTHONPATH. **Requires a clean build**
  (``buildozer android clean && buildozer android debug deploy``)
  for p4a to pick up the new recipe.
- Promoted the ``text_size: self.width, None`` fix from
  per-instance overrides on three ``BodyLabel`` uses to the
  ``<BodyLabel@Label>`` class rule itself. Any ``BodyLabel`` whose
  ``height: self.texture_size[1] + dp(8)`` would otherwise loop
  with ``text_size: self.size`` is now safe by default. The earlier
  per-instance overrides remain (redundant but harmless).

### azt_collabd 0.13.5 — picker version-probe diagnostics
- ``picker_app._probe_server_version`` now surfaces *why* the probe
  failed when it can't show a real server version. Instead of a bare
  ``server ?``, the bottom strip renders one of
  ``server ? (server_unreachable)`` /
  ``server ? (server_too_old)`` /
  ``server ? (client_too_old)`` /
  ``server ? (<ExceptionType>: ...)``. Distinguishes transport down
  vs. version-handshake reject vs. RPC exception without needing
  ``adb logcat``. Also prints a one-line diagnostic to stderr/logcat
  so the post-mortem is in both places.

### azt_collabd 0.13.4 — debug version bump
- No code change. Version-only bump so a freshly-rebuilt server APK
  reports a different ``__version__`` from the previous build,
  letting the user verify on the picker's bottom strip
  (``client X · server Y``) that the device is actually running the
  new build vs a cached install.

### azt_collabd 0.13.3 — picker shows both versions, auth-fallback for clone failures
- Picker bottom strip now shows ``client X · server Y``. The server
  half is fetched off the UI thread via
  ``check_server_compat()``; renders ``server ?`` if the daemon is
  unreachable. Was: client only.
- ``_after_clone_fail`` got a fallback path: when the daemon's
  worker didn't run far enough to attach ``CLONE_AUTH_REQUIRED``
  (e.g. the clone-job kickoff itself failed and the result is None)
  but the error string smells like auth (401 / 403 / 404 /
  unauthorized / forbidden / not found / authentication /
  credential), the auth-prompt modal still appears with the **Open
  settings** button. Same heuristic the daemon uses, mirrored
  client-side for the result-is-None case.
- Auth-modal "Open settings" button now calls ``self.go_config()``
  (in-process screen swap) instead of the removed
  ``open_server_ui`` import. The Android Intent dance is gone from
  this flow entirely.

### azt_collabd 0.13.2 — picker hosts settings screens in-process
- Picker's gear used to call ``azt_collab_client.open_server_ui()``,
  which on Android fires ``getLaunchIntentForPackage`` on the server
  APK. Because the server APK has a single ``PythonActivity`` already
  running the picker, Android collapsed the task back to the calling
  peer (the recorder) instead of switching to settings — there was
  no path forward from the picker.
- ``azt_collabd/ui/app.py`` now exposes a top-level
  ``register_kv(font_name)`` (idempotent) that loads the settings/
  GitHub/GitLab KV. ``CollabUIApp.build`` calls it; the picker_app
  also calls it before its own KV so all class rules are in scope.
- ``picker_app._PickerRoot`` ScreenManager now carries
  ``SettingsScreen`` (with ``back_to: 'picker'``),
  ``GitHubConnectScreen``, and ``GitLabFormScreen`` alongside the
  existing ``ProjectPickerScreen`` / ``LangPickerScreen``. ``go_config()``
  is now ``self.sm.current = 'settings'`` — no Intent, no subprocess.
  Same code path on desktop and Android.
- New ``go(name)`` method on ``PickerApp`` mirrors ``CollabUIApp.go``
  so the existing settings-side KV (``app.go('github')``,
  ``app.go('gitlab')``, ``app.go('settings')``) just works in both
  hosts.
- New ``back_to`` ``StringProperty`` on ``SettingsScreen``. When set
  (the picker_app ``_PickerRoot`` KV sets it to ``'picker'``) the
  screen renders an additional **← Back** ``NavBtn`` at the top of
  the layout. Hidden / disabled in the standalone settings host
  where back has no meaning. The Android back gesture / window-close
  on non-picker screens is also intercepted by ``on_request_close``
  to navigate back instead of exiting.
- Known limitation: a peer calling ``open_server_ui()`` *while a
  picker is already up* still hits the Android launch-flag bug
  (settings doesn't appear; task may collapse to the peer). Rare in
  practice and not worth a separate ``SettingsActivity`` declaration
  yet — tracked as future work.

### azt_collabd 0.13.1 — settings UI Clock-iteration warning fix
- ``BodyLabel`` instances that combined ``text_size: self.size``
  (inherited) with ``height: self.texture_size[1] + dp(8)`` were
  forming a feedback loop on Android: texture_update changes height
  → parent BoxLayout do_layout → child resize → text_size changes →
  texture_update fires. Tolerable before; pushed past Kivy's
  per-frame Clock iteration limit by the new language-toggle row
  and the wrapping of every settings-UI string in ``_(...)``.
  Surgical fix on the three offending BodyLabels (status_label,
  gh_message, the gitlab-form intro): override
  ``text_size: self.width, None`` so the wrap width is bound but
  height flows from content alone, breaking the cycle.

### azt_collabd 0.13.0 — settings UI translatable, language toggle, picker live retranslation
- ``SettingsScreen`` gained an **Interface language** row at the top
  with one button per ``azt_collab_client.i18n.available_languages()``.
  Selecting a language calls ``i18n.set_language(code)`` (which
  persists to ``$AZT_HOME/config.json`` under ``ui.language``) and
  rebuilds every screen in the manager so KV ``text: _('...')``
  bindings re-evaluate against the new catalog. Same dance the
  recorder's ``ConfigScreen`` uses.
- Every visible string in ``azt_collabd/ui/app.py`` (Settings, GitHub
  device-flow, GitLab form) is now wrapped in ``_(...)``. KV imports
  ``_ azt_collab_client.translate.tr`` so subsequent
  ``set_translator``/language switches take effect.
- ``picker_app.py`` watches ``$AZT_HOME/config.json`` mtime once a
  second (Clock interval). When the persisted language changes — for
  example because the user just toggled it in a settings subprocess
  opened from the gear — the picker rebuilds its screens in place.
  The user sees the picker live-retranslate without restart.
- Apply persisted language at picker / settings startup so first
  paint is in the right language.

### azt_collabd 0.12.1 — picker gear wired to settings, both versions on settings page
- Standalone picker (``python -m azt_collabd projects``) now shows
  the settings gear in the top-right and wires it to the daemon's
  settings UI via ``open_server_ui()`` instead of the previous no-op
  stub. Rationale: first-time users land on the picker and need a
  visible path to authentication; previously they had to fail a
  clone before the auth-prompt modal offered one.
- Settings UI (``python -m azt_collabd ui``) now displays both the
  client and server versions in the bottom version strip:
  ``client 0.14.1  ·  server 0.12.1`` — used to show only the
  daemon version. The settings UI subprocess imports
  ``azt_collab_client`` for ``__version__``.
- Both version labels (settings + picker) bumped from
  ``font_size: sp(11) / color: T.TEXT_FAINT`` to
  ``sp(13) / T.TEXT_DIM`` so they're legible without straining.

### azt_collabd 0.12.0 — URL-based credential routing, CLONE_AUTH_REQUIRED, MIN_CLIENT_VERSION handshake
- New ``azt_collabd.MIN_CLIENT_VERSION = '0.14.0'`` — floor on the
  ``azt_collab_client`` version this daemon will talk to. ``/v1/health``
  now publishes ``min_client_version`` alongside ``version``. Symmetric
  to the existing client-side ``MIN_SERVER_VERSION``: when a peer ships
  a too-old client, the peer's startup handshake gets ``client_too_old``
  and can prompt the user to update. Bump this constant in lockstep
  with any wire-format addition that older clients can't decode (e.g.
  the new ``CLONE_AUTH_REQUIRED`` status added in this release).
- ``store.get_sync_credentials()`` now takes an optional remote URL and
  picks credentials by host (``github.com`` → GitHub creds,
  ``gitlab.com`` → GitLab creds), falling back to the user's saved
  ``collab_host`` only when the URL is unrecognized (self-hosted /
  empty). All call sites — ``_h_init_project``, ``_h_clone_project``,
  ``_h_project_sync`` in ``server.py`` and ``_run_sync`` in
  ``scheduler.py`` — pass the remote URL. Symptom this fixes: the user
  picks a LIFT project without first visiting Settings; the daemon
  used to send GitHub creds to a GitLab remote (or vice-versa) just
  because ``collab_host`` was the wrong value.
- New helper ``store.host_for_url(url)`` exposes the URL → host
  classifier.
- New status code ``CLONE_AUTH_REQUIRED`` (with ``host`` param). The
  clone worker now appends it after final failure when either (a) no
  token was available for the URL's host, or (b) the dulwich error
  contains 401/403/404/auth keywords. The picker UI uses this to
  branch into an auth-prompt modal instead of a generic error.
- The auth-shaped retry already in ``_clone_worker`` was extracted to
  ``_clone_error_looks_like_auth(result)`` and now also recognises
  ``not found`` / 404 (private-repo case) — previously only matched
  ``credential`` / ``auth``, so a 404-bearing failure skipped the
  anonymous retry.

### azt_collab_client 0.15.2 — Android ContentProvider path delivery fix
- **Critical bug fix.** The Android transport
  (``azt_collab_client/transports/android_cp.py``) built a URI like
  ``content://<authority><path>`` and called
  ``ContentResolver.call(uri, method, None, extras)``. But the
  ``call(Uri, method, arg, extras)`` overload only delivers
  ``method``, ``arg``, and ``extras`` to
  ``ContentProvider.call(method, arg, extras)`` — the URI's path is
  consumed by provider routing and never reaches the dispatch.
- AZTCollabProvider.java reads ``arg`` as the path
  (``cb.dispatch(method, arg != null ? arg : "", body)``), so on the
  daemon side every RPC was being dispatched with ``path=""``,
  producing ``{ok: False, error: 'not_found'}``. User-visible
  symptom: every clone / list / sync / credential RPC silently
  routed to the dispatcher's catch-all 404 branch.
- Fix: pass the dispatch path as ``arg`` instead of None. The URI
  shrinks to just the authority (no path component) since it's only
  used for provider routing now. One-line change at the call site.
- This was a long-standing bug that likely went unnoticed because
  legacy peers symlinked ``azt_collabd`` and used the loopback
  transport (Python interpreter in-process). Peers on the new
  ContentProvider-only model would have hit it on every non-ping
  call.

### azt_collab_client 0.15.1 — picker gear icon bundled in package
- Picker KV used to reference the gear icon as a relative
  ``'icons/gear.png'`` path, which only resolves when the host's
  cwd happens to contain that file (worked from the recorder repo
  root, broke everywhere else — most visibly in the standalone
  picker subprocess, where Kivy fell back to its missing-image
  texture and the gear rendered as a white square).
- The icon is now an absolute path computed from the package
  location: ``azt_collab_client/ui/assets/gear.png``. The KV
  template injects it at ``register_kv`` time alongside ``font_name``.
- ``register_kv`` (a.k.a. ``register_picker_kv``) gained an optional
  ``gear_icon=`` kwarg so hosts that want a custom icon can pass
  one explicitly (the recorder still ships its own at
  ``azt_recorder/icons/gear.png``); default falls back to the
  package-bundled file.
- **Important**: the binary file
  ``azt_collab_client/ui/assets/gear.png`` is not committed by this
  change; copy from any peer that already has one
  (e.g. ``cp /home/kentr/bin/AZT/azt_recorder/icons/gear.png
  azt_collab_client/ui/assets/gear.png``).

### azt_collab_client 0.15.0 — own i18n domain (azt_collab_client.po), pure-Python msgfmt, fallback chain in translate.tr
- New module ``azt_collab_client.i18n`` owns gettext domain
  ``azt_collab_client``. Public API:
  ``set_language(lang, persist=True)``, ``current_language()``,
  ``available_languages()``, ``language_pref()``, ``_(msg)``,
  ``gettext_translation()``. Persists the active language to
  ``$AZT_HOME/config.json`` under ``ui.language``; auto-applies that
  preference at import so all suite subprocesses converge on the same
  language without a coordination channel.
- New locale tree
  ``azt_collab_client/locales/<lang>/LC_MESSAGES/azt_collab_client.po``
  with French translations of all client-owned strings: picker UI,
  langpicker, popups, ``translate.py`` status messages (the full
  ``S.*`` set), and the settings-UI strings now owned by the client.
- ``i18n.py`` ships a pure-Python PO→MO compiler (msgfmt-lite — single
  magic, sorted msgid array, two parallel offset tables packed via
  ``struct``). Runs lazily on first ``set_language`` whenever the
  ``.mo`` is missing or older than the ``.po``. So peers that ship
  only the ``.po`` (or contributors editing translations in-place) do
  not need a build-time ``msgfmt`` step.
- ``translate.py`` default translator changed from "try
  ``from i18n import _`` (recorder)" to "use the client catalog
  directly". ``set_translator(host_tr)`` overrides as before, but
  ``tr(msg)`` now falls **back** to the client catalog whenever the
  host translator returns ``msg`` unchanged. The fallback layer means
  embedded peers (the recorder) do not need to duplicate client
  strings into ``aztrecorder.po``: a string the recorder catalog
  doesn't know falls through to ``azt_collab_client.po``. Owns its
  own strings, no duplication, gettext-canonical.
- Behavior change to be aware of: hosts that previously relied on
  the implicit ``from i18n import _`` fallback now get the client
  catalog first. Hosts with their own catalogs should keep calling
  ``set_translator(host._)`` at startup; the new fallback in
  ``tr()`` handles client strings transparently.

### azt_collab_client 0.14.1 — picker version label clarified
- Picker bottom-strip version label changed from ``collab X.Y.Z`` to
  ``client X.Y.Z`` so users can tell client and server versions
  apart at a glance (the settings page shows both).
- ``ProjectPickerScreen`` version label sized up:
  ``font_size: sp(13)`` / ``color: T.TEXT_DIM`` (was
  ``sp(11)`` / ``T.TEXT_FAINT``). Same change applied in
  ``azt_collabd/ui/app.py`` for consistency.

### azt_collab_client 0.14.0 — auth-prompt modal on clone failure, client_too_old handshake
- ``check_server_compat()`` gained a third branch: when the server's
  ``min_client_version`` is greater than this client's ``__version__``,
  the function now returns
  ``{'ok': False, 'error': 'client_too_old', 'client_version', 'server_version', 'min_required'}``.
  Mirrors the existing ``server_too_old`` shape so peer apps can branch
  on the same dict. Forward-compatible with pre-0.12.0 daemons that
  don't publish ``min_client_version`` (treated as "no floor").
- New status mirror ``S.CLONE_AUTH_REQUIRED`` and translation:
  *"Clone failed — repository not found. This may be a private
  repository. Are you authenticated to {host}?"* (host is rendered
  Title-cased: GitHub / Gitlab).
- ``azt_collabd/ui/picker_app.py`` clone-fail flow now threads the
  daemon's ``Result`` through ``_after_clone_fail``. When the result
  carries ``CLONE_AUTH_REQUIRED``, the modal renders the translated
  prompt and an extra **Open settings** button that calls
  ``azt_collab_client.open_server_ui()``. Previously the user saw a
  bare *"Clone failed: not found"* and had no path forward — they
  would not have visited Settings before picking a project, so we
  lead them there.
- ``_show_error`` grew an optional ``extra_button=(label, callback)``
  argument so the same modal helper can host either a single Dismiss
  button (existing behavior) or a two-button row.

### azt_collabd 0.11.0 — settings UI restyle, picker typography fix
- The standalone settings UI (``python -m azt_collabd ui`` /
  launcher activity in the server APK) was using stock Kivy buttons
  with no theme, no ``font_name``, and no top bar. It looked
  unrelated to the recorder. Restyled to mirror the recorder's
  ``CollabScreen``: themed top bar (``T.SURFACE`` background,
  ``T.ACCENT`` bold title), ``BG``-painted screens, ``SectionLabel``
  / ``HeaderLabel`` / ``BodyLabel`` / ``DimLabel`` dynamic classes,
  ``ThemedInput`` for text fields, ``RecBtn`` for primary actions,
  ``NavBtn`` for secondary navigation. The host toggle now highlights
  the active host with ``T.GREEN`` (was: a disabled stock button).
- The standalone picker (``picker_app.py``) was rendering its
  ``RecBtn`` with raw ``font_size: 16`` (un-scaled pixels — tiny on
  hi-dpi phones), no ``font_name``, and a hardcoded blue
  ``(0.2, 0.6, 1, 1)`` instead of a theme colour. Replaced with the
  recorder's idiom: ``font_size: sp(16)``, ``font_name: FONT``,
  ``normal_color: T.ACCENT``. The error / loading modal overlays
  were also unstyled stock widgets; now ``T.SURFACE`` rounded
  panels with ``T.TEXT`` labels and a themed dismiss button.
- Both apps now call ``register_charis()`` from the new shared
  helper at startup. If the CharisSIL TTFs can be located (the
  recorder's ``fonts/`` dir during desktop dev, system font dirs on
  Linux, or a future ``azt_collab_client/fonts/`` location) they
  register under LabelBase name ``CharisSIL``; otherwise the apps
  fall back to ``Roboto`` silently. The standalone server APK
  doesn't currently bundle the TTFs (~20 MB), so on-device it falls
  back to Roboto — sizes and theme are aligned, glyphs are not.
  Bundling the fonts in the server APK is a follow-up.

### azt_collab_client 0.13.6 — shared CharisSIL helper, larger picker logo
- New ``azt_collab_client.ui.fonts.register_charis()`` (re-exported
  as ``azt_collab_client.ui.register_charis``). Discovers
  CharisSIL-Regular/Bold/Italic/BoldItalic TTFs across a small list
  of likely locations (canonical client ``fonts/`` slot, sibling
  recorder ``fonts/`` dir, system font dirs) and registers them
  under LabelBase name ``CharisSIL``. Returns the LabelBase name to
  use (``'CharisSIL'`` or ``'Roboto'``); idempotent.
- ``ProjectPickerScreen`` typography: logo grew from ``dp(200)`` to
  ``dp(240)``, title from ``sp(28)`` to ``sp(32)``, subtitle from
  ``sp(16)`` to ``sp(18)``. Logo gets explicit
  ``allow_stretch / keep_ratio: True`` so the larger size doesn't
  pixelate. Title / subtitle now centred with explicit
  ``halign: 'center'`` + ``text_size: self.size`` so they don't
  drift left in narrow layouts.

### azt_collab_client 0.13.5 — open_server_ui dispatches on Android + shared install popup
- ``open_server_ui()``'s docstring has long promised that on Android
  it would dispatch an Intent to the server APK's launcher activity.
  It now does. New ``_open_server_ui_android`` resolves
  ``PackageManager.getLaunchIntentForPackage('org.atoznback.aztcollab')``
  and starts the activity. Returns
  ``{'ok': True, 'launched': 'android-apk'}`` on success.
- If the APK isn't installed, the helper opens a new install-prompt
  popup (``ui.popups.install_server_apk_popup``) and returns
  ``{'ok': False, 'error': 'server_apk_not_installed', 'prompted': True}``
  so the caller knows the popup is on screen. The popup itself
  routes through ``Intent.ACTION_VIEW`` on Android and
  ``webbrowser.open`` on desktop, both pointed at
  ``SERVER_APK_INSTALL_URL``.
- New optional ``on_status`` callback on ``open_server_ui`` /
  ``install_server_apk_popup``: the popup uses it to surface
  "could not open install page — …" failures into the host's
  status bar without the host having to reach into the popup
  internals. Sister apps that don't pass ``on_status`` still work.
- ``ui.popups`` and ``ui/__init__`` export
  ``install_server_apk_popup`` so peers can also call it directly
  (e.g. from a startup-time ``ServerUnavailable``-handling path,
  not just from the settings button).
- Decouples sister apps from per-app reimplementations of
  "launch APK / show install prompt" and lets the viewer collapse
  ~60 lines of jnius / popup boilerplate.

### azt_collabd 0.10.6 — pin AZTCollabProvider callback proxies
- **Bug fix:** ``android_cp/service.install_callbacks`` was passing
  freshly-constructed ``_Dispatch()`` / ``_OpenFile()`` PythonJavaClass
  instances inline to ``Provider.registerCallbacks``. Java held refs;
  Python did not. After a GC cycle (typically within seconds of the
  picker Activity launching) the proxy instances were freed, and the
  next binder-thread call from a peer's ContentResolver into
  ``AZTCollabProvider.call`` dereferenced the dead type object. Net
  effect on hardware: ``Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR),
  fault addr 0x143`` on Thread-3, with backtrace through
  ``NativeInvocationHandler.invoke`` → ``_PyObject_GenericGetAttrWithDict``
  → ``_PyType_Lookup``. From the peer's perspective the picker
  Activity vanished and the recorder logged
  ``[activity-result] code=18247 result=0`` (default RESULT_CANCELED
  from the killed Activity).
- Fix: store strong refs to both proxies at module scope before
  handing them to Java. Validate with a clean rebuild
  (``buildozer android clean && buildozer android debug``).

### azt_collab_client 0.13.4 — gear icon source conditional
- The picker KV had an unconditional ``source: 'icons/gear.png'`` even
  when the host passed ``hide_settings_gear=True`` (size goes to 0×0
  in that case but Kivy still tries to resolve the source). The
  standalone picker app and any sister app that hides the gear was
  logging ``[ERROR ] [Image ] Not found <icons/gear.png>`` on every
  picker open. Cosmetic, not the cause of the segfault — but the
  standalone server APK has no ``icons/`` dir of its own so the line
  was particularly visible there.
- Fix: gate the source on the same ``{show_gear}`` template flag the
  size already uses (``source: 'icons/gear.png' if {show_gear} else ''``
  in ``azt_collab_client/ui/picker.py``).

### azt_collabd 0.10.5 — server APK ships non-Python assets
- **Bug fix:** `server_apk/buildozer.spec` had `source.include_exts =
  py,xml`, which silently stripped `azt_collab_client/ui/assets/
  langtags_mini.json.gz` and `azt_collab_client/azt.png` from the
  packaged APK. Net effect: tapping **Start New** in the picker
  Activity raised `FileNotFoundError: ... langtags_mini.json.gz` from
  `LangPickerScreen._load_langtags`, which crashed the Activity; the
  Activity then finished without an explicit `setResult(-1, ...)` so
  peers saw `[activity-result] code=18247 result=0` (RESULT_CANCELED
  for `_AZT_PICK_REQ_CODE`). The same was true for any flow that
  reaches the langpicker, including the path between Clone-Internet
  and the post-clone confirmation when the user backs out via the
  langpicker.
- Fix: extend `source.include_exts` to `py,xml,gz,png`. `gz` covers
  the langtags blob; `png` covers the suite icon used by `App.icon`
  in `azt_collabd/ui/picker_app.py`. Validate with a clean rebuild
  (`buildozer android clean && buildozer android debug`) — hot
  patches into `.buildozer/.../build/` only prove the patch *content*
  is right, not that the spec change actually fires.

### Suite naming convention
- Adopted a single rule for surfacing the "azt collab" / "azt
  recorder" names across systems with incompatible identifier rules
  (Python identifiers forbid `-`, Android package segments forbid
  `-`, GitHub App slugs forbid `_`). Documented in `CLAUDE.md`. Net:
  `_` is the default for code identifiers, env vars, repo dirs, and
  Python packages; the dropped form (`aztcollab`) is the Android
  package; the hyphenated form (`azt-collaboration`) is the GitHub
  App slug; the human-facing form is "AZT Collaboration" (same word
  in French and English, which keeps i18n natural for the SIL user
  base). The internal token "collab" stays in code (`azt_collabd`,
  `AZT_COLLAB_ACCESS`, `org.atoznback.aztcollab`) — only the
  human-facing surfaces and the GitHub App slug expand to
  "Collaboration" / "azt-collaboration".
- Default `app_slug` in `azt_collabd/config.py` is now
  `azt-collaboration` (was `azt-recorder`, an artifact of the
  recorder's earlier ownership of the GitHub App). Override via
  `AZT_GITHUB_APP_SLUG=` if your suite ships under a different
  registration. The same default is mirrored in
  `server_apk/main.py` and the README env-var table.
- Buildozer title ("AZT Collaboration") and prose mentions ("AZT
  Collaboration service") capitalized consistently across the tree.

### Android manifest assembly
- **Bug fix:** `buildozer.spec`'s manifest-extras key is
  `android.extra_manifest_xml`, NOT `android.manifest_extra_xml`
  (different word order). Buildozer silently ignores unknown keys,
  so before this fix every peer's extras file was being dropped on
  the floor — including the recorder's `<queries>` block, which on
  Android 11+ is required for the client's `discover()` probe to
  even see the server APK.
- New canonical peer manifest extras at
  `android/manifest_extras_peer.xml` (just the suite `<queries>`
  block; meant to be symlinked into each peer as
  `manifest_extras.xml` and referenced from buildozer.spec).
- `server_apk/manifest_extras.xml` rewritten to top-level-only
  content (just the signature `<permission>`). p4a's
  `extra_manifest_xml` injects at top level under `<manifest>`,
  before `<application>` — `<application>` wrappers there end up as
  duplicate-application errors.
- New `before_apk_assemble` step in
  `buildozer_tweaks/p4a_hook.py`:
  `_inject_aztcollab_provider`. Patches the rendered
  `AndroidManifest.xml` post-template-render to inject the
  `<provider>` declaration inside `<application>`. Gated on
  `dist_name == 'aztcollab'` so peer APKs don't accidentally
  inherit the provider declaration. (p4a's SDL2 bootstrap manifest
  template only exposes top-level injection, so the provider — which
  must live inside `<application>` — has no spec-level injection
  point.)

### Build infrastructure (NDK r29 compatibility)
- New local recipe override `recipes/sdl2_ttf/__init__.py` in
  `buildozer_tweaks/`: patches harfbuzz's `Android.mk` to add
  `-DHB_NO_PRAGMA_GCC_DIAGNOSTIC_ERROR -Wno-error=cast-function-type-strict`.
  SDL2_ttf 2.20.2 ships an old harfbuzz whose `hb.hh` promotes
  `-Wcast-function-type` to error via `#pragma GCC diagnostic`, and
  NDK r29's clang lumps the `-strict` variant into that group;
  `hb-ft.cc`'s `(FT_Generic_Finalizer)` casts then fail.
- New `recipes/kivy/__init__.py`: (a) gates kivy's
  `merge(flags, sdl2_flags)` on `not kivy_sdl2_path` so host
  pkg-config doesn't leak `-I/usr/include/SDL2` (and via that
  `sys/cdefs.h`) into the cross-compile; (b) adds
  `-Wno-error=incompatible-function-pointer-types` to CFLAGS so
  kivy 2.3.0's `cgl_gl.c` glShaderSource const-mismatch doesn't
  fail the build.
- All build patches now run from `prebuild_arch` / `before_apk_*`
  hooks — the previous `before_apk_build` placement of the harfbuzz
  and kivy patches in `p4a_hook.py` was dead code (it fires after
  `build_recipes`, way too late). Those legacy `_patch_*` functions
  in the hook are kept as harmless no-ops; safe to remove on a
  future cleanup.

### Server APK packaging
- New `server_apk/setup.sh`: idempotent symlink creator for
  `azt_collabd` and `azt_collab_client` from the parent repo into
  the server APK source dir. Run once after a fresh checkout so
  buildozer can find the daemon code. Replaces the dangling
  `../setup_from_nuke.sh` reference that used to live in
  `main.py`'s comments.
- `server_apk/buildozer.spec` now points at the proper
  `extra_manifest_xml` key and includes icon assets
  (`icon.filename`, `icon.adaptive_*`) so the launcher icon isn't
  the default Kivy logo.

### azt_collabd 0.10.3 — manifest dual-patch + on-device verification

- `_inject_aztcollab_provider` and `_inject_aztcollab_pick_intent`
  in `buildozer_tweaks/p4a_hook.py` now patch BOTH
  `AndroidManifest.xml` (the dist root) AND
  `src/main/AndroidManifest.xml` (the file gradle's default
  sourceSets actually reads). Previously patched only the dist
  root, so gradle ran against the unpatched copy and the resulting
  APK had no `<provider>` despite the dist-root manifest on disk
  looking correct. Symptom: dumpsys showed no provider yet
  `aapt dump xmltree` of the *dist root* manifest confirmed it —
  diverging because gradle's input was a different file.
- New `server_apk/test_install.sh` — 15-check on-device
  verification of the server APK: install, `<permission>`
  declaration, signature self-grant, `<provider>` registration
  (multi-source: per-package dumpsys, system-wide provider table,
  `pm dump`), direct `content query`, bundled
  `azt_collabd`/`azt_collab_client` Python modules, launcher icon
  vs. default Kivy logo, activity launches without crash, source
  symlinks, dist manifest sentinel, hook traces, installed-vs-bin
  APK md5 match, APK's own manifest, all dist manifests' patch
  status, gradle manifest config.
- New `azt_collab_client/test_peer.sh` — peer-side verification:
  walks each `org.atoznback.*` package on the device, confirms
  each requests `AZT_COLLAB_ACCESS`, was granted (signature match),
  declares the suite `<queries>` block, and signs against the
  fingerprint in `android/SUITE_FINGERPRINT`.

### azt_collabd 0.10.1 — naming default + build/manifest plumbing
- Default `_SLUG_DEFAULT` in `config.py` flipped from `'azt-recorder'`
  to `'azt-collaboration'` to match the renamed GitHub App slug.
  Same default mirrored in `server_apk/main.py`.
- All cross-cutting build / manifest / naming work above
  (NDK r29 patches, manifest assembly fix,
  `_inject_aztcollab_provider` hook, `setup.sh`) lands in this
  patch level — no daemon API change, but the server APK packaging
  pipeline becomes reliable for the first time.

### azt_collabd 0.10.0 — picker helper subprocess
- New `python -m azt_collabd projects` entry point in `__main__.py`.
  Runs `azt_collabd.ui.picker_app.PickerApp`, a single-purpose Kivy
  app that hosts the shared `ProjectPickerScreen` +
  `LangPickerScreen` and implements the create-flow callbacks
  (`open_file` / `clone_dialog` / `show_start_over` /
  `new_from_template`) internally. Every successful flow ends in
  `_emit_and_quit(path, langcode='')`, which writes
  `AZT_PICK\t<path>\t<langcode>\n` on stdout and exits 0
  (or sets the Activity result on Android — see server APK below).
  Cancel / window-close exits 1.
- New `azt_collabd/ui/picker_app.py`. Hides the gear icon on the
  shared picker (no settings of its own), mounts the langpicker for
  Start New, drives `clone_project` and `create_project_from_template`
  on worker threads, surfaces errors via an in-window modal overlay
  (window stays open for retry).
- `azt_collabd/ui/app.py` (settings UI) trimmed: no more "Projects"
  NavBar button, no `ProjectPickerScreen` mount, no host-contract
  stubs. App now uses `_AZT_ICON` from `azt_collab_client/azt.png`.
- `server_apk/main.py` reads the launching Intent action; if it's
  `org.atoznback.aztcollab.PICK_PROJECT`, mounts the picker app
  instead of the settings UI. Same `PythonActivity` handles both —
  no second Activity declaration in the manifest required (the
  Intent action is matched by the existing PythonActivity entry +
  the new `<intent-filter>` line described in
  `azt_recorder/docs/p4a_hook_picker_intent.diff`).

### azt_collab_client 0.13.3 — pick_project UI-thread JNI dispatch
- Bug fix: `_pick_project_android` was building the
  `ActivityResultListener` proxy on the worker thread that called
  `pick_project()`. Worker threads attached by jnius' thread hook
  don't carry the app `ClassLoader`, so
  `find_javaclass('org/kivy/android/PythonActivity$ActivityResultListener')`
  fell back to the system loader (which has no app inner classes)
  and raised `JavaException: ClassNotFoundException`. Net effect:
  the recorder fired the `PICK_PROJECT` Intent, the worker thread
  died before `startActivityForResult` ever ran, the recorder's
  blocking modal stayed up forever showing only "Pick a project to
  continue. Cancel" with no picker Activity behind it.
- Fix: dispatch all JNI work (autoclass lookups, Intent build,
  `resolveActivity`, `bind(on_activity_result=...)`,
  `startActivityForResult`) to Kivy's main thread via
  `Clock.schedule_once`. The Kivy main thread is the Android UI
  thread, where the app `ClassLoader` is in scope and inner-class
  resolution works. The caller's thread only blocks on the result
  `Event`. Setup itself is bounded by a 10-second `Event.wait()` so
  a wedged UI thread can't hang the caller indefinitely.
- No API change; the function still returns the same dict shapes.

### azt_collab_client 0.13.2 — test_peer.sh fingerprint extraction
- `test_peer.sh` extends signing-fingerprint detection beyond
  keytool/dumpsys (both miss v2/v3-only signed APKs) with apksigner
  + openssl-on-META-INF fallbacks. Auto-discovers apksigner under
  `ANDROID_HOME` / `ANDROID_SDK_ROOT` / buildozer's bundled SDK at
  `~/.buildozer/android/platform/android-sdk/build-tools/*/apksigner`.
- Diagnostic chain now reports every tool that was tried so a WARN
  line tells you exactly which step failed (was: a single misleading
  message that reflected only the first failure).
- Strips `SHA[- ]?(256|1):` labels before regex extraction so the
  fingerprint match can't latch onto the `56` inside `SHA256:` and
  produce off-by-one bytes (was happening on the SUITE_FINGERPRINT
  file itself).
- Detects `CN=Android Debug` in apksigner output and reports
  `peer is signed with the Android Debug keystore; SUITE_FINGERPRINT
  check skipped (only meaningful for release builds)` instead of
  failing with a spurious mismatch. Debug builds of all suite peers
  share the same default Android debug keystore, so cross-app
  signature-permission gates still work; the suite-fingerprint check
  was only ever meant for release builds.

### azt_collab_client 0.13.1 — picker resilience
- `_pick_project_android`: pre-check Intent resolvability via
  `PackageManager.resolveActivity(intent, 0)` before
  `startActivityForResult`. Returns `server_apk_not_installed`
  immediately when no Activity matches, instead of relying on
  `ActivityNotFoundException` propagation through pyjnius (some
  OEM Android builds silently no-op the call instead of throwing).
- `done.wait()` capped at 10 minutes by default (was infinite). A
  launched-but-hung picker Activity can no longer wedge the caller
  forever; callers can still pass a smaller `timeout_seconds`.

### azt_collab_client 0.13.0 — pick_project()
- New `pick_project(timeout_seconds=None)` in `__init__.py`. Same
  shape as `open_server_ui()`: subprocess spawn on desktop
  (parses `AZT_PICK\t<path>\t<langcode>` from stdout), Intent
  dispatch on Android (uses `android.activity.bind` to wait on
  `onActivityResult`; falls back to
  `{'ok': False, 'error': 'server_apk_not_installed'}` if the
  server APK isn't installed). Sister apps in any toolkit drive
  project selection through this single helper.
- `azt_collab_client/ui/picker.py`: `register_kv` (a.k.a.
  `register_picker_kv`) gains a `hide_settings_gear` kwarg. When
  True the gear icon, its hit area, and the row containing it
  collapse to zero height — used by the standalone picker app
  which has no settings of its own.
- `azt_collab_client/azt.png` — suite icon shipped alongside the
  client. Both standalone Kivy apps (`ui` + `projects`) reference
  it via `os.path.dirname(azt_collab_client.__file__) + '/azt.png'`.
- `azt_collab_client/ui/assets/langtags_mini.json.gz` — moved from
  `azt_recorder/` (deferred item from step 2). The langpicker reads
  it from this default path, so sister apps no longer need to pass
  `langtags_path=` explicitly.

### Documentation
- New `examples/non_kivy_pick.py` — tiny demo of how a non-Kivy host
  drives project selection via `subprocess.run` + the AZT_PICK
  stdout protocol. Proves the cross-toolkit contract.

### Version constants unified
- `_VERSION` was duplicated as a hard-coded
  string in `server.py` (0.9.0) while `azt_collabd.__version__` lagged
  at 0.8.0. `__version__` is now the single source of truth at 0.9.0;
  `server.py` does `from . import __version__ as _VERSION` and all
  five wire-response references (server.json, started.json,
  /v1/health body, HTTP `Server:` header) flow from there.

### Documentation
- Added `azt_collab_client/CLAUDE.md` — a self-contained guide that
  travels with the client when sister apps symlink it in, so Claude
  Code working from a sister app's tree has full client / transport
  / API guidance without needing access to the canonical
  `azt-collab/CLAUDE.md`. The top-level `CLAUDE.md` now `@`-imports
  it to avoid duplication.
- README.md audited and rewritten to match the actual tree:
  removed references to the deleted `azt_collabd_plan.xml` /
  `azt_collabd_cleanup_drafts.xml`; added `server_apk/`,
  `azt_collab_client/ui/`, and `azt_collab_client/_spawn.py` to the
  layout; updated the sister-app symlink list to peer-only
  (`azt_collab_client` + `examples` + `android`, no `azt_collabd`);
  removed the stale "fall back to loopback" Android language and the
  per-peer `<provider>` instructions; added sections for the server
  APK workflow, the picker UI re-use story, the version handshake,
  and the new client API surface (`check_server_compat`,
  `init_project`, `derive_langcode`, `create_project_from_template`,
  `clone_project*`, GitHub install URL / device flow helpers, etc.).

## [0.8.0] — 2026-04-28 — `standalone_server_apk` cleanup-draft #3 (scaffolding)

Lays down the *scaffolding* for the standalone server APK
(`org.atoznback.aztcollab`) and the client-side changes that go
with it. The APK still has to be built and signed against real
devices; what is in this commit is the source tree, manifest, and
the client-side discovery + handshake the new architecture needs.

Per the user's answers in `azt_collabd_cleanup_drafts.xml`:

- q1: peer APKs symlink `azt_collab_client` only; `azt_collabd`
  lives only in the server APK and on desktop installs. The new
  `server_apk/README_NewClient.txt` documents the symlink set.
- q3: the server APK is the *only* component that calls
  `azt_collabd.configure(app_slug=..., client_id=...,
  collaborator=...)` — peers do not. The `server_apk/main.py` boot
  reads identity from env vars with the recorder defaults.
- q4: no persistent foreground-service notification by default;
  the server APK is allowed to be transient and respawns on the
  next peer query. `server_apk/service.py` keeps the always-on
  path behind `AZT_FOREGROUND_SERVICE=1` for opt-in.
- q5: client now exposes `check_server_compat()` and a
  `MIN_SERVER_VERSION` constant. Sister apps call it once at
  startup; an old server returns `{'ok': False, 'error':
  'server_too_old'}` so the peer can surface "Please update the
  AZT Collaboration service".

### New: `server_apk/`
- `buildozer.spec` — single-purpose APK targeting
  `org.atoznback.aztcollab`, requesting the suite signature
  permission, bundling the daemon and the Java provider glue.
- `manifest_extras.xml` — `<permission>`, `<provider>`, `<service>`
  declarations spliced into the generated manifest.
- `main.py` — Kivy entrypoint: configures GitHub App identity,
  registers the ContentProvider callbacks, opens the existing
  `azt_collabd.ui.app` settings UI as the launcher activity.
- `service.py` — opt-in foreground-service stub (off by default).
- `README_NewClient.txt` — peer-app integration guide (no
  `azt_collabd` symlink, no peer `<provider>` declaration,
  signature requirement, install-prompt + min-server-version
  flow).

### azt_collab_client 0.8.0
- `transports.android_cp.discover()` probes only the canonical
  server-APK authority `org.atoznback.aztcollab`. No `.aztcollab`
  suffix fallback (no peer-hosted daemons exist; we're building).
- `pick_transport()` on Android raises
  `ServerUnavailable('server_apk_not_installed')` when the server
  APK isn't reachable, instead of silently falling back to loopback
  (which can't work on Android — no Python interpreter to spawn).
- New `check_server_compat()` helper. Returns structured outcomes
  (`server_too_old` / `server_unreachable` / `ok`) suitable for
  driving an "update / install the AZT Collaboration service" UI
  affordance.
- `MIN_SERVER_VERSION` raised to `0.7.0`.

### azt_collabd 0.8.0
- Version constant aligned with the wider 0.8 baseline. No
  wire-format changes; older clients still talk to this server.
  The version bump is the signal a peer's `check_server_compat()`
  reads when it surfaces an upgrade prompt.

## [0.7.1] — 2026-04-28 — `wire_open_server_ui_button` cleanup-draft #2

The button-wiring itself lives in each sister app's `main.py`
(`../azt_recorder/main.py` for the recorder), which is outside the
canonical-source tree. What this repo can ship is the reusable
helper + documentation so each sister app's button is a one-liner.

### azt_collab_client 0.7.1
- New `open_server_ui()` helper. Desktop: spawns
  `python -m azt_collabd ui` detached and returns
  `{'ok': True, 'pid': ...}`. Android: returns
  `{'ok': False, 'error': 'desktop_only'}` until the standalone
  server APK lands and we can dispatch via Intent. Sister-app button
  code calls this helper, not subprocess directly, so the platform
  branching only lives here.
- Re-exported from `__all__`. `__version__` and `MIN_SERVER_VERSION`
  also re-exported.

### Documentation
- New "Wiring a sync-settings button" section in `README.md` with
  the KV + Python snippet sister apps can paste.
- Quick-reference snippet updated to mention `open_server_ui`.

### azt_collabd
- Unchanged at 0.7.0.

## [0.7.0] — 2026-04-28 — `android_contentprovider_transport` cleanup-draft #1

Closes the loose ends called out in
`azt_collabd_cleanup_drafts.xml` for the ContentProvider transport.
The transport classes, Java glue, and dispatch extraction were
already in place at 0.6.0; 0.7.0 hardens behavior when providers
come and go.

### azt_collab_client 0.7.0
- `rpc.call()` and `rpc.health()` now reset the cached transport and
  re-pick on `ServerUnavailable`. A provider host that gets killed
  mid-session falls through to loopback on the next call without
  the host having to restart. Symmetrically, a provider appearing
  after a loopback startup will be picked up the next time the
  client's transport cache is invalidated.
- `transports.current_transport_name()` exposes which transport is
  in use (``loopback`` / ``android_cp``) for diagnostic surfaces.
- `__version__` and `MIN_SERVER_VERSION` are now defined at the
  package root for sister apps to read.

### azt_collabd 0.7.0
- Version constant aligned with client. `_VERSION` bumped to 0.7.0
  in `azt_collabd/server.py` and a matching `__version__` exposed
  from the package.
- No wire-format changes; clients < 0.7.0 keep working unchanged.

## [0.6.0] — pre-cleanup baseline

Snapshot of the state at the end of the 16-step migration plan
(`azt_collabd_plan.xml`). Cleanup drafts pick up from here.

### azt_collabd 0.6.0
- Loopback HTTP server with bearer token + flock single-instance guard.
- Transport-agnostic `dispatch(method, path, body)` in `server.py`.
- Connectivity watcher and debounced `request_sync` job queue.
- LIFT-aware three-way merge by `<entry guid>` with side-by-side
  conflict preservation.
- Per-project advisory `flock` locking, reentrant within a process.
- pyjnius shim (`azt_collabd/android_cp/service.py`) routing
  ContentProvider calls into the dispatch table; `openFile` streaming
  scoped to `$AZT_HOME/projects/`.
- Crash-log tail returned in `/v1/health`.

### azt_collab_client 0.6.0
- Pluggable `Transport` ABC; `pick_transport()` chooses Android
  ContentProvider when reachable, else loopback.
- Loopback transport spawns the daemon on demand, retries on
  `SERVICE_RESTARTED`.
- Decode-only `Status` / `Result` / `Project` / `ProjectStatus`.
- `translate_status` / `translate_result` for UI display; default
  English + French maps.
