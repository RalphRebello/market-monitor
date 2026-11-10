import json
import os
import queue
import threading
import tkinter as tk
from time import time
from tkinter import ttk, messagebox

from iqoption.iqoption_client import (ASSET_TYPES, INTERVAL_SECONDS, IQMultiStream, IQOptionError, IQSession,
                                      display_name)
from iqoption.settings import SETTINGS_FILE, load_settings, save_settings
from iqoption.monitor_base import (COLORS, SEQUENCE_COLOR, SEQUENCE_TEXT, STRIP_CANDLES, MonitorApp,
                                   normalize_symbol, pattern_title)
from core.sequences import ARMED, COMPLETED
from core.pattern_detector import (DIRECTIONS, GREEN, PATTERNS_FILE, PatternDetector, PatternError, candle_color,
                                   ms_to_datetime)


DEFAULT_ASSET      = 'EUR/USD'
DEFAULT_ASSET_TYPE = 'Opcoes binarias'
ASSETS_REFRESH_MS  = 5 * 60 * 1000
LATE_SECONDS       = 3    # alerta que chega depois disso no candle de entrada mostra o atraso

# Termometro: cor da barra conforme a fracao do gatilho ja formada (frio -> quente).
HEAT_COLORS = ((0.5, '#3498db'), (0.75, '#f1c40f'), (1.0, '#e67e22'))
HEAT_DONE   = '#f1c40f'

# Cartao de cada ativo: faixa de candles em cima e uma linha de termometro por padrao.
CARD_WIDTH   = 540     # largura minima de um cartao; define quantas colunas cabem na janela
CARD_STRIP   = 40
THERMO_ROW   = 22
THERMO_SQUARES = 120   # largura dos quadrados do gatilho na linha do termometro
CARD_GAP     = 10      # espaco do separador entre os grupos de padroes
SEPARATOR    = '#5d6f85'
CARD_BG    = '#1f2630'
CARD_BORDER  = '#3b4a5c'
CARD_FORMED  = '#e5262d'    # borda do cartao com padrao formado
CARDS_SHARE  = 0.55    # fracao maxima da altura da janela ocupada pelos cartoes (o resto rola)
CLOSE_COLOR  = '#e5262d'


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


def pattern_group(pattern):
    """Padroes do mesmo tamanho e tempo dividem a faixa de candles do cartao."""
    return len(pattern.trigger), pattern.interval


def strip_name(pattern):
    return f'{pattern.name} ({pattern.interval})' if pattern.interval else pattern.name


