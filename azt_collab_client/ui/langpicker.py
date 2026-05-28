"""LangPickerScreen — pick / construct a BCP-47 language tag.

Moved from azt_recorder/main.py as step 2 of
azt_collab_picker_migration.xml. The host registers KV via
``register_kv(font_name, langtags_path)`` after its own KV is loaded;
the host's ``app._pending_vernlang``/``app.new_from_template`` continue
to be invoked from ``_on_continue`` for now (step 3 will move the
template-download path into the daemon).
"""

import gzip
import json
import os

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.uix.button import Button
from kivy.uix.screenmanager import Screen

from . import theme

from ..translate import tr as _tr


_DEFAULT_LANGTAGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'assets', 'langtags_mini.json.gz')

_LANGTAGS_PATH = _DEFAULT_LANGTAGS_PATH

_KV_TEMPLATE = '''
#:import dp kivy.metrics.dp
#:import sp kivy.metrics.sp
#:import T azt_collab_client.ui.theme
#:import _ azt_collab_client.translate.tr
#:set FONT '{font_name}'

<LangPickerScreen>:
    canvas.before:
        Color:
            rgba: T.BG
        Rectangle:
            pos: self.pos
            size: self.size
    BoxLayout:
        orientation: 'vertical'
        padding: dp(16)
        spacing: dp(10)
        Label:
            text: _('Choose your language')
            font_size: sp(22)
            font_name: FONT
            bold: True
            color: T.ACCENT
            size_hint_y: None
            height: dp(44)
        TextInput:
            id: lang_search
            hint_text: _('Type a language name...')
            font_size: sp(16)
            font_name: FONT
            multiline: False
            size_hint_y: None
            height: dp(44)
            background_color: T.SURFACE
            foreground_color: T.TEXT
            hint_text_color: T.HINT
            cursor_color: T.ACCENT
            padding: [dp(10), dp(10)]
            on_text: root._on_search_text(self.text)
        Widget:
            id: selection_placeholder
            size_hint_y: None
            height: 0
        ScrollView:
            id: results_scroll
            size_hint_y: None
            height: min(results_box.minimum_height, dp(416))
            do_scroll_x: False
            bar_width: dp(4)
            BoxLayout:
                id: results_box
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                spacing: dp(4)
        Widget:
'''


_SELECTION_KV = '''
BoxLayout:
    orientation: 'vertical'
    size_hint_y: None
    height: self.minimum_height
    spacing: dp(6)
    Label:
        id: selected_label
        text: ''
        font_size: sp(15)
        font_name: FONT
        color: T.TEXT
        size_hint_y: None
        height: dp(32)
        halign: 'left'
        text_size: self.width, None
    Label:
        id: region_title
        text: _('Select region:')
        font_size: sp(14)
        font_name: FONT
        color: T.TEXT_DIM
        size_hint_y: None
        height: 0
        opacity: 0
        halign: 'left'
        text_size: self.width, None
    ScrollView:
        id: region_scroll
        size_hint_y: None
        height: min(region_box.minimum_height, dp(420))
        do_scroll_x: False
        bar_width: dp(4)
        BoxLayout:
            id: region_box
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(4)
    BoxLayout:
        id: region_chosen
        orientation: 'horizontal'
        size_hint_y: None
        height: 0
        opacity: 0
        disabled: True
        spacing: dp(8)
        Label:
            id: region_chosen_label
            text: ''
            font_size: sp(14)
            font_name: FONT
            color: T.TEXT
            halign: 'left'
            valign: 'middle'
            text_size: self.size
            shorten: True
        Button:
            id: region_change_btn
            text: _('Change')
            size_hint_x: None
            width: dp(96)
            background_color: T.SURFACE_ALT
            background_normal: ''
            color: T.ACCENT
            font_name: FONT
            font_size: sp(13)
    BoxLayout:
        size_hint_y: None
        height: dp(40)
        spacing: dp(8)
        CheckBox:
            id: dialect_check
            size_hint_x: None
            width: dp(40)
            active: False
        Label:
            text: _("I'm working on a dialect")
            font_size: sp(14)
            font_name: FONT
            color: T.TEXT
            halign: 'left'
            valign: 'middle'
            text_size: self.size
    TextInput:
        id: dialect_input
        hint_text: _('Variant code (2-8 chars)')
        font_size: sp(14)
        font_name: FONT
        multiline: False
        size_hint_y: None
        height: 0
        opacity: 0
        background_color: T.SURFACE
        foreground_color: T.TEXT
        hint_text_color: T.HINT
        cursor_color: T.ACCENT
        padding: [dp(10), dp(10)]
    Label:
        id: code_label
        text: ''
        font_size: sp(16)
        font_name: FONT
        bold: True
        color: T.GREEN
        size_hint_y: None
        height: dp(28)
        halign: 'left'
        text_size: self.width, None
    Button:
        id: continue_btn
        text: _('Continue')
        size_hint_y: None
        height: dp(52)
        background_color: T.GREEN
        background_normal: ''
        color: 1, 1, 1, 1
        font_name: FONT
        font_size: sp(16)
        bold: True
'''


