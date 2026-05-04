[app]

# AZT Collaboration service — single-purpose APK that owns $AZT_HOME on
# Android and exposes the daemon to sibling AZT suite apps via
# AZTCollabProvider. Peer APKs (recorder, sister apps) become pure
# ContentProvider clients of this APK; see README_NewClient.txt.

title = AZT Collaboration
package.name = aztcollab
package.domain = org.atoznback
icon.filename = %(source.dir)s/icons/icon.png
icon.adaptive_foreground.filename = %(source.dir)s/icons/icon_adaptive_fg.png
icon.adaptive_background.filename = %(source.dir)s/icons/icon_adaptive_bg.png

presplash.filename = %(source.dir)s/presplash.png
android.presplash_color = #8cd9bf

source.dir = .
# We pull the daemon and client from sibling directories. Buildozer
# resolves these paths at packaging time, then the resulting APK
# bundles both packages alongside server_apk/main.py.
# Extensions: py (code) + xml (manifest extras) + gz (langtags_mini.json.gz
# under azt_collab_client/ui/assets/) + png (azt_collab_client/azt.png,
# referenced by azt_collabd/ui/picker_app.py as App.icon).
source.include_patterns = main.py,service.py
# po/mo: gettext catalogs under azt_collab_client/locales/<lang>/LC_MESSAGES.
# Without these the device's available_languages() finds nothing and the
# settings UI only offers English.
source.include_exts = py,xml,gz,png,po,mo

version.regex = __version__ = ['"](.*)['"]
version.filename = %(source.dir)s/../azt_collabd/__init__.py

requirements = python3,kivy,pyjnius,dulwich,certifi,urllib3,typing_extensions
# Pulled in via path symlink at packaging time. (See setup.sh in this
# directory for the symlink dance — we mirror the sister-app pattern
# documented in README.md so the server APK source tree contains
# azt_collabd/ + azt_collab_client/ as siblings of main.py.)

orientation = portrait
fullscreen = 0

# Identity of this APK. Peer APKs probe for exactly this package name
# / authority — see azt_collab_client/transports/android_cp.py.
android.package = org.atoznback.aztcollab
p4a.branch = master
p4a.hook = /home/kentr/bin/raspy/buildozer_tweaks/p4a_hook.py
p4a.local_recipes = /home/kentr/bin/raspy/buildozer_tweaks/recipes
android.api = 36 
#was 33
android.minapi = 26 
#was 21
android.archs = arm64-v8a, armeabi-v7a
#p4a.develop:
android.ndk = 29 
#p4a.master:
#android.ndk = 27

# Custom suite signature permission. Same name and signature as
# every peer APK. Without that match, install-time grant is denied
# and peers cannot reach this provider — desired failure mode. The
# same permission gates bindService against AZTServiceProviderhost.
android.permissions = INTERNET, org.atoznback.AZT_COLLAB_ACCESS

# ContentProvider + sticky-bound service declarations.
# Note: buildozer's key is `extra_manifest_xml` (NOT `manifest_extra_xml`,
# which buildozer silently ignores). Content from this file is inlined
# into p4a's --extra-manifest-xml and lands at top-level under
# <manifest>, so this file may only contain top-level elements
# (<permission>, <queries>, ...). The <provider> and <service>
# declarations that belong inside <application> are injected by the
# _inject_aztcollab_provider and _inject_aztcollab_service steps in
# p4a_hook.py.
android.extra_manifest_xml = %(source.dir)s/manifest_extras.xml

# Suite Java glue is bundled here so peers don't have to ship it.
# Holds AZTCollabProvider.java (provider class) and
# AZTServiceProviderhost.java (sticky-bound service class). Peer APKs
# also compile these but only the server APK declares them in its
# manifest — the unused classes are harmless ballast.
android.add_src = ../android/src/main/java

# Sign with the suite keystore — same key as every peer APK.
#This avoids creating an aab, but also turns off signing (you need to self-sign)
android.release_artifact = apk
android.signing.keystore = /home/kentr/bin/azt-suite.keystore                                                     
android.signing.key_alias = azt                                              
p4a.sign = True

[buildozer]

log_level = 2
warn_on_root = 0
build_dir = /home/kentr/bin/AZT/.buildozer