class AssetCard:
    """Um ativo exibido na tela: escolha do ativo, ultimos candles e termometro dos padroes.

    So mostra o ativo: a procura acompanha todos os ativos abertos do tipo, com ou sem cartao.
    """

    def __init__(self, app, parent):
        self.app  = app
        self.var  = tk.StringVar()
        self.name = None    # ativo exibido (nome valido da lista)

        self.frame = tk.Frame(parent, background=CARD_BORDER, padx=2, pady=2)
        body       = ttk.Frame(self.frame, padding=4)
        body.pack(fill='both', expand=True)

        head = ttk.Frame(body)
        head.pack(fill='x', pady=(0, 4))

        self.box = ttk.Combobox(head, textvariable=self.var, width=20)
        self.box.pack(side='left', padx=(0, 8))
        self.box.bind('<KeyRelease>', self._filter)
        self.box.bind('<<ComboboxSelected>>', lambda _event: app._on_card_asset(self))
        self.box.bind('<Return>', lambda _event: app._on_card_asset(self))
        block_wheel(self.box, app._on_wheel)

        close = tk.Label(head, text='\u2715', foreground=CLOSE_COLOR, font=('TkDefaultFont', 12, 'bold'),
                         cursor='hand2')
        close.pack(side='right', padx=(8, 0))
        close.bind('<Button-1>', lambda _event: app._close_card(self))

        self.status_label = ttk.Label(head, foreground='#8a8f98')
        self.status_label.pack(side='left', fill='x', expand=True)

        self.canvas = tk.Canvas(body, height=1, background=CARD_BG, highlightthickness=0)
        self.canvas.pack(fill='x')

        self.draw()

    def _filter(self, event):
        if event.keysym in ('Return', 'Up', 'Down', 'Escape'):
            return

        typed = normalize_symbol(self.var.get())
        self.box['values'] = [name for key, name in self.app.symbol_keys.items() if typed in key][:300]

    def set_status(self, text, error=False):
        self.status_label.configure(text=text, foreground='#d35400' if error else '#8a8f98')

    def draw(self):
        canvas = self.canvas
        canvas.delete('all')

        # So os padroes ativos; um grupo por tamanho de gatilho e tempo: faixa de candles, termometros
        # desses padroes e separador.
        # Abaixo deles, uma linha por sequencia ativa.
        patterns  = self.app.active_patterns
        sequences = self.app.active_sequences
        groups    = list(dict.fromkeys(pattern_group(pattern) for pattern in patterns))
        height    = (len(groups) * CARD_STRIP + max(0, len(groups) - 1) * CARD_GAP +
                     len(patterns) * THERMO_ROW + 2) if patterns else CARD_STRIP
        if sequences:
            height += CARD_GAP + len(sequences) * THERMO_ROW

        if int(canvas.cget('height')) != height:
            canvas.configure(height=height)

        detectors = {self.app.detector_of(self.name, pattern) for pattern in patterns} - {None}
        rows      = {row[0]: row for detector in detectors for row in detector.pattern_progress()}
        progress  = [rows.get(pattern, (pattern, 0, None)) for pattern in patterns]
        any_done  = any(formed == len(pattern.trigger) for pattern, formed, _ in progress)

        self.frame.configure(background=CARD_FORMED if any_done else CARD_BORDER)

        if not patterns:
            canvas.create_text(8, 18, anchor='w', fill='#8a8f98', text='Nenhum padrao ativo')

        y = 0
        for group in groups:
            if y:
                # A largura real do canvas acompanha a janela; o excesso da linha fica cortado.
                canvas.create_line(0, y + 2, 4000, y + 2, fill=SEPARATOR, width=2)
                y += CARD_GAP

            size    = group[0]
            members = [row for row in progress if pattern_group(row[0]) == group]
            forming = [pattern for pattern, formed, _ in members if formed == size]

            self._draw_strip(y, size, [row[0] for row in members], forming)
            y += CARD_STRIP

            for pattern, formed, entry in members:
                self._draw_thermometer(y, pattern, formed, entry)
                y += THERMO_ROW

        if sequences:
            y = max(y, CARD_STRIP)
            canvas.create_line(0, y + 2, 4000, y + 2, fill=SEPARATOR, width=2)
            y += CARD_GAP

        for sequence in sequences:
            self._draw_sequence(y, sequence)
            y += THERMO_ROW

    def _draw_sequence(self, y, sequence):
        """Situacao da sequencia neste ativo: aguardando o primeiro padrao ou armada aguardando o segundo."""
        armed = self.app.tracker.armed_at(sequence, self.name) if self.name else None

        # Aguardando o primeiro: cinza. Armada, aguardando o segundo: amarelo, como o popup que vira.
        waiting = sequence.first if armed is None else sequence.then
        color   = '#8a8f98' if armed is None else SEQUENCE_COLOR

        self.canvas.create_text(6, y + 9, anchor='w', text=f'Sequencia {sequence.name} | Aguardando {waiting}',
                                fill=color, font=('TkDefaultFont', 8, 'bold'))

    def _draw_strip(self, y, trigger_len, patterns, forming):
        """Candles fechados; os ultimos numerados como o gatilho (1..N) e o candle de entrada como N+1."""
        canvas   = self.canvas
        detector = self.app.detector_of(self.name, patterns[0])
        colors   = detector.last_colors(STRIP_CANDLES) if detector else ''

        if not colors:
            if y:    # A situacao aparece so na primeira faixa; as outras mostram de que padroes sao.
                text = ' / '.join(strip_name(pattern) for pattern in patterns)
            elif detector:
                text = 'Aguardando candles...'
            elif self.app.stream:
                text = 'Ativo fora da procura (fechado ou de outro tipo)'
            else:
                text = 'Procura parada'

            canvas.create_text(8, y + 18, anchor='w', fill='#8a8f98', text=text)
            return

        window = min(trigger_len, len(colors))
        first  = len(colors) - window
        x0     = lambda i: 6 + i * 18

        canvas.create_rectangle(x0(first) - 3, y + 1, x0(len(colors)) - 3, y + 35, fill='#2c3746',
                                outline=HEAT_DONE if forming else '', width=2)

        for i, color in enumerate(colors):
            x = x0(i)
            canvas.create_rectangle(x, y + 14, x + 13, y + 32, fill=COLORS[color], outline='')

            if i >= first:
                canvas.create_text(x + 6, y + 7, text=str(trigger_len - (len(colors) - 1 - i)),
                                   fill='#e8e8e8', font=('TkDefaultFont', 7, 'bold'))

        # Candle de entrada (ainda aberto): contorno tracejado; seta e cor da operacao se o padrao formou.
        # Padrao sem direcao: so destaca que formou. Enquanto nao forma, a faixa mostra de que padrao e.
        x         = x0(len(colors))
        pattern   = forming[0] if forming else None
        direction = pattern.direction if pattern else None
        outline   = COLORS[direction] if direction else HEAT_DONE if pattern else '#8a8f98'

        canvas.create_text(x + 6, y + 7, text=str(trigger_len + 1), fill=outline, font=('TkDefaultFont', 7, 'bold'))
        canvas.create_rectangle(x, y + 14, x + 13, y + 32, outline=outline, dash=(3, 2), width=2)

        if direction:
            tip, base = (18, 28) if direction == GREEN else (28, 18)
            canvas.create_polygon(x + 2, y + base, x + 11, y + base, x + 6, y + tip, fill=outline)
            label = f'{pattern.name}: {DIRECTIONS[direction]}'
        elif pattern:
            label = f'{pattern.name}: FORMADO'
        else:
            label = ' / '.join(strip_name(item) for item in patterns if not item.direction)

        if label:
            canvas.create_text(x + 22, y + 23, anchor='w', fill=outline, text=label,
                               font=('TkDefaultFont', 9, 'bold' if pattern else 'normal'))

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
        # Gatilhos longos usam quadrados mais estreitos para caber antes da barra.
        step  = min(15, THERMO_SQUARES // size)
        width = min(11, step - 2)

        for i, color in enumerate(pattern.trigger):
            x = 112 + i * step
            if i < formed:
                canvas.create_rectangle(x, y + 3, x + width, y + 15, fill=COLORS[color], outline='')
            elif i == formed:
                canvas.create_rectangle(x, y + 3, x + width, y + 15, outline=COLORS[color], dash=(2, 2), width=2)
            else:
                canvas.create_rectangle(x, y + 3, x + width, y + 15, outline='#4a5568')

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

        if done and pattern.direction:
            status = f'FORMADO: {DIRECTIONS[pattern.direction]} {ms_to_datetime(entry).strftime("%H:%M")}'
            color  = COLORS[pattern.direction]
        elif done:
            status = f'FORMADO: entrada {ms_to_datetime(entry).strftime("%H:%M")}'
            color  = HEAT_DONE
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
    """Monitor de padroes com os candles vindo da IQ Option.

    A procura acompanha todos os ativos abertos do tipo escolhido; os cartoes so escolhem quais aparecem.
    """

    def __init__(self, root):
        settings = load_settings()

        self.session      = IQSession()
        self.assets_job   = None
        self.iq_events    = queue.Queue()
        self.email_var    = tk.StringVar(value=os.environ.get('IQ_LOGIN') or os.environ.get('IQ_EMAIL', ''))
        self.password_var = tk.StringVar(value=os.environ.get('IQ_PASSWORD', ''))
        self.type_var     = tk.StringVar(value=settings.get('asset_type') if settings.get('asset_type')
                                         in ASSET_TYPES else DEFAULT_ASSET_TYPE)
        self.saved_assets = [str(name) for name in settings.get('assets', [])]
        # Escolhas gravadas antes dos botoes +/x: so os primeiros 'count' cartoes estavam na tela.
        if isinstance(settings.get('count'), int):
            self.saved_assets = self.saved_assets[:max(1, settings['count'])]
        self.resume       = bool(settings.get('searching'))    # retoma a procura apos o login
        self.cards        = []
        self.streams       = {}    # stream_id -> IQMultiStream: o do Tempo escolhido e o de cada tempo fixo
        self.main_interval = None  # Tempo escolhido ao iniciar a procura
        self.detectors     = {}    # tempo -> {ativo -> PatternDetector} (todos os ativos, com ou sem cartao)
        self.asset_status = {}     # ativo da procura -> (texto, erro), do stream do Tempo escolhido
        self.alerts       = []
        self.cards_layout = None
        self.cards_columns = 0

        super().__init__(root)

        self.root.title('Monitor de Padroes - IQ Option')
        self.root.geometry(settings.get('geometry') or '1120x860')
        self.root.minsize(620, 520)
        self.root.bind('<Configure>', self._on_root_resized)

        if settings.get('interval') in INTERVAL_SECONDS:
            self.interval_var.set(settings['interval'])

        for var, key in ((self.alerts_var, 'alerts'), (self.sound_var, 'sound'), (self.minutes_var, 'entry_minutes')):
            if isinstance(settings.get(key), bool):
                var.set(settings[key])

        for key, variables in (('disabled_patterns', self.pattern_vars), ('disabled_sequences', self.sequence_vars)):
            disabled = settings.get(key)
            if isinstance(disabled, list):
                for name, var in variables.items():
                    var.set(name not in disabled)

        self.interval_box['values'] = list(INTERVAL_SECONDS)
        if self.interval_var.get() not in INTERVAL_SECONDS:
            self.interval_var.set('1m')

        self._refresh_patterns()
        self._redraw_cards()

        if self.saved_assets:
            self.write_log(f'Restaurados {len(self.cards)} cartao(oes) de {SETTINGS_FILE}' +
                           ('; a procura sera retomada apos o login' if self.resume else ''))

        self.root.after(100, self._process_iq_events)

    # ------------------------------------------------------------------ UI

    @property
    def SYMBOLS_LABEL(self):
        return f'ativos abertos ({self.type_var.get()})'

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

        # Trocar o tipo reinicia a procura: so com clique, nunca com a roda do mouse.
        block_wheel(self.type_box)

        ttk.Button(top, text='+', width=3, command=self._add_card).pack(side='left', padx=(0, 12))

    def _build_cards(self):
        """Cartoes dos ativos numa area com rolagem; as colunas acompanham a largura da janela."""
        self.cards_area = ttk.LabelFrame(self.root, text='Ativos na tela', padding=4)
        self.cards_area.pack(fill='x', padx=10, pady=8)

        self.cards_canvas = tk.Canvas(self.cards_area, highlightthickness=0, height=1)
        self.cards_scroll = ttk.Scrollbar(self.cards_area, orient='vertical', command=self.cards_canvas.yview)
        self.cards_canvas.configure(yscrollcommand=self.cards_scroll.set)
        self.cards_canvas.pack(side='left', fill='both', expand=True)

        self.cards_frame  = ttk.Frame(self.cards_canvas)
        self.cards_window = self.cards_canvas.create_window(0, 0, window=self.cards_frame, anchor='nw')

        self.cards_frame.bind('<Configure>', lambda _event: self._fit_cards_height())
        self.cards_canvas.bind('<Configure>', lambda _event: self._layout_cards())

        for sequence in WHEEL_EVENTS:
            self.root.bind_all(sequence, self._on_wheel, add='+')

        for name in self.saved_assets or [DEFAULT_ASSET]:
            self._new_card(name)

        self._layout_cards(force=True)
        self._update_cards_title()

    def _new_card(self, typed):
        card = AssetCard(self, self.cards_frame)
        card.var.set(typed)
        card.box['values'] = list(self.symbols)[:300]
        self.cards.append(card)
        self._show_card_asset(card, self._resolve_symbol(typed))
        return card

    def _add_card(self):
        """Botao +: novo cartao com um ativo aberto que ainda nao esta na tela."""
        free = self._free_assets()
        card = self._new_card(free[0] if free else '')

        self._layout_cards(force=True)
        self._update_cards_title()
        self.root.after_idle(lambda: self.cards_canvas.yview_moveto(1))    # mostra o cartao novo
        self._save_settings()

        if not free:
            card.box.focus_set()

    def _open_alert_card(self, name):
        """Clique no popup: traz a janela para a frente e mostra o cartao do ativo, criando-o se preciso."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

        if name not in self.symbols:
            self.write_log(f'{name} nao esta mais aberto em {self.type_var.get()}; cartao nao aberto', 'error')
            return

        card = next((card for card in self.cards if card.name == name), None)

        if card is None:
            card = self._new_card(name)
            self._layout_cards(force=True)
            self._update_cards_title()
            self._save_settings()

        self.root.after_idle(lambda: self._scroll_to_card(card))

    def _scroll_to_card(self, card):
        self.cards_frame.update_idletasks()

        if card.frame.winfo_exists():
            self.cards_canvas.yview_moveto(card.frame.winfo_y() / max(1, self.cards_frame.winfo_reqheight()))

    def _close_card(self, card):
        """Botao x: tira o ativo da tela. A procura continua acompanhando o ativo."""
        self.cards.remove(card)
        card.frame.destroy()
        self._layout_cards(force=True)
        self._update_cards_title()
        self._save_settings()

    def _update_cards_title(self):
        self.cards_area.configure(text=f'Ativos na tela ({len(self.cards)})' + (
            f'  |  procura em {len(self.stream.assets)} ativos' if self.stream else ''))

    def detector_of(self, name, pattern):
        """Detector que acompanha o padrao neste ativo (None: ativo fora da procura)."""
        return self.detectors.get(pattern.interval or self.main_interval, {}).get(name)

    def _layout_cards(self, force=False):
        """Distribui os cartoes em tantas colunas quantas couberem."""
        width   = self.cards_canvas.winfo_width()
        count   = len(self.cards)
        columns = max(1, min(count, width // CARD_WIDTH))

        self.cards_canvas.itemconfigure(self.cards_window, width=width)

        if not force and self.cards_layout == (columns, count):
            return

        self.cards_layout = (columns, count)

        for column in range(max(columns, self.cards_columns)):
            self.cards_frame.columnconfigure(column, weight=1 if column < columns else 0,
                                             uniform='card' if column < columns else '')
        self.cards_columns = columns

        for index, card in enumerate(self.cards):
            card.frame.grid(row=index // columns, column=index % columns, sticky='nsew', padx=3, pady=3)

        self._fit_cards_height()

    def _fit_cards_height(self):
        """Area dos cartoes do tamanho do conteudo, ate CARDS_SHARE da janela; acima disso, rola."""
        self.cards_frame.update_idletasks()
        needed    = self.cards_frame.winfo_reqheight() if self.cards else 30
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
        return {card.name for card in self.cards if card.name}

    def _cards_of(self, name):
        return [card for card in self.cards if card.name == name]

    def _free_assets(self):
        """Ativos abertos que ainda nao estao na tela (EUR/USD primeiro)."""
        used = {card.name for card in self.cards}
        return [name for name in dict.fromkeys([DEFAULT_ASSET, *self.symbols])
                if name in self.symbols and name not in used]

    def _show_card_asset(self, card, name):
        """Liga o cartao ao ativo (None: nenhum ativo valido) e redesenha."""
        card.name = name
        if name:
            card.var.set(name)

        text, error = self.asset_status.get(name, ('', False))
        card.set_status(text, error)
        card.draw()

    def _fill_default_assets(self):
        """Cartoes sem ativo valido recebem um ativo aberto ainda nao exibido (EUR/USD primeiro)."""
        for card in self.cards:
            name = self._resolve_symbol(card.var.get())

            if name is not None and not any(other.name == name for other in self.cards if other is not card):
                if name != card.name:
                    self._show_card_asset(card, name)
                continue

            free = self._free_assets()
            if not free:
                self._show_card_asset(card, None)
                continue

            previous = card.var.get().strip()
            self._show_card_asset(card, free[0])

            if previous:
                self.write_log(f'Cartao {previous}: nao esta aberto em {self.type_var.get()} agora; '
                               f'trocado por {card.name}', 'error')

    def _on_card_asset(self, card):
        typed = card.var.get().strip().upper()
        name  = self._resolve_symbol(typed)

        if name is None:
            if self.symbols:
                messagebox.showwarning('Ativo invalido', f'{typed or "(vazio)"} nao esta aberto em '
                                       f'{self.type_var.get()} na IQ Option.')
            card.var.set(card.name or '')    # escolha invalida: o cartao continua no ativo que estava
            return

        if any(other.name == name for other in self.cards if other is not card):
            messagebox.showwarning('Ativo repetido', f'{name} ja esta na tela.')
            card.var.set(card.name or '')
            return

        self._show_card_asset(card, name)
        self._save_settings()

    def _sync_stream(self):
        """Com a procura ligada, acompanha os ativos que abriram e deixa os que fecharam."""
        watched = self.detectors[self.main_interval]
        added   = [name for name in self.symbols if name not in watched]
        removed = [name for name in watched if name not in self.symbols]

        for name in removed:
            self.tracker.reset(asset=name)

            for stream in self.streams.values():
                stream.remove_asset(name)
                self.detectors[stream.interval].pop(name)

            self.asset_status.pop(name, None)

        for name in added:
            self._attach(name)

            for stream in self.streams.values():
                stream.add_asset(name, self.symbols[name])

        if added or removed:
            for card in self.cards:
                if card.name in added or card.name in removed:
                    self._show_card_asset(card, card.name)

            self._update_cards_title()
            self.write_log(f'Procura: +{len(added)} / -{len(removed)} ativos (acompanhando {len(watched)})')

    def _attach(self, name):
        """Inclui o ativo na procura, com um detector novo em cada tempo acompanhado."""
        for interval, detectors in self.detectors.items():
            patterns        = [pattern for pattern in self.patterns
                               if (pattern.interval or self.main_interval) == interval]
            detectors[name] = PatternDetector(patterns, interval,
                                              use_entry_minutes=self._use_entry_minutes(interval))

        self.asset_status[name] = ('conectando...', False)

    def _on_minutes_toggled(self):
        for interval, detectors in self.detectors.items():
            for detector in detectors.values():
                detector.use_entry_minutes = self._use_entry_minutes(interval)

        self._refresh_patterns()

        for card in self.cards:
            card.draw()

        self._save_settings()
        self.write_log('Filtro de minutos ' + ('ligado' if self.minutes_var.get() else 'desligado: qualquer horario'))

    def _on_pattern_toggled(self, pattern):
        """Liga/desliga um padrao: some dos cartoes e deixa de gerar alerta, sem reiniciar a procura."""
        super()._on_pattern_toggled(pattern)
        self._redraw_cards()
        self._save_settings()

    def _on_sequence_toggled(self, sequence):
        """Ligada ou desligada, a sequencia recomeca do zero: aguardando o primeiro padrao."""
        super()._on_sequence_toggled(sequence)
        self.tracker.reset(sequence)
        self._redraw_cards()
        self._save_settings()

    def _redraw_cards(self):
        """Redesenha os cartoes; a altura deles muda com o numero de padroes ativos."""
        for card in self.cards:
            card.draw()

        self._fit_cards_height()

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

                    if self.stream:
                        self._sync_stream()

                    # A procura estava ligada quando o programa foi fechado: retoma.
                    elif self.resume:
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

        interval  = self.interval_var.get()
        intervals = list(dict.fromkeys([interval, *(p.interval for p in self.patterns if p.interval)]))

        self.main_interval = interval
        self.detectors     = {item: {} for item in intervals}
        self.tracker.reset()
        self.asset_status  = {}
        for name in self.symbols:
            self._attach(name)

        self.close_time = None
        self.streams    = {}

        # Um stream por tempo; padroes de tempo fixo igual ao escolhido usam o mesmo stream.
        for item in intervals:
            self.stream_id += 1
            stream = IQMultiStream(self.stream_id, self.session, self.symbols, item, self.events)
            self.streams[self.stream_id] = stream

            if item == interval:
                self.stream = stream

            stream.start()

        for card in self.cards:
            self._show_card_asset(card, card.name)

        self._update_cards_title()

        self.search_button.configure(text='Parar procura')
        self.status_label.configure(text=f'Conectando em {self.stream.name} ({interval})...')
        self.write_log(f'Procura iniciada em todos os {len(self.symbols)} {self.SYMBOLS_LABEL} '
                       f'({" + ".join(intervals)})')
        self._save_settings()

    def _stop_fixed_streams(self):
        """Para os streams de tempo fixo; o do Tempo escolhido e parado pela classe base."""
        for stream in self.streams.values():
            if stream is not self.stream:
                stream.stop()

        self.streams = {}

    def stop_search(self):
        self._stop_fixed_streams()
        super().stop_search()
        self.asset_status = {}
        self.tracker.reset()

        for card in self.cards:
            card.set_status('')

        self._update_cards_title()

    # -------------------------------------------------------------- events

    def _process_events(self):
        """Processa os eventos do stream; cada cartao alterado e redesenhado uma vez por rodada."""
        changed = set()

        try:
            while True:
                stream_id, kind, payload = self.events.get_nowait()

                # Ignora eventos de streams antigos (tipo trocado ou procura parada).
                stream = self.streams.get(stream_id)
                if stream is None:
                    continue

                # A situacao geral e a contagem regressiva sao as do Tempo escolhido.
                main = stream is self.stream

                if kind == 'status':
                    if main:
                        self.status_label.configure(text=payload)
                    self.write_log(payload)
                    continue

                if kind == 'open':
                    if main:
                        self.close_time = payload['close_time']
                    continue

                name     = payload['name']
                detector = self.detectors[stream.interval].get(name)
                if detector is None:
                    continue

                if kind == 'history':
                    self.time_offset = payload['time_offset']
                    detector.load_history(payload['candles'])
                    self._log_history_signals(name, detector)
                    changed.add(name)

                elif kind == 'kline':
                    self.time_offset = payload['time_offset']
                    self._on_asset_candle(name, payload['candle'], detector)
                    changed.add(name)

                elif kind == 'revised':
                    self._on_asset_revised(name, payload['candle'], detector)
                    changed.add(name)

                elif kind == 'asset' and not main:
                    if payload['error'] and self._cards_of(name):
                        self.write_log(f'{name} ({stream.interval}): {payload["text"]}', 'error')

                elif kind == 'asset':
                    self.asset_status[name] = (payload['text'], payload['error'])
                    shown = self._cards_of(name)

                    for card in shown:
                        card.set_status(payload['text'], payload['error'])

                    # So os ativos na tela vao para o historico, para nao encher com todos os ativos.
                    if payload['error'] and shown:
                        self.write_log(f'{name}: {payload["text"]}', 'error')

        except queue.Empty:
            pass

        for card in self.cards:
            if card.name in changed:
                card.draw()

        self.root.after(100, self._process_events)

    def _on_asset_candle(self, name, candle, detector):
        # Os resultados (WIN/LOSS) que o detector confere nao sao exibidos: so os padroes formados.
        signals, _results = detector.add_closed_candle(candle)

        for signal in signals:
            self._on_asset_signal(name, signal, detector)

    def _on_asset_revised(self, name, candle, detector):
        """A IQ Option mudou um candle depois de enviado: troca o candle pela versao final."""
        old_color, _changed = detector.revise(candle)
        when                = ms_to_datetime(candle['open_time']).strftime('%H:%M')

        if old_color is not None and self._cards_of(name):
            self.write_log(f'{name}: candle {when} corrigido pela IQ Option: {old_color} -> '
                           f'{candle_color(candle)} (a busca dos padroes neste candle usou a cor antiga)', 'error')

    def _log_history_signals(self, name, detector):
        """Padroes que se formaram antes de o ativo entrar na procura.

        Se o candle de entrada ainda esta aberto, ainda da para operar: vira um sinal com alerta (marcado
        com o atraso). Os que ja passaram sao ignorados.
        """
        server_now = time() * 1000 - self.time_offset

        for signal, entry_candle in detector.history_signals():
            if entry_candle is None and server_now < signal.entry_open_time + detector.interval_ms:
                self._on_asset_signal(name, signal, detector)

    def _entry_text(self, signal, detector):
        """(texto da entrada, segundos desde a abertura, entrada ja fechou)."""
        interval = detector.interval_ms / 1000
        elapsed  = (time() * 1000 - self.time_offset - signal.entry_open_time) / 1000
        entry    = ms_to_datetime(signal.entry_open_time).strftime('%H:%M')

        if elapsed > LATE_SECONDS and elapsed < interval:
            entry += f' (atrasado {int(elapsed)}s, fecha em {int(interval - elapsed)}s)'

        return entry, elapsed, elapsed >= interval

    def _on_asset_signal(self, name, signal, detector):
        """Sinal de um padrao em qualquer ativo da procura, esteja ele na tela ou nao.

        As sequencias acompanham o sinal mesmo com o padrao desativado; o alerta verde so sai se ativo.
        """
        events = self.tracker.observe(name, signal, detector.interval_ms, self.active_sequences)

        if self.is_active(signal.pattern):
            self._alert_pattern(name, signal, detector)

        for sequence, kind, _first in events:
            self._on_sequence_event(name, sequence, kind, signal, detector)

        if events:
            for card in self._cards_of(name):
                card.draw()

    def _alert_pattern(self, name, signal, detector):
        """Alerta de um padrao cujo candle de entrada esta aberto; se ja fechou, so registra."""
        entry, elapsed, missed = self._entry_text(signal, detector)

        if missed:
            self.write_log(f'Padrao perdido (sem alerta): {name} | {pattern_title(signal.pattern.name)} | '
                           f'entrada {entry} ja fechou, {int(elapsed)}s atrasado', 'error')
            return

        # Mesmo texto do popup: ativo, padrao formado e entrada (a hora do registro vem no inicio da linha).
        self.write_log(f'{name} | {pattern_title(signal.pattern.name)} | entrada {entry}', 'buy')
        self._popup(signal, name, entry)

    def _on_sequence_event(self, name, sequence, kind, signal, detector):
        """Sequencia armada (so registra) ou completada (alerta amarelo); completada, ela zera."""
        if kind == ARMED:
            self.write_log(f'{name} | Sequencia {sequence.name}: {sequence.first} formado, '
                           f'aguardando {sequence.then}')
            return

        entry, elapsed, missed = self._entry_text(signal, detector)

        if missed:
            self.write_log(f'Sequencia perdida (sem alerta): {name} | {sequence.name} | entrada {entry} ja fechou, '
                           f'{int(elapsed)}s atrasado; aguardando {sequence.first} de novo', 'error')
            return

        self.write_log(f'{name} | Sequencia {sequence.name} | entrada {entry}', 'sequence')
        self._popup(signal, name, entry, title=f'Sequencia {sequence.name}', color=SEQUENCE_COLOR,
                    text_color=SEQUENCE_TEXT)

    def _popup(self, signal, name, entry, **style):
        if not self.alerts_var.get():
            return

        # Varios ativos podem sinalizar no mesmo candle: os alertas abrem em cascata, sem se cobrir.
        self.alerts = [alert for alert in self.alerts if alert.winfo_exists()]
        alert       = self._show_alert(signal, name, entry, on_click=lambda: self._open_alert_card(name), **style)
        offset      = 40 * (len(self.alerts) % 10)

        alert.geometry(f'+{self.root.winfo_rootx() + 80 + offset}+{self.root.winfo_rooty() + 80 + offset}')
        self.alerts.append(alert)

    def _save_settings(self):
        settings = {
            'asset_type'    : self.type_var.get(),
            'assets'        : [card.var.get().strip() for card in self.cards],
            'interval'      : self.interval_var.get(),
            'alerts'        : self.alerts_var.get(),
            'sound'         : self.sound_var.get(),
            'entry_minutes' : self.minutes_var.get(),
            'disabled_patterns' : [name for name, var in self.pattern_vars.items() if not var.get()],
            'disabled_sequences': [name for name, var in self.sequence_vars.items() if not var.get()],
            'searching'     : self.stream is not None,
            'geometry'      : self.root.geometry(),
        }

        try:
            save_settings(settings)
        except OSError as error:
            self.write_log(f'Nao foi possivel salvar as escolhas em {SETTINGS_FILE}: {error}', 'error')

    def on_close(self):
        self._save_settings()
        self._stop_fixed_streams()
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