_FONT_NAME = 'Roboto'


def register_kv(font_name='Roboto', langtags_path=None):
    """Load LangPickerScreen KV with the host's font and asset path.

    Call once after your main KV is loaded (so the ScreenManager rule
    referencing LangPickerScreen finds the class).
    """
    global _LANGTAGS_PATH, _FONT_NAME
    _FONT_NAME = font_name
    if langtags_path:
        _LANGTAGS_PATH = langtags_path
    Builder.load_string(_KV_TEMPLATE.format(font_name=font_name))


class LangPickerScreen(Screen):
    """Language code picker shown when creating a new project."""
    _langtags = None
    _search_index = None
    _region_names = None

    _selected = None
    _selected_region = ''
    _dialect_code = ''
    _selection_box = None

    def on_enter(self):
        self._selected = None
        self._selected_region = ''
        self._dialect_code = ''
        si = self.ids.get('lang_search')
        if si:
            si.text = ''
        self._hide_selection()
        if LangPickerScreen._langtags is None:
            self._load_langtags()

    @classmethod
    def _load_langtags(cls):
        with open(_LANGTAGS_PATH, 'rb') as f:
            blob = json.loads(gzip.decompress(f.read()))
        cls._langtags = blob['langs']
        cls._region_names = blob.get('region_names', {})
        idx = []
        for entry in cls._langtags:
            parts = [entry.get('n', '').lower()]
            if 'ln' in entry:
                parts.append(entry['ln'].lower())
            if 'ns' in entry:
                parts.extend(n.lower() for n in entry['ns'])
            if 'lns' in entry:
                parts.extend(n.lower() for n in entry['lns'])
            if 't' in entry:
                parts.append(entry['t'].lower())
            if 'i' in entry:
                parts.append(entry['i'].lower())
            idx.append(' '.join(parts))
        cls._search_index = idx

    def _on_search_text(self, text):
        if self._selected and text:
            self._selected = None
            self._selected_region = ''
            self._dialect_code = ''
            self._hide_selection()
        if hasattr(self, '_search_ev') and self._search_ev:
            self._search_ev.cancel()
        self._search_ev = Clock.schedule_once(
            lambda dt: self._do_search(text), 0.25)

    def _do_search(self, text):
        box = self.ids.get('results_box')
        if not box:
            return
        box.clear_widgets()
        if not text or len(text) < 2 or self._langtags is None:
            return
        q = text.lower()
        matches = []
        for i, searchable in enumerate(self._search_index):
            if q in searchable:
                matches.append(self._langtags[i])
                if len(matches) >= 50:
                    break
        for entry in matches:
            self._add_result_row(box, entry)

    def _add_result_row(self, box, entry):
        btn = Button(
            text=self._format_entry(entry),
            font_size=sp(13),
            font_name=_FONT_NAME,
            size_hint_y=None,
            height=dp(48),
            halign='left',
            valign='middle',
            background_color=theme.SURFACE,
            background_normal='',
            color=theme.TEXT,
            padding=(dp(10), dp(4)),
        )
        btn.text_size = (None, None)
        btn.bind(size=lambda w, s: setattr(w, 'text_size', s))
        btn.bind(on_release=lambda w: self._select_language(entry))
        box.add_widget(btn)

    @staticmethod
    def _format_entry(entry):
        name = entry.get('n', '')
        local = entry.get('ln', '')
        tag = entry.get('t', '')
        region = entry.get('rn', '')
        parts = [name]
        if local and local != name:
            parts[0] += f'  ({local})'
        parts.append(f'[{tag}]')
        if region:
            parts.append(f'- {region}')
        return '  '.join(parts)

    def _get_selection_box(self):
        if self._selection_box is None:
            self._selection_box = Builder.load_string(_SELECTION_KV)
            dc = self._selection_box.ids.get('dialect_check')
            if dc:
                dc.bind(active=lambda w, v: self._toggle_dialect(v))
            di = self._selection_box.ids.get('dialect_input')
            if di:
                di.bind(text=lambda w, t: self._update_code())
            cb = self._selection_box.ids.get('continue_btn')
            if cb:
                cb.bind(on_release=lambda w: self._on_continue())
            rcb = self._selection_box.ids.get('region_change_btn')
            if rcb:
                rcb.bind(on_release=lambda w: self._change_region())
        return self._selection_box

    def _show_selection(self):
        box = self._get_selection_box()
        parent = self.ids.get('selection_placeholder').parent
        if box.parent is None:
            placeholder = self.ids.get('selection_placeholder')
            idx = list(parent.children).index(placeholder)
            parent.add_widget(box, index=idx)

    def _remove_selection(self):
        if self._selection_box and self._selection_box.parent:
            self._selection_box.parent.remove_widget(self._selection_box)

    @property
    def _sel_ids(self):
        if self._selection_box:
            return self._selection_box.ids
        return {}

    def _select_language(self, entry):
        self._selected = entry
        self._selected_region = ''
        self._show_selection()
        ids = self._sel_ids
        lbl = ids.get('selected_label')
        if lbl:
            lbl.text = self._format_entry(entry)
        box = self.ids.get('results_box')
        if box:
            box.clear_widgets()
        si = self.ids.get('lang_search')
        if si:
            si.text = ''

        regions = entry.get('rs', [])
        primary = entry.get('r', '')
        all_regions = []
        if primary:
            all_regions.append(primary)
        for r in regions:
            if r not in all_regions:
                all_regions.append(r)

        region_box = ids.get('region_box')
        region_title = ids.get('region_title')
        region_scroll = ids.get('region_scroll')
        region_chosen = ids.get('region_chosen')
        region_chosen_label = ids.get('region_chosen_label')
        if region_box:
            region_box.clear_widgets()
        if region_chosen:
            region_chosen.height = 0
            region_chosen.opacity = 0
            region_chosen.disabled = True
        if region_chosen_label:
            region_chosen_label.text = ''
        if region_scroll:
            region_scroll.opacity = 1
            region_scroll.disabled = False
        if len(all_regions) > 1:
            if region_title:
                region_title.height = dp(20)
                region_title.opacity = 1
                region_title.disabled = False
            rnames = self._region_names or {}
            btn = Button(
                text=_tr('Multiple / all regions'),
                font_size=sp(13),
                font_name=_FONT_NAME,
                size_hint_y=None,
                height=dp(38),
                background_color=theme.SURFACE_ALT,
                background_normal='',
                color=theme.TEXT,
            )
            btn.bind(on_release=lambda w: self._select_region(''))
            region_box.add_widget(btn)
            for rc in all_regions:
                rn = rnames.get(rc, rc)
                btn = Button(
                    text=f'{rn} ({rc})',
                    font_size=sp(13),
                    font_name=_FONT_NAME,
                    size_hint_y=None,
                    height=dp(38),
                    background_color=theme.SURFACE_ALT,
                    background_normal='',
                    color=theme.TEXT,
                )
                btn.bind(on_release=lambda w, c=rc: self._select_region(c))
                region_box.add_widget(btn)
        else:
            if region_title:
                region_title.height = 0
                region_title.opacity = 0

        self._update_code()
        cb = ids.get('continue_btn')
        if cb:
            cb.disabled = False

    def _select_region(self, region_code):
        self._selected_region = region_code
        ids = self._sel_ids
        if region_code:
            rname = (self._region_names or {}).get(region_code, region_code)
            display = f'{rname} ({region_code})'
        else:
            display = _tr('Multiple / all regions')
        region_title = ids.get('region_title')
        region_scroll = ids.get('region_scroll')
        region_chosen = ids.get('region_chosen')
        region_chosen_label = ids.get('region_chosen_label')
        if region_title:
            region_title.height = 0
            region_title.opacity = 0
            region_title.disabled = True
        if region_scroll:
            region_scroll.height = 0
            region_scroll.opacity = 0
            region_scroll.disabled = True
        if region_chosen:
            region_chosen.height = dp(40)
            region_chosen.opacity = 1
            region_chosen.disabled = False
        if region_chosen_label:
            region_chosen_label.text = display
        self._update_code()

    def _change_region(self):
        ids = self._sel_ids
        self._selected_region = ''
        region_title = ids.get('region_title')
        region_scroll = ids.get('region_scroll')
        region_box = ids.get('region_box')
        region_chosen = ids.get('region_chosen')
        if region_title:
            region_title.height = dp(20)
            region_title.opacity = 1
            region_title.disabled = False
        if region_scroll and region_box:
            region_scroll.height = min(region_box.minimum_height, dp(420))
            region_scroll.opacity = 1
            region_scroll.disabled = False
        if region_chosen:
            region_chosen.height = 0
            region_chosen.opacity = 0
            region_chosen.disabled = True
        self._update_code()

    def _toggle_dialect(self, active):
        di = self._sel_ids.get('dialect_input')
        if di:
            di.height = dp(44) if active else 0
            di.opacity = 1 if active else 0
            if not active:
                di.text = ''
                self._dialect_code = ''
        self._update_code()

    def _hide_selection(self):
        self._remove_selection()
        ids = self._sel_ids
        if not ids:
            return
        region_title = ids.get('region_title')
        if region_title:
            region_title.height = 0
            region_title.opacity = 0
        region_box = ids.get('region_box')
        if region_box:
            region_box.clear_widgets()
        region_chosen = ids.get('region_chosen')
        if region_chosen:
            region_chosen.height = 0
            region_chosen.opacity = 0
            region_chosen.disabled = True
        region_chosen_label = ids.get('region_chosen_label')
        if region_chosen_label:
            region_chosen_label.text = ''
        di = ids.get('dialect_input')
        if di:
            di.height = 0
            di.opacity = 0
            di.text = ''
        dc = ids.get('dialect_check')
        if dc:
            dc.active = False
        cl = ids.get('code_label')
        if cl:
            cl.text = ''
        cb = ids.get('continue_btn')
        if cb:
            cb.disabled = True

    def _update_code(self):
        if not self._selected:
            return
        ids = self._sel_ids
        code = self._selected.get('t', '')
        if self._selected_region:
            code += '-' + self._selected_region
        di = ids.get('dialect_input')
        if di and di.text.strip():
            variant = di.text.strip().lower()
            variant = ''.join(c for c in variant if c.isalnum())[:8]
            self._dialect_code = variant
            if len(variant) >= 2:
                code += '-x-' + variant
        else:
            self._dialect_code = ''
        cl = ids.get('code_label')
        if cl:
            cl.text = _tr('Language code: {code}').format(code=code)

    def _assembled_code(self):
        if not self._selected:
            return ''
        code = self._selected.get('t', '')
        if self._selected_region:
            code += '-' + self._selected_region
        if self._dialect_code and len(self._dialect_code) >= 2:
            code += '-x-' + self._dialect_code
        return code

    def _on_continue(self):
        app = App.get_running_app()
        code = self._assembled_code()
        app._pending_vernlang = code
        app._show_loading_overlay(
            _tr('Setting up wordlist for {code}...').format(code=code))
        app.new_from_template()
