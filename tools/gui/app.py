from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import filedialog, font, ttk, messagebox, colorchooser

from .settings import Settings, STATE
from .privacy import full_access, is_restricted
from .viewmodel import COLUMNS, number, party_total, percentage, actor_rows, attack_rows, source_rows, clipboard_summary, status_view, rate_tooltip, display_elapsed, elapsed_label
from .timing import with_dps_mode, DPS_MODES
from ..attack_catalog.catalog import attack_label, get_catalog, with_attack_names, EDITABLE_CSV
from ..combat_core.quest_catalog import QUESTS_CSV, get_quest_catalog, quest_label
from ..combat_core.damage_policy import EXCLUSIONS_PATH, dps_damage, read_damage_exclusions
from .text_export import EXPORT_DIRECTORY, filename
from .backend import stored_history, history_key
from .control import preparation_error
from .appearance import (palette, rgb, rgb_color, Background, import_background,
                         remove_background, status_band_color, DEFAULT_BACKGROUND, DEFAULT_ACCENT)
from .windows import minimize as minimize_window

BG = '#0a0a0a'
PANEL = '#15191c'
TEXT = '#e4e8eb'
MUTED = '#939da5'
LINE = '#343c41'
ACCENT = '#76d5cf'
COLUMN_GRIP = 4  # pixels either side of a header border that start a resize
COLUMN_MIN_WIDTH = 24
MIN_WIDTH, MIN_HEIGHT = 500, 190
EDGE = 5  # pixels inside the window border that resize it
EDGE_TAG = 'LazyParseEdge'
EDGE_CURSORS = {'': '', 'n': 'size_ns', 's': 'size_ns', 'w': 'size_we', 'e': 'size_we',
                'nw': 'size_nw_se', 'se': 'size_nw_se', 'ne': 'size_ne_sw', 'sw': 'size_ne_sw'}


class Tooltip:
    def __init__(self, root, theme=None):
        self.root, self.window, self.timer = root, None, None
        self.theme = theme

    def hide(self, event=None):
        if self.timer:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.window:
            self.window.destroy()
            self.window = None

    def show(self, text, x, y):
        self.hide()
        def open_tip():
            self.timer = None
            self.window = tk.Toplevel(self.root)
            self.window.overrideredirect(True)
            self.window.attributes('-topmost', True)
            self.window.geometry(f'+{x + 10}+{y + 14}')
            tk.Label(self.window, text=text, background='#252d32', foreground=TEXT, padx=8, pady=5,
                     font=('Meiryo UI', 9), justify='left').pack()
            if self.theme:
                self.theme(self.window)
        self.timer = self.root.after(600, open_tip)


class DetailWindow:
    def __init__(self, parent, report, actor):
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title(actor['name'] + ' — LazyParse')
        self.window.geometry('1150x355')
        self.window.configure(bg=BG)
        self.label = tk.Label(self.window, bg=BG, fg=TEXT, anchor='w', padx=10, pady=9, font=('Meiryo UI', 10))
        self.label.pack(fill='x')
        notebook = self.notebook = ttk.Notebook(self.window)
        notebook.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.sort_state = {}   # tree -> (column, descending)
        self.sort_values = {}  # tree -> {item: {column: raw value}}
        self.titles = {}       # tree -> {column: heading text}
        self.attacks = self._table(notebook, '攻撃別', [('name', 'Attack', 205), ('id', 'Attack ID', 112),
            ('kind', '種別', 70), ('damage', 'Damage', 95), ('excluded', 'Damage(対象外)', 110), ('share', '%', 58), ('hits', 'Hits', 55),
            ('max', 'Max Hit', 84), ('dps', 'DPS', 82), ('ja', 'JA %', 60), ('crit', 'Crit %', 60)])
        self.sources = self._table(notebook, '発生元', [('name', 'Name / ID', 220), ('damage', 'Damage', 110),
                                                      ('key', 'Source', 220)])
        info_frame = tk.Frame(notebook, bg=BG)
        notebook.add(info_frame, text='識別・品質')
        self.info = tk.Text(info_frame, background=BG, foreground=TEXT, borderwidth=0,
                            font=('Consolas', 9), wrap='word')
        self.info.pack(fill='both', expand=True)
        self.refresh(report, actor)
        notebook.select(self.attacks.master)
        self.window.update_idletasks()
        self.window.deiconify()

    def _table(self, notebook, title, columns):
        frame = tk.Frame(notebook, bg=BG)
        tree = ttk.Treeview(frame, columns=[key for key, _, _ in columns], show='headings', style='LazyParse.Treeview')
        self.titles[tree] = {key: name for key, name, _ in columns}
        self.sort_values[tree] = {}
        for key, name, width in columns:
            tree.heading(key, text=name, command=lambda key=key, tree=tree: self.sort_by(tree, key))
            tree.column(key, width=width, minwidth=45, anchor='w' if key in {'name', 'id', 'key', 'kind'} else 'e')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview, style='LazyParse.Vertical.TScrollbar')
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True)
        notebook.add(frame, text=title)
        return tree

    def sort_by(self, tree, column):
        """Heading click: sort by this column; a second click reverses it."""
        previous, descending = self.sort_state.get(tree, (None, False))
        text_column = column in {'name', 'id', 'key', 'kind'}
        descending = not descending if previous == column else not text_column
        self.sort_state[tree] = (column, descending)
        self._apply_sort(tree)

    def _apply_sort(self, tree):
        if tree not in self.sort_state:
            return
        column, descending = self.sort_state[tree]
        values = self.sort_values[tree]
        items = list(tree.get_children())
        # Rows without a value (—) stay last in either direction.
        present = sorted((item for item in items if values[item][column] is not None),
                         key=lambda item: values[item][column], reverse=descending)
        for index, item in enumerate(present + [item for item in items if values[item][column] is None]):
            tree.move(item, '', index)
        for key, name in self.titles[tree].items():
            tree.heading(key, text=name + (' ▼' if descending else ' ▲') if key == column else name)

    def _insert(self, tree, raw, shown):
        item = tree.insert('', 'end', values=shown)
        self.sort_values[tree][item] = raw

    def refresh(self, report, actor):
        unlinked = actor.get('hp_unlinked_damage', 0)
        whole = actor.get('total_damage_dealt', actor['damage_dealt'])
        self.label.configure(text=f"{actor['name']}    Damage {whole:,}    DPS {number(actor['dps'], 1)}    Hits {actor['hits_dealt']}"
                                 f"    JA {percentage(actor['ja'])}    Crit {percentage(actor['crit'])}"
                                 + (f"    HP非連動（DPS対象外） {unlinked:,}" if unlinked else ''))
        for tree in (self.attacks, self.sources):
            tree.delete(*tree.get_children())
            self.sort_values[tree].clear()
        elapsed = display_elapsed(report)
        for attack in attack_rows(report, actor):
            name = attack_label(attack['attack_id'])
            # HP-unlinked hits stay listed but are left out of the list and DPS.
            excluded = attack.get('hp_unlinked_damage', 0)
            share = attack['damage_dealt'] * 100 / whole if whole else None
            dps = dps_damage(attack) / elapsed if elapsed else None
            raw = {'name': name, 'id': attack['attack_id'], 'kind': attack['attack_kind'] or '',
                   'damage': attack['damage_dealt'], 'excluded': excluded, 'share': share,
                   'hits': attack['hits_dealt'], 'max': attack['max_hit'], 'dps': dps,
                   'ja': attack['ja'], 'crit': attack['crit']}
            self._insert(self.attacks, raw, (name, f"0x{attack['attack_id']:08X}", attack['attack_kind'] or '—',
                number(attack['damage_dealt']), number(excluded) if excluded else '—', f'{share:.1f}' if share is not None else '—',
                attack['hits_dealt'], number(attack['max_hit']), number(dps, 1),
                number(attack['ja'], 1), number(attack['crit'], 1)))
        for source in source_rows(report, actor):
            self._insert(self.sources, {'name': source['name'] or '', 'damage': source['damage'], 'key': source['source_key']},
                         (source['name'], number(source['damage']), source['source_key']))
        for tree in (self.attacks, self.sources):
            self._apply_sort(tree)
        self.info.configure(state='normal')
        self.info.delete('1.0', 'end')
        self.info.insert('end', json.dumps({'name': actor['name'], 'object_id': actor['object_id'], 'role': actor['role'],
            'name_kind': actor.get('name_kind'), 'identity_evidence': actor.get('evidence'),
            'name_evidence': actor.get('name_evidence'), 'owner': actor.get('owner'),
            'aggregation_policy': report.get('aggregation_policy'), 'quality': report.get('quality'),
            'timing': report.get('timing'),
            'quest_measurement': report.get('quest_measurement'),
            'hit_rates': actor['hit_rates'], 'hit_rate_policy': report.get('hit_rate_policy'),
            'attack_catalog': get_catalog().metadata(),
            'attack_details': [{'attack_id': entry['attack_id'], 'name': entry['attack_name'],
                'kind': entry['attack_kind'], 'note': entry['attack_note'], 'source': entry['attack_name_source'],
                'hit_rates': entry['hit_rates']}
                for entry in attack_rows(report, actor)]},
            ensure_ascii=False, indent=2))
        self.info.configure(state='disabled')


