"""Shared Kivy UI for project-picking flows.

Sister apps register screens from this package into their own
ScreenManager. Translations route through ``azt_collab_client.translate``
(call ``set_translator`` once at startup if your host already has an
i18n module).

Step 2 of the picker migration (azt_collab_picker_migration.xml) only
moves ``LangPickerScreen``; ``ProjectPickerScreen`` lands in step 5.
"""

from .langpicker import LangPickerScreen, register_kv as register_langpicker_kv
from .picker import ProjectPickerScreen, register_kv as register_picker_kv
from .popups import clone_url_popup

__all__ = ['LangPickerScreen', 'register_langpicker_kv',
           'ProjectPickerScreen', 'register_picker_kv',
           'clone_url_popup']
