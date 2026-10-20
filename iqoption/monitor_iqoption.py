import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from time import time
from tkinter import ttk, messagebox

from iqoption.iqoption_client import (ASSET_TYPES, INTERVAL_SECONDS, MAX_ASSETS, IQMultiStream,
                                      IQOptionError, IQSession, display_name)
from binance.monitor_patterns import COLORS, PATTERNS_FILE, STRIP_CANDLES, MonitorApp, normalize_symbol
from core.pattern_detector import (DIRECTIONS, GREEN, PatternDetector, PatternError, candle_color,
                                   ms_to_datetime)


DEFAULT_ASSET      = 'EUR/USD'
DEFAULT_ASSET_TYPE = 'Opcoes binarias'
ASSETS_REFRESH_MS  = 5 * 60 * 1000

# Escolhas da janela (tipo, ativos, tempo, opcoes), restauradas ao abrir. Senha e e-mail nao sao gravados.
SETTINGS_FILE = Path(os.environ.get('IQ_SETTINGS_FILE') or
                     Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') /
                     'monitor_padroes' / 'iqoption.json')


def load_settings():
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

    return settings if isinstance(settings, dict) else {}


def save_settings(settings):
    """Grava num arquivo temporario e troca, para nao deixar um arquivo pela metade."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(SETTINGS_FILE)

# Termometro: cor da barra conforme a fracao do gatilho ja formada (frio -> quente).
HEAT_COLORS = ((0.5, '#3498db'), (0.75, '#f1c40f'), (1.0, '#e67e22'))
HEAT_DONE   = '#f1c40f'

# Cartao de cada ativo: faixa de candles em cima e uma linha de termometro por padrao.
CARD_WIDTH   = 540     # largura minima de um cartao; define quantas colunas cabem na janela
CARD_STRIP   = 40
THERMO_ROW   = 22
CARD_BG      = '#1f2630'
CARD_BORDER  = '#3b4a5c'
CARDS_SHARE  = 0.55    # fracao maxima da altura da janela ocupada pelos cartoes (o resto rola)


WHEEL_EVENTS = ('<MouseWheel>', '<Button-4>', '<Button-5>')


def block_wheel(widget, scroll=None):
    """A roda do mouse sobre o campo nao troca o valor (so a seta ou o teclado trocam).

    Com scroll, a roda continua rolando a area em volta (ex.: os cartoes dos ativos).
    """
    def on_wheel(event):
        if scroll:
            scroll(event)
        return 'break'

    for sequence in WHEEL_EVENTS:
        widget.bind(sequence, on_wheel)


def heat_color(fraction):
    if fraction >= 1:
        return HEAT_DONE

    return next(color for limit, color in HEAT_COLORS if fraction <= limit)


class AssetCard:
    """Um ativo acompanhado: escolha do ativo, ultimos candles e termometro dos padroes."""

    def __init__(self, app, parent, index):
        self.app      = app
        self.index    = index
        self.var      = tk.StringVar()
        self.name     = None    # ativo da procura em andamento
        self.detector = None

        self.frame = tk.Frame(parent, background=CARD_BORDER, padx=2, pady=2)
        body       = ttk.Frame(self.frame, padding=4)
        body.pack(fill='both', expand=True)

        head = ttk.Frame(body)
        head.pack(fill='x', pady=(0, 4))

        ttk.Label(head, text=f'{index + 1}.', font=('TkDefaultFont', 10, 'bold')).pack(side='left')

        self.box = ttk.Combobox(head, textvariable=self.var, width=20)
        self.box.pack(side='left', padx=(4, 8))
        self.box.bind('<KeyRelease>', self._filter)
        self.box.bind('<<ComboboxSelected>>', lambda _event: app._on_card_asset(self))
        self.box.bind('<Return>', lambda _event: app._on_card_asset(self))
        block_wheel(self.box, app._on_wheel)

        self.stats_label = ttk.Label(head)
        self.stats_label.pack(side='right')

        self.status_label = ttk.Label(head, foreground='#8a8f98')
        self.status_label.pack(side='left', fill='x', expand=True)

        self.canvas = tk.Canvas(body, height=CARD_STRIP + len(app.patterns) * THERMO_ROW + 2,
                                background=CARD_BG, highlightthickness=0)
        self.canvas.pack(fill='x')

        self.update_stats()
        self.draw()

    def _filter(self, event):
        if event.keysym in ('Return', 'Up', 'Down', 'Escape'):
            return

        typed = normalize_symbol(self.var.get())
        self.box['values'] = [name for key, name in self.app.symbol_keys.items() if typed in key][:300]

    def set_status(self, text, error=False):
        self.status_label.configure(text=text, foreground='#d35400' if error else '#8a8f98')

    def update_stats(self):
        s = self.app.asset_stats.get(self.name) if self.name else None
        self.stats_label.configure(text=f"Sinais {s['signals']}  W {s['win']}  L {s['loss']}" if s else '')

    def draw(self):
        canvas = self.canvas
        canvas.delete('all')

        patterns = self.app.patterns
        progress = (self.detector.pattern_progress() if self.detector
                    else [(pattern, 0, None) for pattern in patterns])
        forming  = [pattern for pattern, formed, _ in progress if formed == len(pattern.trigger)]

        self.frame.configure(background=HEAT_DONE if forming else CARD_BORDER)
        self._draw_strip(forming)

        for row, (pattern, formed, entry) in enumerate(progress):
            self._draw_thermometer(CARD_STRIP + row * THERMO_ROW, pattern, formed, entry)

    def _draw_strip(self, forming):
        """Candles fechados; os ultimos (gatilho) numerados 1..8 e o candle de entrada como 9."""
        canvas      = self.canvas
        trigger_len = max(len(pattern.trigger) for pattern in self.app.patterns)
        colors      = self.detector.last_colors(STRIP_CANDLES) if self.detector else ''

        if not colors:
            canvas.create_text(8, 18, anchor='w', fill='#8a8f98',
                               text='Aguardando candles...' if self.detector else 'Procura parada')
            return

        window = min(trigger_len, len(colors))
        first  = len(colors) - window
        x0     = lambda i: 6 + i * 18

        canvas.create_rectangle(x0(first) - 3, 1, x0(len(colors)) - 3, 35, fill='#2c3746',
                                outline=HEAT_DONE if forming else '', width=2)

        for i, color in enumerate(colors):
            x = x0(i)
            canvas.create_rectangle(x, 14, x + 13, 32, fill=COLORS[color], outline='')

            if i >= first:
                canvas.create_text(x + 6, 7, text=str(trigger_len - (len(colors) - 1 - i)),
                                   fill='#e8e8e8', font=('TkDefaultFont', 7, 'bold'))

        # Candle de entrada (ainda aberto): contorno tracejado; seta e cor da operacao se o padrao formou.
        x       = x0(len(colors))
        outline = COLORS[forming[0].direction] if forming else '#8a8f98'

        canvas.create_text(x + 6, 7, text=str(trigger_len + 1), fill=outline, font=('TkDefaultFont', 7, 'bold'))
        canvas.create_rectangle(x, 14, x + 13, 32, outline=outline, dash=(3, 2), width=2)

        if forming:
            pattern   = forming[0]
            tip, base = (18, 28) if pattern.direction == GREEN else (28, 18)
            canvas.create_polygon(x + 2, base, x + 11, base, x + 6, tip, fill=outline)
            canvas.create_text(x + 22, 23, anchor='w', fill=outline, font=('TkDefaultFont', 9, 'bold'),
                               text=f'{pattern.name}: {DIRECTIONS[pattern.direction]}')

    def _draw_thermometer(self, y, pattern, formed, entry):
        """Candles do gatilho ja formados, barra de progresso e o que falta."""
        canvas   = self.canvas
        size     = len(pattern.trigger)
        fraction = formed / size
        mid      = y + 9
        done     = formed == size
        heat     = heat_color(fraction)

        canvas.create_text(6, mid, anchor='w', text=pattern.name, fill='#e8e8e8',
                           font=('TkDefaultFont', 8, 'bold'))

        # Preenchidos os ja formados; o proximo esperado com contorno tracejado.
        for i, color in enumerate(pattern.trigger):
            x = 112 + i * 15
            if i < formed:
                canvas.create_rectangle(x, y + 3, x + 11, y + 15, fill=COLORS[color], outline='')
            elif i == formed:
                canvas.create_rectangle(x, y + 3, x + 11, y + 15, outline=COLORS[color], dash=(2, 2), width=2)
            else:
                canvas.create_rectangle(x, y + 3, x + 11, y + 15, outline='#4a5568')

        # Termometro: bulbo + tubo, enchendo com cor mais quente conforme o padrao avanca.
        x0, x1 = 244, 336
        canvas.create_rectangle(x0 + 5, y + 5, x1, y + 13, fill='#2c3746', outline='#4a5568')
        if formed:
            canvas.create_rectangle(x0 + 5, y + 5, x0 + 5 + (x1 - x0 - 5) * fraction, y + 13,
                                    fill=heat, outline='')
        canvas.create_oval(x0 - 3, y + 2, x0 + 11, y + 16, fill=heat if formed else '#4a5568',
                           outline='#4a5568')

        canvas.create_text(x1 + 6, mid, anchor='w', text=f'{fraction:4.0%}',
                           fill=heat if formed else '#8a8f98', font=('TkDefaultFont', 8, 'bold'))

        if done:
            status = f'FORMADO: {DIRECTIONS[pattern.direction]} {ms_to_datetime(entry).strftime("%H:%M")}'
            color  = COLORS[pattern.direction]
        elif formed:
            missing = size - formed
            status  = (f'{formed}/{size} ' + ('falta 1' if missing == 1 else f'faltam {missing}') +
                       f' | entrada {ms_to_datetime(entry).strftime("%H:%M")}')
            color   = '#e8e8e8'
        else:
            status = f'0/{size} aguardando {pattern.trigger[0]}'
            color  = '#8a8f98'

        canvas.create_text(x1 + 40, mid, anchor='w', text=status, fill=color,
                           font=('TkDefaultFont', 8, 'bold' if done else 'normal'))


class IQMonitorApp(MonitorApp):
    """Monitor de padroes da Binance com os candles vindo da IQ Option, para ate MAX_ASSETS ativos."""

    def __init__(self, root):
        settings = load_settings()

        self.session      = IQSession()
        self.assets_job   = None
        self.iq_events    = queue.Queue()
        self.email_var    = tk.StringVar(value=os.environ.get('IQ_EMAIL', ''))
        self.password_var = tk.StringVar(value=os.environ.get('IQ_PASSWORD', ''))
        self.type_var     = tk.StringVar(value=settings.get('asset_type') if settings.get('asset_type')
                                         in ASSET_TYPES else DEFAULT_ASSET_TYPE)
        self.count_var    = tk.IntVar(value=settings.get('count') if settings.get('count')
                                      in range(1, MAX_ASSETS + 1) else 1)
        self.saved_assets = [str(name) for name in settings.get('assets', [])][:MAX_ASSETS]
        self.resume       = bool(settings.get('searching'))    # retoma a procura apos o login
        self.cards        = []
        self.cards_by_name = {}    # ativo da procura em andamento -> cartao
        self.asset_stats  = {}     # ativo -> placar do ativo (sinais, win, loss) na sessao
        self.alerts       = []
        self.cards_layout = None

        super().__init__(root)

        self.root.title('Monitor de Padroes - IQ Option')
        self.root.geometry(settings.get('geometry') or '1120x860')
        self.root.minsize(620, 520)
        self.root.bind('<Configure>', self._on_root_resized)

        if settings.get('interval') in INTERVAL_SECONDS:
            self.interval_var.set(settings['interval'])

        for var, key in ((self.alerts_var, 'alerts'), (self.sound_var, 'sound'),
                         (self.minutes_var, 'entry_minutes')):
            if isinstance(settings.get(key), bool):
                var.set(settings[key])

        self.interval_box['values'] = list(INTERVAL_SECONDS)
        if self.interval_var.get() not in INTERVAL_SECONDS:
            self.interval_var.set('1m')

        self._refresh_patterns()

        if self.saved_assets:
            self.write_log(f'Restaurados {self.count_var.get()} ativo(s) de {SETTINGS_FILE}' +
                           ('; a procura sera retomada apos o login' if self.resume else ''))

        self.root.after(100, self._process_iq_events)

    # ------------------------------------------------------------------ UI

    @property
    def SYMBOLS_LABEL(self):
        return f'ativos abertos ({self.type_var.get()})'

    @property
    def visible_cards(self):
        return self.cards[:self.count_var.get()]

    def _build_ui(self):
        self._build_login()
        self._build_controls()
        block_wheel(self.interval_box)
        self._build_info()
        self._build_cards()
        self._build_patterns()
        self._build_log()

        # O historico ocupa o espaco que sobrar; os cartoes tem prioridade.
        self.log.configure(height=6)

    def _build_login(self):
        login = ttk.Frame(self.root, padding=(10, 10, 10, 0))
        login.pack(fill='x')

        ttk.Label(login, text='E-mail:').pack(side='left')
        ttk.Entry(login, textvariable=self.email_var, width=28).pack(side='left', padx=(4, 12))

        ttk.Label(login, text='Senha:').pack(side='left')
        password = ttk.Entry(login, textvariable=self.password_var, width=18, show='*')
        password.pack(side='left', padx=(4, 12))
        password.bind('<Return>', lambda _event: self.login())

        self.login_button = ttk.Button(login, text='Entrar', command=self.login)
        self.login_button.pack(side='left')

        self.login_label = ttk.Label(login, text='Desconectado')
        self.login_label.pack(side='left', padx=12)

    def _build_asset_fields(self, top):
        ttk.Label(top, text='Tipo:').pack(side='left')

        self.type_box = ttk.Combobox(top, textvariable=self.type_var, width=16,
                                     values=list(ASSET_TYPES), state='readonly')
        self.type_box.pack(side='left', padx=(4, 12))
        self.type_box.bind('<<ComboboxSelected>>', self._on_type_selected)

        ttk.Label(top, text='Ativos:').pack(side='left')

        count_box = ttk.Spinbox(top, from_=1, to=MAX_ASSETS, textvariable=self.count_var, width=3,
                                state='readonly', command=self._on_count_changed)
        count_box.pack(side='left', padx=(4, 12))

        # Trocar tipo ou quantidade reinicia a procura: so com clique, nunca com a roda do mouse.
        block_wheel(self.type_box)
        block_wheel(count_box)

    def _build_cards(self):
        """Cartoes dos ativos numa area com rolagem; as colunas acompanham a largura da janela."""
        area = ttk.LabelFrame(self.root, text='Ativos acompanhados', padding=4)
        area.pack(fill='x', padx=10, pady=8)

        self.cards_canvas = tk.Canvas(area, highlightthickness=0, height=1)
        self.cards_scroll = ttk.Scrollbar(area, orient='vertical', command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=self.cards_scroll.set)
        self.cards_canvas.pack(side='left', fill='both', expand=True)

        self.cards_frame  = ttk.Frame(self.cards_canvas)
        self.cards_window = self.cards_canvas.create_window(0, 0, window=self.cards_frame, anchor='nw')

        self.cards_frame.bind('<Configure>', lambda _event: self._fit_cards_height())
        self.cards_canvas.bind('<Configure>', lambda _event: self._layout_cards())

        for sequence in WHEEL_EVENTS:
            self.root.bind_all(sequence, self._on_wheel, add='+')

        self._ensure_cards()

    def _ensure_cards(self):
        while len(self.cards) < self.count_var.get():
            card = AssetCard(self, self.cards_frame, len(self.cards))
            if card.index < len(self.saved_assets):
                card.var.set(self.saved_assets[card.index])
            self.cards.append(card)

        if len(self.cards) == 1 and not self.cards[0].var.get():
            self.cards[0].var.set(DEFAULT_ASSET)

    def _layout_cards(self, force=False):
        """Distribui os cartoes visiveis em tantas colunas quantas couberem."""
        width   = self.cards_canvas.winfo_width()
        count   = self.count_var.get()
        columns = max(1, min(count, width // CARD_WIDTH))

        self.cards_canvas.itemconfigure(self.cards_window, width=width)

        if not force and self.cards_layout == (columns, count):
            return

        self.cards_layout = (columns, count)

        for column in range(MAX_ASSETS):
            self.cards_frame.columnconfigure(column, weight=1 if column < columns else 0,
                                             uniform='card' if column < columns else '')

        for card in self.cards:
            if card.index < count:
                card.frame.grid(row=card.index // columns, column=card.index % columns,
                                sticky='nsew', padx=3, pady=3)
            else:
                card.frame.grid_forget()

    def _fit_cards_height(self):
        """Area dos cartoes do tamanho do conteudo, ate CARDS_SHARE da janela; acima disso, rola."""
        needed    = self.cards_frame.winfo_reqheight()
        available = max(180, int(self.root.winfo_height() * CARDS_SHARE))
        height    = min(needed, available)

        if int(self.cards_canvas.cget('height')) != height:
            self.cards_canvas.configure(height=height)

        self.cards_canvas.configure(scrollregion=(0, 0, 0, needed))

        if needed > height:
            self.cards_scroll.pack(side='right', fill='y')
        else:
            self.cards_scroll.pack_forget()
            self.cards_canvas.yview_moveto(0)

    def _on_root_resized(self, event):
        if event.widget is self.root:
            self._fit_cards_height()

    def _on_wheel(self, event):
        """Rola os cartoes quando o mouse esta sobre eles (e ha o que rolar)."""
        widget = self.root.winfo_containing(event.x_root, event.y_root)

        if widget is None or not str(widget).startswith(str(self.cards_canvas)):
            return

        if self.cards_scroll.winfo_ismapped():
            step = -1 if event.num == 4 or event.delta > 0 else 1
            self.cards_canvas.yview_scroll(step, 'units')

    def _show_symbols(self):
        values = list(self.symbols)[:300]

        for card in self.cards:
            card.box['values'] = values

    def _watched_names(self):
        return set(self.cards_by_name) if self.stream else set()

    def _fill_default_assets(self):
        """Cartoes sem ativo valido recebem um ativo aberto ainda nao usado (EUR/USD primeiro)."""
        cards = self.visible_cards
        used  = {self._resolve_symbol(card.var.get()) for card in cards} - {None}
        free  = [name for name in [DEFAULT_ASSET, *self.symbols] if name in self.symbols and name not in used]

        for card in cards:
            if self._resolve_symbol(card.var.get()) is None and free:
                previous = card.var.get().strip()
                card.var.set(free.pop(0))
                free = [name for name in free if name != card.var.get()]

                if previous:
                    self.write_log(f'Ativo {card.index + 1}: {previous} nao esta aberto em '
                                   f'{self.type_var.get()} agora; trocado por {card.var.get()}', 'error')

    def _on_count_changed(self):
        self._ensure_cards()
        self._fill_default_assets()
        self._layout_cards()

        # Com a procura ativa, so os ativos que entraram ou sairam mudam; os outros seguem sem reiniciar.
        if self.stream:
            self._sync_stream()

    def _on_card_asset(self, card):
        name = self._resolve_symbol(card.var.get())
        if name:
            card.var.set(name)

        if self.stream and name != card.name and not self._sync_stream():
            card.var.set(card.name)    # escolha invalida: o cartao continua no ativo que estava

    def _chosen_assets(self):
        """[(cartao, ativo)] dos cartoes visiveis; None (com aviso) se algum ativo e invalido ou repetido."""
        chosen = []

        for card in self.visible_cards:
            typed = card.var.get().strip().upper()
            name  = self._resolve_symbol(typed)

            if name is None:
                messagebox.showwarning('Ativo invalido', f'Ativo {card.index + 1}: {typed or "(vazio)"} '
                                       f'nao esta aberto em {self.type_var.get()} na IQ Option.')
                return None

            if any(name == other for _, other in chosen):
                messagebox.showwarning('Ativo repetido', f'{name} foi escolhido mais de uma vez.')
                return None

            card.var.set(name)
            chosen.append((card, name))

        return chosen

    def _attach(self, card, name, interval):
        """Liga o cartao a um ativo da procura, com um detector novo."""
        card.name     = name
        card.detector = PatternDetector(self.patterns, interval,
                                        use_entry_minutes=self._use_entry_minutes(interval))
        card.set_status('conectando...')
        self.asset_stats.setdefault(name, {'signals': 0, 'win': 0, 'loss': 0})
        self.cards_by_name[name] = card

    def _sync_stream(self):
        """Leva para a procura em andamento os ativos atuais dos cartoes, sem reiniciar os que nao mudaram.

        Devolve False (sem mudar nada) se algum ativo escolhido e invalido.
        """
        chosen = self._chosen_assets()
        if chosen is None:
            return False

        wanted  = {name: card for card, name in chosen}
        removed = [name for name, card in self.cards_by_name.items() if wanted.get(name) is not card]
        added   = [(card, name) for name, card in wanted.items() if self.cards_by_name.get(name) is not card]

        for name in removed:
            card = self.cards_by_name.pop(name)
            self.stream.remove_asset(name)

            if not any(card is new for new, _ in added):
                card.set_status('')    # cartao escondido: deixa de ser acompanhado

        for card, name in added:
            self._attach(card, name, self.stream.interval)
            self.stream.add_asset(name, self.symbols[name])
            card.draw()

        if removed or added:
            self._update_stats()
            self.write_log('Ativos alterados: ' +
                           ', '.join([f'-{name}' for name in removed] + [f'+{name}' for _, name in added]) +
                           f' (acompanhando {len(wanted)})')
            self._save_settings()

        return True

    def _update_stats(self):
        super()._update_stats()

        for card in self.cards:
            card.update_stats()

    def _draw_strip(self):
        for card in self.visible_cards:
            card.draw()

    # ---------------------------------------------------------------- login

    def login(self):
        email    = self.email_var.get().strip()
        password = self.password_var.get()

        if not email or not password:
            messagebox.showwarning('Login', 'Informe e-mail e senha da IQ Option.')
            return

        self.stop_search()
        self.login_button.configure(state='disabled')
        self.login_label.configure(text='Conectando...')

        def worker():
            try:
                self.session.connect(email, password)
            except IQOptionError as error:
                self.iq_events.put(('login_error', str(error)))
                return

            self.iq_events.put(('login_ok', None))

        threading.Thread(target=worker, daemon=True).start()

    def _load_symbols(self):
        # Os ativos so podem ser listados depois do login.
        self.write_log('Faca login na IQ Option para carregar os ativos')

    def _refresh_assets(self):
        """Carrega os ativos agora e de novo a cada ASSETS_REFRESH_MS (mercados abrem e fecham)."""
        if self.assets_job:
            self.root.after_cancel(self.assets_job)

        self.assets_job = self.root.after(ASSETS_REFRESH_MS, self._refresh_assets)

        if not self.session.connected:
            return

        type_label = self.type_var.get()

        def worker():
            try:
                assets = self.session.get_assets(ASSET_TYPES[type_label])
                self.iq_events.put(('assets', (type_label, assets)))
            except IQOptionError as error:
                self.iq_events.put(('assets_error', (type_label, str(error))))

        threading.Thread(target=worker, daemon=True).start()

    def _on_type_selected(self, _event=None):
        # Outro tipo, outra lista: a procura atual e encerrada e a lista recarregada.
        self.stop_search()
        self.symbols     = {}
        self.symbol_keys = {}
        self._show_symbols()

        if self.session.connected:
            self.write_log(f'Carregando {self.type_var.get()}...')
            self._refresh_assets()

    def _process_iq_events(self):
        try:
            while True:
                kind, payload = self.iq_events.get_nowait()

                if kind == 'login_ok':
                    self.login_label.configure(text='Conectado')
                    self.login_button.configure(state='normal')
                    self.write_log('Login na IQ Option realizado')
                    self.symbols = {}
                    self._refresh_assets()

                elif kind == 'login_error':
                    self.login_label.configure(text='Desconectado')
                    self.login_button.configure(state='normal')
                    self.write_log(payload, 'error')

                # Respostas de um tipo que ja nao esta selecionado sao descartadas.
                elif payload[0] != self.type_var.get():
                    continue

                elif kind == 'assets':
                    self._on_symbols_loaded({display_name(name): active_id
                                             for name, (active_id, is_open) in payload[1].items()
                                             if is_open})
                    self._fill_default_assets()

                    # A procura estava ligada quando o programa foi fechado: retoma com os mesmos ativos.
                    if self.resume and not self.stream:
                        self.resume = False
                        self.start_search()

                elif kind == 'assets_error':
                    self.write_log(f'Nao foi possivel carregar {payload[0]}: {payload[1]}', 'error')

        except queue.Empty:
            pass

        self.root.after(100, self._process_iq_events)

    # -------------------------------------------------------------- search

    def start_search(self):
        if not self.session.connected:
            messagebox.showwarning('IQ Option', 'Faca login antes de iniciar a procura.')
            return

        if not self.symbols:
            messagebox.showwarning('IQ Option', 'A lista de ativos ainda nao foi carregada.')
            return

        chosen = self._chosen_assets()
        if chosen is None:
            return

        names    = [name for _, name in chosen]
        interval = self.interval_var.get()

        self.cards_by_name = {}
        for card, name in chosen:
            self._attach(card, name, interval)

        self.close_time = None
        self._draw_strip()
        self._update_stats()

        self.stream_id += 1
        self.stream = IQMultiStream(self.stream_id, self.session, {name: self.symbols[name] for name in names},
                                    interval, self.events)
        self.stream.start()

        self.search_button.configure(text='Parar procura')
        self.status_label.configure(text=f'Conectando em {self.stream.name} ({interval})...')
        self.write_log(f'Procura iniciada em {", ".join(names)} ({interval})')
        self._save_settings()

    def stop_search(self):
        super().stop_search()

        for card in self.cards:
            card.set_status('')

    def _on_minutes_toggled(self):
        if self.stream:
            use_minutes = self._use_entry_minutes(self.stream.interval)
            for card in self.cards_by_name.values():
                card.detector.use_entry_minutes = use_minutes

        self._refresh_patterns()
        self._draw_strip()
        self.write_log('Regra de horario de entrada ' +
                       ('ligada' if self.minutes_var.get() else 'desligada: qualquer horario'))

    # -------------------------------------------------------------- events

    def _process_events(self):
        """Processa os eventos do stream; cada cartao alterado e redesenhado uma vez por rodada."""
        changed = set()

        try:
            while True:
                stream_id, kind, payload = self.events.get_nowait()

                # Ignora eventos de streams antigos (ativos trocados ou procura parada).
                if self.stream is None or stream_id != self.stream_id:
                    continue

                if kind == 'status':
                    self.status_label.configure(text=payload)
                    self.write_log(payload)
                    continue

                if kind == 'open':
                    self.close_time = payload['close_time']
                    continue

                card = self.cards_by_name.get(payload['name'])
                if card is None:
                    continue

                if kind == 'history':
                    self.time_offset = payload['time_offset']
                    card.detector.load_history(payload['candles'])
                    self._log_history_signals(card)
                    changed.add(card)

                elif kind == 'kline':
                    self._on_card_candle(card, payload['candle'])
                    changed.add(card)

                elif kind == 'asset':
                    card.set_status(payload['text'], payload['error'])
                    if payload['error']:
                        self.write_log(f'{card.name}: {payload["text"]}', 'error')

        except queue.Empty:
            pass

        if changed:
            for card in changed:
                card.draw()

            self._update_stats()

        self.root.after(100, self._process_events)

    def _on_card_candle(self, card, candle):
        signals, results = card.detector.add_closed_candle(candle)
        stats            = self.asset_stats[card.name]

        for signal, color, won in results:
            key = 'win' if won else 'loss'
            self.stats[key] += 1
            stats[key]      += 1
            self.write_log(f'Resultado {card.name} | {signal.pattern.name}: candle {color} -> '
                           f'{"WIN" if won else "LOSS"}', 'buy' if won else 'sell')

        for signal in signals:
            self.stats['signals'] += 1
            stats['signals']      += 1
            self._on_asset_signal(signal, card.name)

    def _log_history_signals(self, card):
        """Padroes que se formaram antes de o ativo entrar na procura.

        Se o candle de entrada ainda esta aberto, ainda da para operar: vira um sinal com alerta (marcado
        como em andamento) e o resultado e acompanhado. Os demais so vao para o historico, sem alerta.
        """
        server_now = time() * 1000 - self.time_offset

        for signal, entry_candle in card.detector.history_signals():
            entry     = ms_to_datetime(signal.entry_open_time).strftime('%H:%M')
            closes_in = (signal.entry_open_time + card.detector.interval_ms - server_now) / 1000

            if entry_candle is None and closes_in > 0:
                card.detector.follow(signal)
                self.stats['signals']                += 1
                self.asset_stats[card.name]['signals'] += 1
                self._on_asset_signal(signal, card.name, closes_in=closes_in)
                continue

            if entry_candle is None:
                outcome = 'entrada em andamento'
            else:
                color   = candle_color(entry_candle)
                outcome = f"candle {color} -> {'WIN' if color == signal.direction else 'LOSS'}"

            self.write_log(f'Historico (sem alerta): {signal.label} em {card.name} | '
                           f'entrada {entry} | {signal.pattern.name} | {outcome}')

    def _on_asset_signal(self, signal, name, closes_in=None):
        """closes_in: segundos ate o fim do candle de entrada, quando ele ja esta em andamento."""
        entry = ms_to_datetime(signal.entry_open_time).strftime('%H:%M')
        if closes_in is not None:
            entry += f' (em andamento, fecha em {int(closes_in)}s)'

        self.write_log(f'SINAL: {signal.label} em {name} ({self.stream.interval}) | entrada {entry} | '
                       f'{signal.pattern.name}', 'buy' if signal.direction == GREEN else 'sell')

        if not self.alerts_var.get():
            return

        # Varios ativos podem sinalizar no mesmo candle: os alertas abrem em cascata, sem se cobrir.
        self.alerts = [alert for alert in self.alerts if alert.winfo_exists()]
        alert       = self._show_alert(signal, name, entry)
        offset      = 40 * len(self.alerts)

        alert.geometry(f'+{self.root.winfo_rootx() + 80 + offset}+{self.root.winfo_rooty() + 80 + offset}')
        self.alerts.append(alert)

    def _save_settings(self):
        settings = {
            'asset_type'    : self.type_var.get(),
            'count'         : self.count_var.get(),
            # Cartoes que nao chegaram a ser criados nesta sessao mantem o ativo gravado antes.
            'assets'        : ([card.var.get().strip() for card in self.cards] +
                               self.saved_assets[len(self.cards):]),
            'interval'      : self.interval_var.get(),
            'alerts'        : self.alerts_var.get(),
            'sound'         : self.sound_var.get(),
            'entry_minutes' : self.minutes_var.get(),
            'searching'     : self.stream is not None,
            'geometry'      : self.root.geometry(),
        }

        try:
            save_settings(settings)
        except OSError as error:
            self.write_log(f'Nao foi possivel salvar as escolhas em {SETTINGS_FILE}: {error}', 'error')

    def on_close(self):
        self._save_settings()
        super().on_close()
        self.session.close()


def main():
    root = tk.Tk()

    try:
        app = IQMonitorApp(root)
    except (PatternError, OSError, KeyError, json.JSONDecodeError) as error:
        messagebox.showerror('Erro nos padroes', f'Falha ao carregar {PATTERNS_FILE}:\n{error}')
        root.destroy()
        return

    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