class Application:
    def __init__(self, root, backend, *, settings_path=STATE / 'preferences.json', start_backend=True):
        self.root, self.backend, self.settings_path = root, backend, Path(settings_path)
        self.settings = Settings.load(self.settings_path)
        self.colors = palette(self.settings.theme_background, self.settings.theme_accent)
        self.background = Background()
        self.backend.interval = self.settings.interval
        self.backend.auto_quests = self.settings.auto_quests
        self._sync_text_options()
        self.latest = {}
        self.saved = None
        self.snapshot = {}
        self.live_update_pending = False
        self.last_live_display = None
        self.rows = []
        self.report = {}
        self.selected = None
        self.sort_key, self.descending = 'damage_dealt', True
        self.busy = False
        self.notice = ''
        self.last_preparation_error = None
        self.notice_timer = None
        self.details = {}
        self.drag = None
        self.column_drag = None
        self.edge_resize = None
        self.closed = False
        self.tooltip = Tooltip(root, self._theme_widgets)
        self.fonts = {}
        self.root.title('LazyParse')
        self.app_icon = tk.PhotoImage(master=self.root, file=Path(__file__).with_name('assets') / 'icon.png')
        self.root.iconphoto(True, self.app_icon)
        self.root.configure(bg=LINE)
        self.root.geometry(self.settings.geometry)
        self.root.minsize(MIN_WIDTH, MIN_HEIGHT)
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', self.settings.topmost)
        self.root.attributes('-alpha', self.settings.opacity)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self._style()
        self._build()
        self._menus()
        self._apply_theme()
        self._load_background()
        self._install_edge_resize()
        self.root.bind('<Control-Shift-E>', lambda event: self.command('Next'))
        self.root.bind('<Control-Shift-R>', lambda event: self.command('Reset'))
        self.root.bind('<Control-Shift-A>', lambda event: self.toggle('topmost'))
        self.root.bind('<Control-c>', lambda event: self.copy())
        self.root.bind('<Map>', self._mapped)
        self.root.bind('<Escape>', lambda event: self.tooltip.hide())
        self.visibility_timer = self.root.after(100, self._ensure_visible)
        if start_backend:
            self.backend.start()
        self.consume_timer = self.root.after(50, self._consume)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        c = self.colors
        style.configure('LazyParse.Treeview', background=c[BG], fieldbackground=c[BG], foreground=c[TEXT],
                        borderwidth=0, rowheight=23, font=('Meiryo UI', 9))
        style.configure('LazyParse.Treeview.Heading', background=c[PANEL], foreground=c[TEXT], font=('Meiryo UI', 9))
        style.map('LazyParse.Treeview', background=[('selected', c['#205c61'])], foreground=[('selected', c[TEXT])])
        style.configure('TNotebook', background=c[BG], borderwidth=0)
        style.configure('TNotebook.Tab', background=c[PANEL], foreground=c[TEXT], padding=(10, 5))
        style.map('TNotebook.Tab', background=[('selected', c['#1b6467'])])
        style.configure('LazyParse.Vertical.TScrollbar', background=c['#29353b'], troughcolor=c[BG],
                        bordercolor=c[BG], arrowcolor=c[MUTED], lightcolor=c['#29353b'], darkcolor=c['#29353b'])
        style.map('LazyParse.Vertical.TScrollbar', background=[('active', c['#426068'])])
        style.configure('LazyParse.Treeview.Heading', relief='flat', borderwidth=0,
                        lightcolor=c[LINE], darkcolor=c[LINE], bordercolor=c[LINE])

    def _button(self, frame, text, command):
        return tk.Button(frame, text=text, command=command, bg=PANEL, fg=TEXT, activebackground='#29383f',
            activeforeground='white', disabledforeground='#586168', font=('Meiryo UI', 8),
            relief='flat', borderwidth=0, padx=7, pady=2, takefocus=False)

    def _build(self):
        inner = tk.Frame(self.root, bg=BG)
        inner.pack(fill='both', expand=True, padx=1, pady=1)
        self.bar = tk.Frame(inner, bg=BG, height=28)
        self.bar.pack(fill='x')
        self.bar.bind('<ButtonPress-1>', self._drag_start)
        self.bar.bind('<B1-Motion>', self._drag_move)
        self.menu_holder = tk.Frame(self.bar, bg=BG)
        self.menu_holder.pack(side='left')
        self._button(self.bar, '×', self.close).pack(side='right')
        self._button(self.bar, '−', self.minimize).pack(side='right')
        self.stop_button = self._button(self.bar, '停止', lambda: self.command('Stop'))
        self.stop_button.pack(side='right', padx=(2, 0))
        self.start_button = self._button(self.bar, '記録', lambda: self.command('Start'))
        self.start_button.pack(side='right')
        self.status = tk.Label(self.bar, bg=BG, fg=MUTED, text='● 待機中', font=('Meiryo UI', 8), padx=9)
        self.status.pack(side='right')
        self.status.bind('<ButtonPress-1>', self._drag_start)
        self.status.bind('<B1-Motion>', self._drag_move)
        self.header = tk.Canvas(inner, height=23, bg=BG, highlightthickness=0)
        self.header.pack(fill='x')
        self.body_frame = tk.Frame(inner, bg=BG)
        self.body_frame.pack(fill='both', expand=True)
        self.table = tk.Canvas(self.body_frame, bg=BG, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.body_frame, orient='vertical', command=self.table.yview,
                                       style='LazyParse.Vertical.TScrollbar')
        self.table.configure(yscrollcommand=self._scrolled)
        self.table.pack(fill='both', expand=True)
        self.table.bind('<Button-1>', self._select)
        self.table.bind('<Double-Button-1>', self._open_selected)
        self.table.bind('<Button-3>', self._row_menu)
        self.table.bind('<Motion>', self._row_tip)
        self.table.bind('<Leave>', self.tooltip.hide)
        self.table.bind('<MouseWheel>', lambda event: self.table.yview_scroll(-int(event.delta / 120), 'units'))
        self.table.bind('<Configure>', lambda event: self.render())
        self.header.bind('<ButtonPress-1>', self._header_press)
        self.header.bind('<B1-Motion>', self._header_drag)
        self.header.bind('<ButtonRelease-1>', self._header_release)
        self.header.bind('<Double-Button-1>', lambda event: self._column_border(event.x) is not None and self.reset_column_widths())
        self.header.bind('<Motion>', self._header_tip)
        self.header.bind('<Button-3>', self._header_menu)
        self.header.bind('<Leave>', self.tooltip.hide)
        footer = tk.Frame(inner, bg=BG)
        footer.pack(side='bottom', fill='x', before=self.body_frame)
        self.indicator = tk.Frame(footer, height=2, bg=LINE)
        self.indicator.pack(fill='x')
        # Pack the grip first: a long summary must not squeeze it out.
        self.grip = tk.Label(footer, text='◢', bg=BG, fg=MUTED, cursor='size_nw_se', padx=3)
        self.grip.pack(side='right')
        self.grip.bind('<ButtonPress-1>', self._resize_start)
        self.grip.bind('<B1-Motion>', self._resize_move)
        self.summary = tk.Label(footer, text='Time —    Total —', bg=BG, fg=TEXT, anchor='w', padx=6, pady=3,
                                font=('Meiryo UI', 8), width=1)
        self.summary.pack(side='left', fill='x', expand=True)
        self.summary.bind('<Button-1>', lambda event: self.show_diagnostics())
        self.summary.bind('<Button-3>', self._row_menu)

    def _menu(self, label):
        button = tk.Menubutton(self.menu_holder, text=label, bg=BG, fg=TEXT, activebackground=PANEL,
            activeforeground='white', font=('Meiryo UI', 8), borderwidth=0, padx=7, pady=4)
        menu = tk.Menu(button, tearoff=False, bg=PANEL, fg=TEXT, activebackground='#24575d', activeforeground='white',
                       font=('Meiryo UI', 9))
        button.configure(menu=menu)
        button.pack(side='left')
        return menu

    def _menus(self):
        self.variables = {}
        logging = self._menu('計測')
        self.logging_menu = logging
        logging.add_command(label='記録開始', command=lambda: self.command('Start'))
        logging.add_command(label='記録停止', command=lambda: self.command('Stop'))
        logging.add_separator()
        logging.add_command(label='保存して次の計測へ', accelerator='Ctrl+Shift+E', command=lambda: self.command('Next'))
        logging.add_command(label='新しい計測へリセット', accelerator='Ctrl+Shift+R', command=lambda: self.command('Reset'))
        logging.add_separator()
        logging.add_command(label='取得を終了', command=lambda: self.command('Shutdown'))
        parsing = self.parsing_menu = self._menu('解析')
        self.variables['dps_mode'] = tk.StringVar(value=self.settings.dps_mode)
        for mode, label in [('quest', 'DPS：クエスト全体表示'), ('damage', 'DPS：総ダメージ基準表示')]:
            parsing.add_radiobutton(label=label, value=mode, variable=self.variables['dps_mode'],
                                   command=lambda mode=mode: self.set_dps_mode(mode))
        parsing.add_separator()
        self._check(parsing, 'プレイヤーのみ表示', 'players_only')
        if full_access():
            self._check(parsing, '自分のみ表示', 'only_me')
        self._check(parsing, '人物名を匿名表示', 'anonymize')
        self._check(parsing, 'クエスト外の与ダメ件数（別枠）を表示', 'show_outside_hits')
        parsing.add_separator()
        parsing.add_command(label='取得状態・品質を確認', command=self.show_diagnostics)
        parsing.add_separator()
        parsing.add_command(label='攻撃名CSVを開く', command=self.open_attack_csv)
        parsing.add_command(label='攻撃名CSVを再読込', command=self.reload_attack_names)
        parsing.add_command(label='除外する攻撃CSVを開く', command=self.open_exclusion_csv)
        parsing.add_command(label='除外する攻撃CSVを再読込', command=self.reload_exclusions)
        parsing.add_command(label='クエスト名CSVを開く', command=self.open_quest_csv)
        parsing.add_command(label='クエスト名CSVを再読込', command=self.reload_quest_names)
        setup = self.setup_menu = self._menu('設定')
        setup.add_command(label='PSO2フォルダーを選択・DLLを配置…', command=self.choose_game_folder)
        setup.add_command(label='追加したDLLを撤去', command=self.remove_loader)
        setup.add_separator()
        self._check(setup, 'クエスト毎に自動計測', 'auto_quests')
        self._check(setup, 'Timeを小数点まで表示', 'time_decimals')
        self._check(setup, '計測終了後にTXTを自動出力', 'auto_text_export')
        display = self._menu('表示')
        self._check(display, '常に手前に表示', 'topmost')
        self._check(display, 'ダメージバー', 'bars')
        self._check(display, '自分を強調', 'highlight_self')
        display.add_separator()
        display.add_command(label='テーマカラー（RGB）…', command=self.choose_theme)
        display.add_command(label='背景画像を選択…', command=self.choose_background)
        display.add_command(label='背景画像をデフォルトに戻す', command=self.reset_background)
        display.add_command(label='外観をデフォルトに戻す', command=self.reset_appearance)
        display.add_separator()
        columns = tk.Menu(display, tearoff=False, bg=PANEL, fg=TEXT)
        display.add_cascade(label='表示する列', menu=columns)
        display.add_command(label='列幅を元に戻す', command=self.reset_column_widths)
        self.column_vars = {}
        for key, label, _ in COLUMNS:
            variable = tk.BooleanVar(value=key in self.settings.columns)
            self.column_vars[key] = variable
            columns.add_checkbutton(label=label, variable=variable,
                command=self._columns_changed, state='disabled' if key == 'name' else 'normal')
        opacity = tk.Menu(display, tearoff=False, bg=PANEL, fg=TEXT)
        display.add_cascade(label='不透明度', menu=opacity)
        for value in (1.0, .9, .75, .5):
            opacity.add_command(label=f'{value:.0%}', command=lambda value=value: self.set_opacity(value))
        sizes = tk.Menu(display, tearoff=False, bg=PANEL, fg=TEXT)
        display.add_cascade(label='文字サイズ', menu=sizes)
        for value in (8, 9, 10, 11, 12):
            sizes.add_command(label=str(value), command=lambda value=value: self.set_font(value))
        display.add_command(label='ウィンドウサイズを戻す', command=lambda: self.root.geometry('670x275'))
        logs = self._menu('ログ')
        logs.add_command(label='履歴から開く…', command=self.open_history)
        logs.add_command(label='保存ログを開く…', command=self.open_log)
        logs.add_separator()
        logs.add_command(label='最新の状態へ戻る', command=self.follow_live)
        logs.add_separator()
        logs.add_command(label='一覧をコピー', accelerator='Ctrl+C', command=self.copy)
        logs.add_command(label='集計をJSONへ保存…', command=self.export)
        logs.add_command(label='計測結果をTXTへ保存…', command=self.export_text)
        logs.add_command(label='TXT出力フォルダーを開く', command=self.open_export_folder)
        logs.add_command(label='元データの場所を開く', command=self.open_folder)

    def _check(self, menu, label, key):
        variable = tk.BooleanVar(value=getattr(self.settings, key))
        self.variables[key] = variable
        menu.add_checkbutton(label=label, variable=variable, command=lambda: self.toggle(key, variable.get()))

    def _theme_widgets(self, widget):
        # Remember semantic defaults once; RGB values can coincide in a custom
        # palette, so reverse-mapping the current colors would lose their roles.
        roles = getattr(widget, '_lazyparse_color_roles', None)
        if roles is None:
            roles = {}
            for option in ('background', 'foreground', 'activebackground', 'activeforeground',
                           'disabledforeground', 'insertbackground', 'selectbackground', 'selectforeground',
                           'selectcolor'):
                if option not in widget.keys() or option == 'selectcolor' and not isinstance(widget, tk.Menu):
                    continue
                value = str(widget.cget(option)).lower()
                if option == 'selectcolor':
                    # Menu check marks default to the system's black menu text,
                    # which only shows on the hover color; draw them as text.
                    roles[option] = TEXT
                elif value in self.colors:
                    roles[option] = value
                elif value.startswith('system'):
                    roles[option] = TEXT if 'foreground' in option else PANEL if 'active' in option else BG
            widget._lazyparse_color_roles = roles
        if roles:
            widget.configure(**{option: self.colors[role] for option, role in roles.items()})
        for child in widget.winfo_children():
            self._theme_widgets(child)

    def _apply_theme(self):
        self.colors = palette(self.settings.theme_background, self.settings.theme_accent)
        self._style()
        self._theme_widgets(self.root)

    def _load_background(self):
        try:
            self.background.load(self.settings.background_image, self.settings_path.parent)
        except (OSError, ValueError) as error:
            self.last_error = str(error)
            self.notify('背景画像を読み込めません。「表示」から選び直すか、デフォルトに戻してください。')

    def _commit_appearance(self, background, accent, image):
        previous = (self.settings.theme_background, self.settings.theme_accent, self.settings.background_image)
        self.settings.theme_background, self.settings.theme_accent, self.settings.background_image = background, accent, image
        try:
            self.settings.save(self.settings_path)
        except OSError as error:
            self.settings.theme_background, self.settings.theme_accent, self.settings.background_image = previous
            messagebox.showerror('外観を保存できません', str(error), parent=self.root)
            return False
        self._apply_theme()
        self._load_background()
        if previous[2] and previous[2] != image:
            remove_background(previous[2], self.settings_path.parent)
        self.render()
        return True

    def choose_theme(self):
        window = tk.Toplevel(self.root)
        window.title('テーマカラー（RGB）')
        window.resizable(False, False)
        window.transient(self.root)
        rows = []
        for index, (label, value) in enumerate((('背景・パネル', self.settings.theme_background),
                                               ('アクセント', self.settings.theme_accent))):
            tk.Label(window, text=label, bg=BG, fg=TEXT).grid(row=index*2, column=0, padx=12, pady=8, sticky='w')
            variables = [tk.StringVar(window, str(v)) for v in rgb(value)]
            rows.append(variables)
            for column, (channel, variable) in enumerate(zip(('R', 'G', 'B'), variables), 1):
                frame = tk.Frame(window, bg=BG)
                frame.grid(row=index*2, column=column, padx=4)
                tk.Label(frame, text=channel, bg=BG, fg=TEXT).pack(side='left')
                tk.Spinbox(frame, from_=0, to=255, width=4, textvariable=variable,
                           bg=PANEL, fg=TEXT, insertbackground=TEXT).pack(side='left')
            def pick(variables=variables):
                try:
                    initial = rgb_color([v.get() for v in variables])
                except ValueError:
                    initial = DEFAULT_ACCENT
                _, chosen = colorchooser.askcolor(initialcolor=initial, parent=window, title='カラーを選択')
                if chosen:
                    for variable, value in zip(variables, rgb(chosen)):
                        variable.set(str(value))
            self._button(window, '色を選択…', pick).grid(row=index*2, column=4, padx=12)
        def apply():
            try:
                background, accent = (rgb_color([v.get() for v in row]) for row in rows)
            except ValueError as error:
                messagebox.showerror('RGBを確認してください', str(error), parent=window)
                return
            if self._commit_appearance(background, accent, self.settings.background_image):
                window.destroy()
        def defaults():
            for row, color in zip(rows, (DEFAULT_BACKGROUND, DEFAULT_ACCENT)):
                for variable, value in zip(row, rgb(color)):
                    variable.set(str(value))
        buttons = tk.Frame(window, bg=BG)
        buttons.grid(row=5, column=0, columnspan=5, pady=12)
        for label, callback in (('デフォルトの色', defaults), ('適用', apply), ('キャンセル', window.destroy)):
            self._button(buttons, label, callback).pack(side='left', padx=5)
        self._theme_widgets(window)
        window.grab_set()
        return window

    def choose_background(self):
        path = filedialog.askopenfilename(parent=self.root, title='背景画像を選択',
            filetypes=[('画像', '*.png *.jpg *.jpeg *.bmp *.gif *.webp'), ('すべてのファイル', '*.*')])
        if not path:
            return
        try:
            name = import_background(path, self.settings_path.parent)
        except (OSError, ValueError) as error:
            messagebox.showerror('背景画像を読み込めません', str(error), parent=self.root)
            return
        if not self._commit_appearance(self.settings.theme_background, self.settings.theme_accent, name):
            if name != self.settings.background_image:
                remove_background(name, self.settings_path.parent)

    def reset_background(self):
        self._commit_appearance(self.settings.theme_background, self.settings.theme_accent, '')

    def reset_appearance(self):
        self._commit_appearance(DEFAULT_BACKGROUND, DEFAULT_ACCENT, '')

    def _scrolled(self, first, last):
        self.scrollbar.set(first, last)
        if self.table.find_withtag('background'):
            self.table.coords('background', 0, self.table.canvasy(0))

    def ensure_loader(self):
        if self.settings.game_directory:
            self.backend.submit('CheckLoader', path=self.settings.game_directory)
        else:
            self.choose_game_folder()

    def choose_game_folder(self):
        if self.busy:
            return
        folder = filedialog.askdirectory(parent=self.root, title='PSO2の pso2_bin/sub フォルダーを選択',
            initialdir=self.settings.game_directory or None, mustexist=True)
        if folder:
            self.busy = True
            self.backend.submit('InstallLoader', path=folder)
        else:
            self.notify('DLLを配置していません。「設定」から再選択できます。保存ログの閲覧は可能です。')

    def remove_loader(self):
        if not self.busy:
            self.busy = True
            self.backend.submit('RemoveLoader')

    def toggle(self, key, value=None):
        setattr(self.settings, key, not getattr(self.settings, key) if value is None else value)
        self.variables[key].set(getattr(self.settings, key))
        if key == 'topmost':
            self.root.attributes('-topmost', self.settings.topmost)
        elif key == 'auto_quests':
            self.backend.submit('AutoOn' if self.settings.auto_quests else 'AutoOff')
        self._sync_text_options()
        self.render()

    def _sync_text_options(self):
        self.backend.text_options = (self.settings.auto_text_export, self.settings.dps_mode,
                                     self.settings.anonymize, self.settings.time_decimals)

    def set_opacity(self, value):
        self.settings.opacity = value
        self.root.attributes('-alpha', value)

    def set_dps_mode(self, mode):
        if mode not in DPS_MODES:
            raise ValueError('Unknown DPS mode')
        self.settings.dps_mode = mode
        self.variables['dps_mode'].set(mode)
        self._sync_text_options()
        self.render()

    def open_quest_csv(self):
        try:
            os.startfile(QUESTS_CSV)
        except OSError as error:
            self.last_error = str(error)
            self.notify(f'クエスト名CSVを開けませんでした: {QUESTS_CSV}')

    def reload_quest_names(self):
        get_quest_catalog.cache_clear()
        catalog = get_quest_catalog()
        self.notify('クエスト名CSVを再読込しました。' if not catalog.error else 'クエスト名CSVを確認してください: ' + catalog.error)
        self.render()

    def reload_attack_names(self):
        get_catalog.cache_clear()
        catalog = get_catalog()
        self.render()
        self.notify('攻撃名CSVを再読込しました。' if not catalog.error else
                    '攻撃名CSVに問題があります。取得状態・品質の辞書エラーを確認してください。')

    def open_exclusion_csv(self):
        try:
            os.startfile(EXCLUSIONS_PATH)
        except OSError as error:
            self.last_error = str(error)
            self.notify(f'除外する攻撃CSVを開けませんでした: {EXCLUSIONS_PATH}')

    def reload_exclusions(self):
        # Each measurement reads the file when it starts, so the running one
        # keeps its setting; reopening a saved log re-aggregates with the new one.
        ids, metadata = read_damage_exclusions()
        if metadata['error']:
            self.last_error = metadata['error']
            self.notify('除外する攻撃CSVに問題があるため、初期設定（攻撃ID 0 のみ）を使います: ' + metadata['error'])
        else:
            self.notify(f'除外する攻撃CSVを確認しました（{len(ids)}件）。次の計測と、開き直した保存ログから反映されます。')
        self.render()

    def open_attack_csv(self):
        try:
            os.startfile(EDITABLE_CSV)
        except OSError as error:
            self.last_error = str(error)
            self.notify(f'攻撃名CSVを開けませんでした: {EDITABLE_CSV}')

    def set_font(self, value):
        self.settings.font_size = value
        self.render()

    def _columns_changed(self):
        self.settings.columns = [key for key, _, _ in COLUMNS if self.column_vars[key].get()]
        self.render()

    def _consume(self):
        if self.closed:
            return
        changed = False
        try:
            self.latest = self.backend.updates.get_nowait()
            self.live_update_pending = self.saved is None
        except queue.Empty:
            pass
        while True:
            try:
                event = self.backend.events.get_nowait()
            except queue.Empty:
                break
            self.busy = False
            changed = True
            if event['type'] == 'opened':
                self.saved = event['snapshot']
                saved_report = (self.saved.get('measurement') or {}).get('report', {})
                selected_report = with_dps_mode(saved_report, self.settings.dps_mode)
                if display_elapsed(selected_report) is None:
                    self.notify('選択した方式の時間情報がありません。元OPCAPを開いてください。')
                else:
                    self._clear_notice()
            else:
                if event.get('message'):
                    self.notify(event['message'])
                if event['type'] == 'loader_installed':
                    self.settings.game_directory = event['folder']
                    try:
                        self.settings.save(self.settings_path)
                    except OSError as error:
                        messagebox.showerror('設定を保存できません', str(error), parent=self.root)
                elif event['type'] == 'loader_missing':
                    messagebox.showerror('取得DLLの配置を確認してください', event['message'], parent=self.root)
                    self.root.after_idle(self.choose_game_folder)
                elif event['type'] == 'loader_reselect':
                    messagebox.showinfo('LazyParseの更新', event['message'], parent=self.root)
                    self.root.after_idle(self.choose_game_folder)
                elif event['type'] == 'game_version_error':
                    self.last_error = event['message']
                    messagebox.showerror('PSO2のゲームDLLを確認してください', event['message'], parent=self.root)
                if event['type'] == 'error':
                    self.last_error = event['message']
                    if event.get('loader_action'):
                        messagebox.showerror('取得DLLの配置を確認してください', event['message'], parent=self.root)
        if self.live_update_pending and self.saved is None:
            startup_error = preparation_error(self.latest)
            if startup_error and startup_error != self.last_preparation_error:
                self.last_error = startup_error
                self.notify(startup_error)
                changed = True
            self.last_preparation_error = startup_error
            now = time.monotonic()
            if (changed or self.settings.time_decimals or self.last_live_display is None or
                    now - self.last_live_display >= 1 or
                    self._display_state(self.latest) != self._display_state(self.snapshot)):
                self.last_live_display = now
                self.live_update_pending = False
                changed = True
        if changed:
            self.snapshot = self.saved if self.saved is not None else self.latest
            self.render()
        self.consume_timer = self.root.after(100, self._consume)

    @staticmethod
    def _display_state(snapshot):
        measurement = snapshot.get('measurement') or {}
        report = measurement.get('report') or {}
        quest = report.get('quest_measurement') or {}
        return (status_view(snapshot), snapshot.get('state'), snapshot.get('effective_recording'),
                snapshot.get('worker_verified'), snapshot.get('heartbeat_fresh'),
                measurement.get('capture_path'), measurement.get('quest_id'), measurement.get('complete'),
                report.get('stopped'), quest.get('id'), quest.get('end_reason'))

    def notify(self, text):
        self.notice = text
        if self.notice_timer:
            self.root.after_cancel(self.notice_timer)
        self.notice_timer = self.root.after(7000, self._clear_notice)

    def _clear_notice(self):
        self.notice = ''
        self.notice_timer = None
        self.render()

    def _layout(self):
        widths = self.settings.column_widths
        columns = [(key, title, widths.get(key, width)) for key, title, width in COLUMNS if key in self.settings.columns]
        width = max(1, self.table.winfo_width())
        total = sum(value for _, _, value in columns)
        x = 0
        result = []
        for key, title, value in columns:
            end = x + width * value / total
            result.append((key, title, x, end))
            x = end
        return result

    @staticmethod
    def _elide(text, available, current_font):
        if current_font.measure(text) <= available:
            return text
        while text and current_font.measure(text + '…') > available:
            text = text[:-1]
        return text + '…' if text else ''

    def render(self):
        if self.closed:
            return
        snapshot = self.snapshot
        measurement = snapshot.get('measurement') or {}
        self.report = with_dps_mode(with_attack_names(measurement.get('report') or {}), self.settings.dps_mode)
        if snapshot.get('identity') and not self.saved:
            self.report['identity_state'] = snapshot['identity']['state']
        self.rows = actor_rows(self.report, players_only=self.settings.players_only, only_me=self.settings.only_me,
            anonymize=self.settings.anonymize, sort=self.sort_key, descending=self.descending,
            current_identities=self.report.get('identity_state') if self.saved is None and
                (snapshot.get('identity') or {}).get('is_live') else None)
        label, color = status_view(snapshot, saved=self.saved is not None)
        self.status.configure(text='● ' + ('操作中' if self.busy else label), fg=color)
        self.indicator.configure(bg=status_band_color(label, color, self.settings.theme_accent))
        runtime = self.latest.get('runtime') or {}
        ready = self.saved is None and not self.busy and self.latest.get('worker_verified') and self.latest.get('heartbeat_fresh')
        can_start = ready and not self.latest.get('game_version_error')
        requested = runtime.get('desired_recording', False)
        self.start_button.configure(state='normal' if can_start and not requested and runtime.get('identity_observing') and
                                    not runtime.get('identity_context_error') and self.latest.get('state') != 'shutdown' else 'disabled')
        self.stop_button.configure(state='normal' if ready and requested else 'disabled')
        self.header.delete('all')
        self.table.delete('all')
        self.background.overlays.clear()
        c = self.colors
        width = max(1, self.table.winfo_width())
        photo = self.background.image(self.table, (width, max(1, self.table.winfo_height())), c[BG])
        if photo:
            self.table.create_image(0, self.table.canvasy(0), image=photo, anchor='nw', tags='background')
        size = self.settings.font_size
        if size not in self.fonts:
            self.fonts[size] = (font.Font(self.root, family='Meiryo UI', size=size),
                                font.Font(self.root, family='Meiryo UI', size=size, weight='bold'))
        current_font, bold = self.fonts[size]
        self.row_height = max(20, current_font.metrics('linespace') + 5)
        layout = self._layout()
        for key, title, x, end in layout:
            if key == self.sort_key:
                title += ' ▾' if self.descending else ' ▴'
            title = self._elide(title, max(1, end - x - 10), current_font)
            self.header.create_text(x + 6 if key == 'name' else end - 6, 11, text=title, fill=c[MUTED],
                                    anchor='w' if key == 'name' else 'e', font=current_font)
        self.header.create_line(0, 22, self.header.winfo_width(), 22, fill=c[LINE])
        highest = max((row['damage_dealt'] for row in self.rows), default=1) or 1
        width = self.table.winfo_width()
        for index, row in enumerate(self.rows):
            y = index * self.row_height
            own = row['role'] == 'self' and self.settings.highlight_self
            base = '#102b2d' if own else '#111619' if index % 2 == 0 else BG
            def rectangle(end, color, opacity):
                if photo:
                    overlay = self.background.overlay(self.table, (max(1, round(end)), self.row_height), color, opacity)
                    self.table.create_image(0, y, image=overlay, anchor='nw')
                else:
                    self.table.create_rectangle(0, y, end, y + self.row_height, fill=color, outline='')
            rectangle(width, c[base], .25)
            if self.settings.bars and row['damage_dealt']:
                rectangle(width * row['damage_dealt'] / highest,
                    c['#19565a' if own else '#103c40' if row['kind'] == 'player' else '#252c31'], .50)
            if self.selected == row['key']:
                self.table.create_rectangle(1, y + 1, width - 1, y + self.row_height - 1, outline=c['#4a878a'])
            for key, _, x, end in layout:
                value = row.get(key)
                text = row['name'] if key == 'name' else f"{row['share']:.1f}" if key == 'share' else str(value) if key == 'attack' else number(value, 1 if key == 'dps' else 0)
                text = self._elide(text, max(1, end - x - 10), current_font)
                self.table.create_text(x + 6 if key == 'name' else end - 6, y + self.row_height / 2,
                    text=text, anchor='w' if key == 'name' else 'e', fill=c[ACCENT] if own and key == 'name' else c[TEXT],
                    font=bold if own and key == 'name' else current_font)
        if not self.rows and measurement:
            self.table.create_text(width / 2, 38, text='ダメージを待っています。', fill=c[MUTED], font=current_font)
        self.table.configure(scrollregion=(0, 0, width, len(self.rows) * self.row_height))
        if len(self.rows) * self.row_height > self.table.winfo_height():
            if not self.scrollbar.winfo_manager():
                self.scrollbar.pack(side='right', fill='y', before=self.table)
        elif self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()
        elapsed = display_elapsed(self.report)
        if is_restricted(self.report):
            # Only the player's own row is listed; the bar still shows everyone.
            total = party_total(self.report, self.settings.players_only)
            hits = sum(actor['hits_dealt'] for actor in self.report.get('actors', [])
                       if not self.settings.players_only or actor['kind'] == 'player')
        else:
            total = sum(row['damage_dealt'] for row in self.rows)
            hits = sum(row['hits_dealt'] for row in self.rows)
        dps = total / elapsed if elapsed else None
        flags = self.report.get('quality', {}).get('flags', [])
        text = self.notice or f"Time {elapsed_label(elapsed, self.settings.time_decimals)} s    Total {number(total)}    DPS {number(dps, 1)}    Hits {hits}"
        if flags and not self.notice:
            text += f'    ⚠ {len(flags)}'
        if self.report.get('quest_measurement', {}).get('kind') == 'quest' and not self.notice:
            text += '    ' + quest_label(self.report)
        outside = (snapshot.get('quest_view') or {}).get('outside_player_hits', 0)
        if outside and self.settings.show_outside_hits and measurement.get('quest_id') and not self.notice:
            text += f'    別枠 {outside}件'
        self.summary.configure(text=text)
        for key, detail in list(self.details.items()):
            if not detail.window.winfo_exists():
                del self.details[key]
                continue
            row = next((row for row in self.rows if row['key'] == key), None)
            if row:
                detail.refresh(self.report, row)

    def _select(self, event):
        index = int(self.table.canvasy(event.y) // self.row_height)
        if 0 <= index < len(self.rows):
            self.selected = self.rows[index]['key']
            self.render()

    def _open_selected(self, event=None):
        if event:
            self._select(event)
        actor = next((row for row in self.rows if row['key'] == self.selected), None)
        if actor:
            detail = self.details.get(actor['key'])
            if detail and detail.window.winfo_exists():
                detail.window.lift()
            else:
                self.details[actor['key']] = DetailWindow(self.root, self.report, actor)
                self._theme_widgets(self.details[actor['key']].window)

    def _row_menu(self, event):
        self._select(event)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label='攻撃別の詳細', command=self._open_selected)
        menu.add_command(label='一覧をコピー', command=self.copy)
        menu.add_command(label='計測結果をTXTへ保存…', command=self.export_text)
        self._theme_widgets(menu)
        menu.tk_popup(event.x_root, event.y_root)

    def _sort(self, event):
        for key, _, x, end in self._layout():
            if x <= event.x < end:
                self.descending = not self.descending if self.sort_key == key else key != 'name'
                self.sort_key = key
                self.render()
                break

    def _column_border(self, x):
        """Index of the column whose right edge is under x (the last edge is the window's)."""
        layout = self._layout()
        for index, (_, _, _, end) in enumerate(layout[:-1]):
            if abs(x - end) <= COLUMN_GRIP:
                return index
        return None

    def _header_press(self, event):
        self.tooltip.hide()
        index = self._column_border(event.x)
        if index is None:
            self.column_drag = None
            self._sort(event)
            return
        layout = self._layout()
        total = sum(self.settings.column_widths.get(key, width) for key, _, width in COLUMNS
                    if key in self.settings.columns)
        scale = layout[-1][3] / total
        (left, left_title, start, _), (right, right_title, _, end) = layout[index], layout[index + 1]
        self.column_drag = (left, right, start, end, scale,
                            self._column_min_width(left_title), self._column_min_width(right_title))

    def _column_min_width(self, title):
        """Narrowest width that still shows the header title and a sort arrow."""
        current_font = self.fonts.get(self.settings.font_size, (None,))[0]
        if current_font is None:
            return COLUMN_MIN_WIDTH
        return max(COLUMN_MIN_WIDTH, current_font.measure(title + ' ▾') + 12)

    def _header_drag(self, event):
        if not self.column_drag:
            return
        left, right, start, end, scale, left_min, right_min = self.column_drag
        if end - start < left_min + right_min:
            return
        border = min(max(event.x, start + left_min), end - right_min)
        # The pair keeps its combined width, so the other columns stay put.
        self.settings.column_widths[left] = (border - start) / scale
        self.settings.column_widths[right] = (end - border) / scale
        self.render()

    def _header_release(self, event):
        self.column_drag = None

    def reset_column_widths(self):
        self.settings.column_widths = {}
        self.render()

    def _header_tip(self, event):
        self.header.configure(cursor='sb_h_double_arrow' if self.column_drag or
                              self._column_border(event.x) is not None else '')
        if self.column_drag:
            return
        for key, _, x, end in self._layout():
            if x <= event.x < end:
                if key in {'ja', 'crit'}:
                    message = ('JA成立ヒット数 / JA対象ヒット数。ペット・タクト通常・設定した除外IDは対象外。' if key == 'ja' else
                               'Crit成立ヒット数 / 正値ヒット数。所有者へ帰属したペット攻撃も含みます。')
                    message += '\n対象0件や判定情報がない場合は—。行にカーソルを合わせると件数を表示します。'
                elif key == 'dps':
                    message = ('この方式の時間情報がありません。元OPCAPを開いて再集計してください。' if display_elapsed(self.report) is None else
                               '正値ダメージの最初～最後の区間を合算して割ります。10秒以上の空白で別区間になります。' if self.settings.dps_mode == 'damage' else
                               '最初の与ダメージから計測終了までの連続時間で割ります。無攻撃・移動時間も含みます。')
                elif key == 'attack':
                    message = '最大ヒットの攻撃名。行にカーソルを合わせると名前・ID・種別を表示します。'
                else:
                    break
                self.tooltip.show(message, event.x_root, event.y_root)
                return
        self.tooltip.hide()

    def _header_menu(self, event):
        self.tooltip.hide()
        menu = tk.Menu(self.root, tearoff=False)
        for key, label, _ in COLUMNS:
            menu.add_checkbutton(label=label, variable=self.column_vars[key], command=self._columns_changed,
                                 state='disabled' if key == 'name' else 'normal')
        menu.add_separator()
        menu.add_command(label='列幅を元に戻す', command=self.reset_column_widths)
        self._theme_widgets(menu)
        menu.tk_popup(event.x_root, event.y_root)

    def _row_tip(self, event):
        index = int(self.table.canvasy(event.y) // self.row_height)
        for key, _, x, end in self._layout():
            if key in {'ja', 'crit'} and x <= event.x < end and 0 <= index < len(self.rows):
                self.tooltip.show(rate_tooltip(self.rows[index], key), event.x_root, event.y_root)
                return
            if key == 'attack' and x <= event.x < end and 0 <= index < len(self.rows):
                row = self.rows[index]
                attack_id = row['max_attack_id']
                if attack_id is not None:
                    labels = get_catalog().labels(attack_id)
                    self.tooltip.show(f"{row['attack']}\nID {attack_id} / 0x{attack_id:08X}\n"
                        f"{labels['attack_kind'] or '種別未登録'} · {labels['attack_note'] or ''}", event.x_root, event.y_root)
                    return
        self.tooltip.hide()

    def command(self, action):
        if self.saved is not None:
            self.notify('保存ログを表示中です。「ログ → 最新の状態へ戻る」で切り替えてください。')
            return
        runtime = self.latest.get('runtime') or {}
        if startup_error := preparation_error(self.latest):
            self.last_error = startup_error
            self.notify(startup_error)
            return
        if action in {'Start', 'Next', 'Reset'} and self.latest.get('game_version_error'):
            self.notify(self.latest['game_version_error'])
            return
        if self.busy or not self.latest.get('worker_verified') or not self.latest.get('heartbeat_fresh') or self.latest.get('state') == 'shutdown':
            self.notify('取得ワーカーの準備を待っています。')
            return
        if action in {'Next', 'Reset'} and not runtime.get('desired_recording'):
            self.notify('区切り・リセットは記録中に使えます。')
            return
        self.busy = True
        self.backend.submit(action, snapshot=deepcopy(self.latest))
        self.render()

    def open_log(self, path=None):
        path = path or filedialog.askopenfilename(parent=self.root, title='保存ログを開く',
            filetypes=[('LazyParse logs', '*.opcap *.json'), ('All files', '*.*')])
        if path:
            self.busy = True
            self.backend.submit('Open', path=path)

    def follow_live(self):
        self.saved = None
        self.selected = None
        self.snapshot = self.latest
        self._clear_notice()
        self.render()

    def open_history(self):
        window = tk.Toplevel(self.root)
        window.title('記録の履歴')
        window.configure(bg=BG)
        window.geometry('620x300')
        history = list(reversed(self.latest.get('history', [])))
        quest_history = list(reversed(self.latest.get('quest_history', [])))
        if self.saved and self.saved.get('quest_view'):
            history = []
            quest_history = [{'report': report, 'label':
                (quest_label(report) if report['quest_measurement']['kind'] == 'quest' else 'クエスト外') +
                f" #{report['quest_measurement']['number']}",
                'complete': report['quest_measurement']['stream_finalized']}
                for report in reversed(self.saved['quest_view']['history'])]
        history = [{'run_name': entry['label'], 'segment_number': '自動',
                    'roles': entry['report'].get('roles', {}),
                    'capture_path': entry.get('report_path'), 'complete': entry['complete'],
                    'display_state': '待機中' if entry['report']['quest_measurement']['end_reason'] else '計測中',
                    'report': entry['report']} for entry in quest_history] + history
        known = {entry.get('capture_path') for entry in history}
        quest_keys = {history_key(entry.get('report'), entry.get('capture_path')) for entry in history}
        directory = getattr(self.backend, 'log_directory', STATE / 'logs')
        history += [entry for entry in stored_history(directory) if entry['capture_path'] not in known
                    and entry['history_key'] not in quest_keys]
        tree = ttk.Treeview(window, columns=('run', 'segment', 'damage', 'state'), show='headings', style='LazyParse.Treeview')
        for key, text, width in [('run', 'Run', 290), ('segment', 'Segment', 80), ('damage', 'Damage', 100), ('state', '保存', 100)]:
            tree.heading(key, text=text)
            tree.column(key, width=width)
        for index, entry in enumerate(history):
            role = entry.get('roles', {}).get('self')
            damage = dps_damage(role) if role else 0
            tree.insert('', 'end', iid=str(index), values=(entry['run_name'], entry['segment_number'], number(damage),
                entry.get('display_state') or ('完了' if entry.get('complete') else '未完了')))
        tree.pack(fill='both', expand=True)
        def opened(event):
            selected = tree.selection()
            if selected:
                entry = history[int(selected[0])]
                if entry.get('capture_path'):
                    self.open_log(entry['capture_path'])
                elif entry.get('report'):
                    self.saved = {'state': 'saved_log', 'measurement': {'report': entry['report']}}
                    self.snapshot = self.saved
                    self.render()
                window.destroy()
        tree.bind('<Double-Button-1>', opened)
        def popup(event):
            item = tree.identify_row(event.y)
            if not item:
                return
            tree.selection_set(item)
            entry = history[int(item)]
            menu = tk.Menu(window, tearoff=False)
            menu.add_command(label='開く', command=lambda: opened(None))
            menu.add_command(label='計測結果をTXTへ保存…', command=lambda: self.export_text(entry=entry, parent=window))
            self._theme_widgets(menu)
            menu.tk_popup(event.x_root, event.y_root)
        tree.bind('<Button-3>', popup)
        if not history:
            tk.Label(window, text='この監視中に確定した記録はありません。保存ログはメニューから開けます。').pack()
        self._theme_widgets(window)

    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(clipboard_summary(self.report, self.rows, time_decimals=self.settings.time_decimals))
        self.notify('一覧をクリップボードへコピーしました。')

    def export_text(self, *, entry=None, parent=None):
        report = entry.get('report') if entry is not None else self.report
        source = entry.get('capture_path') if entry is not None else None
        if not report and not source:
            self.notify('保存する集計がありません。')
            return
        try:
            EXPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.last_error = str(error)
            self.notify('TXT出力フォルダーを作成できませんでした。')
            return
        proposed = filename(report or {})
        path = filedialog.asksaveasfilename(parent=parent or self.root, title='計測結果をTXTへ保存',
            initialdir=str(EXPORT_DIRECTORY), initialfile=proposed, defaultextension='.txt',
            filetypes=[('テキスト', '*.txt')])
        if path:
            self.busy = True
            requested = {'measurement': {'report': deepcopy(report)}, 'export_source': source}
            self.backend.submit('ExportText', snapshot=requested, path=path)

    def open_export_folder(self):
        try:
            EXPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
            os.startfile(EXPORT_DIRECTORY)
        except OSError as error:
            self.last_error = str(error)
            self.notify('TXT出力フォルダーを開けませんでした。')

    def export(self):
        if not self.report:
            self.notify('保存する集計がありません。')
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='集計を保存', defaultextension='.json',
                                           filetypes=[('JSON', '*.json')])
        if path:
            try:
                with Path(path).open('x', encoding='utf-8') as stream:
                    json.dump(self.report, stream, ensure_ascii=False, indent=2)
                self.notify('集計を保存しました。')
            except OSError as error:
                self.last_error = str(error)
                self.notify('保存できませんでした。既存ファイルと別の名前を指定してください。')

    def open_folder(self):
        path = (self.snapshot.get('measurement') or {}).get('capture_path')
        if path and os.name == 'nt':
            os.startfile(str(Path(path).parent))

    def show_diagnostics(self):
        window = tk.Toplevel(self.root)
        window.title('取得状態・品質')
        window.geometry('640x390')
        text = tk.Text(window, bg=BG, fg=TEXT, wrap='word', font=('Consolas', 9))
        text.pack(fill='both', expand=True)
        text.insert('end', json.dumps({'state': self.snapshot.get('state'), 'capture': (self.snapshot.get('measurement') or {}).get('capture_path'),
            'quality': self.report.get('quality'), 'attack_catalog': get_catalog().metadata(), 'pending': self.snapshot.get('pending'),
            'attack_exclusions': (self.report.get('aggregation_policy') or {}).get('configured_attack_exclusions'),
            'quest_measurement': self.report.get('quest_measurement'),
            'outside_quest_records': (self.snapshot.get('quest_view') or {}).get('outside_records'),
            'outside_quest_player_hits': (self.snapshot.get('quest_view') or {}).get('outside_player_hits'),
            'auto_save_error': self.snapshot.get('auto_save_error'),
            'text_export_error': self.snapshot.get('text_export_error'),
            'quest_catalog_error': get_quest_catalog().error,
            'worker_verified': self.latest.get('worker_verified'), 'worker_probe': self.latest.get('worker_probe'),
            'heartbeat_fresh': self.latest.get('heartbeat_fresh'), 'state_error': self.latest.get('error'),
            'runtime_heartbeat_fresh': self.latest.get('runtime_heartbeat_fresh'),
            'runtime_worker_verified': self.latest.get('runtime_worker_verified'),
            'runtime_worker_probe': self.latest.get('runtime_worker_probe'),
            'preparation_error': preparation_error(self.latest),
            'runtime': self.latest.get('runtime'), 'last_error': getattr(self, 'last_error', None)}, ensure_ascii=False, indent=2))
        text.configure(state='disabled')
        self._theme_widgets(window)

    def _drag_start(self, event):
        self.drag = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event):
        if self.drag:
            x, y, left, top = self.drag
            self.root.geometry(f'+{max(0, left + event.x_root - x)}+{max(0, top + event.y_root - y)}')

    def _resize_start(self, event):
        self.resize = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, event):
        x, y, width, height = self.resize
        self.root.geometry(f'{max(MIN_WIDTH, width + event.x_root - x)}x{max(MIN_HEIGHT, height + event.y_root - y)}')

    def _install_edge_resize(self):
        """Resize from every border and corner like a framed window.

        The window has no system frame, so a bindtag placed before each
        widget's own bindings claims presses within EDGE pixels of the border
        (the footer's diagnostics click, the title bar drag, ...).
        """
        pending = [self.root]
        while pending:
            widget = pending.pop()
            pending.extend(child for child in widget.winfo_children()
                           if not isinstance(child, (tk.Toplevel, tk.Menu)))
            if EDGE_TAG not in widget.bindtags():
                widget.bindtags((EDGE_TAG,) + widget.bindtags())
        self.root.bind_class(EDGE_TAG, '<Motion>', self._edge_hover)
        self.root.bind_class(EDGE_TAG, '<ButtonPress-1>', self._edge_press)
        self.root.bind_class(EDGE_TAG, '<B1-Motion>', self._edge_drag)
        self.root.bind_class(EDGE_TAG, '<ButtonRelease-1>', self._edge_release)

    def _edges(self, event):
        x, y = event.x_root - self.root.winfo_rootx(), event.y_root - self.root.winfo_rooty()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        vertical = 'n' if y < EDGE else 's' if y >= height - EDGE else ''
        horizontal = 'w' if x < EDGE else 'e' if x >= width - EDGE else ''
        return vertical + horizontal

    def _edge_hover(self, event):
        if not self.edge_resize:
            self.root.configure(cursor=EDGE_CURSORS[self._edges(event)])

    def _edge_press(self, event):
        edges = self._edges(event)
        if not edges:
            return None
        self.tooltip.hide()
        self.edge_resize = (edges, event.x_root, event.y_root, self.root.winfo_rootx(), self.root.winfo_rooty(),
                            self.root.winfo_width(), self.root.winfo_height())
        return 'break'

    def _edge_drag(self, event):
        if not self.edge_resize:
            return None
        edges, x_root, y_root, x, y, width, height = self.edge_resize
        dx, dy = event.x_root - x_root, event.y_root - y_root
        new_width = max(MIN_WIDTH, width + dx if 'e' in edges else width - dx if 'w' in edges else width)
        new_height = max(MIN_HEIGHT, height + dy if 's' in edges else height - dy if 'n' in edges else height)
        # Dragging the left/top border keeps the opposite border in place.
        left = x + width - new_width if 'w' in edges else x
        top = y + height - new_height if 'n' in edges else y
        self.root.geometry(f'{new_width}x{new_height}{left:+d}{top:+d}')
        return 'break'

    def _edge_release(self, event):
        if not self.edge_resize:
            return None
        self.edge_resize = None
        self.root.configure(cursor=EDGE_CURSORS[self._edges(event)])
        return 'break'

    def minimize(self):
        self.tooltip.hide()
        minimize_window(self.root)

    def _mapped(self, event):
        # Only the non-Windows fallback drops override-redirect to minimize.
        if event.widget is self.root and self.root.state() == 'normal' and not self.root.overrideredirect():
            self.root.overrideredirect(True)

    def _ensure_visible(self):
        if self.root.winfo_x() > self.root.winfo_screenwidth() - 50 or self.root.winfo_y() > self.root.winfo_screenheight() - 50:
            self.root.geometry('+100+100')

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.tooltip.hide()
        for timer in (self.consume_timer, self.visibility_timer, self.notice_timer):
            if timer:
                self.root.after_cancel(timer)
        self.settings.geometry = self.root.geometry()
        try:
            self.settings.save(self.settings_path)
        except OSError:
            pass
        self.backend.close()
        # Release Tk-owned resources on the UI thread, before a backend thread
        # could collect a closed window's reference cycle.
        self.background.photo = None
        self.background.overlays.clear()
        self.app_icon = None
        self.fonts.clear()
        self.variables.clear()
        self.column_vars.clear()
        self.tooltip.theme = None
        self.root.destroy()
