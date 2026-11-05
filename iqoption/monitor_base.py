import queue
import re
import tkinter as tk
from time import time
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from core.pattern_detector import (DIRECTIONS, DOJI, GREEN, INTERVAL_MS, PATTERNS_FILE, RED, load_patterns,
                                   ms_to_datetime)


ALERT_SECONDS  = 30
STRIP_CANDLES  = 16

COLORS = {
    GREEN : '#26a65b',
    RED   : '#e5262d',
    DOJI  : '#8a8f98',
}


def pattern_title(name):
    """Nome do padrao sem a direcao no final: 'Padrao 1 - CALL' vira 'Padrao 1'."""
    return re.sub(r'\s*-\s*(CALL|PUT)\s*$', '', name, flags=re.IGNORECASE) or name


def normalize_symbol(text):
    """Chave de busca: 'eur/usd (otc)', 'EURUSD-OTC' e 'EUR/USD (OTC)' viram 'EURUSDOTC'."""
    return ''.join(ch for ch in text.upper() if ch.isalnum())


class MonitorApp:
    """Janela base do monitor: controles, padroes, historico e alertas.

    A subclasse monta a janela (_build_ui, _build_asset_fields), carrega os ativos (_load_symbols,
    _show_symbols, _watched_names), inicia a procura (start_search) e processa os eventos (_process_events).
    """

    SYMBOLS_LABEL = 'ativos'

    def __init__(self, root):
        self.root = root
        self.root.title('Monitor de Padroes - Opcoes Binarias')
        self.root.geometry('900x620')

        # O intervalo do JSON e o padrao e o unico em que o filtro de minutos se aplica.
        self.patterns_interval, self.patterns = load_patterns(PATTERNS_FILE)

        self.events      = queue.Queue()
        self.stream      = None
        self.stream_id   = 0
        self.symbols     = {}    # nome exibido -> identificador na API da plataforma
        self.symbol_keys = {}    # normalize_symbol(nome) -> nome exibido
        self.time_offset = 0
        self.close_time  = None

        self.interval_var = tk.StringVar(value=self.patterns_interval)
        self.minutes_var  = tk.BooleanVar(value=True)
        self.alerts_var   = tk.BooleanVar(value=True)
        self.sound_var    = tk.BooleanVar(value=True)

        self._build_ui()
        self._load_symbols()

        self.root.after(100, self._process_events)
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------------ UI

    def _build_controls(self):
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill='x')

        self._build_asset_fields(top)

        ttk.Label(top, text='Tempo:').pack(side='left')

        self.interval_box = ttk.Combobox(top, textvariable=self.interval_var, width=5,
                                         values=list(INTERVAL_MS), state='readonly')
        self.interval_box.pack(side='left', padx=(4, 12))
        self.interval_box.bind('<<ComboboxSelected>>', self._on_interval_selected)

        self.search_button = ttk.Button(top, text='Iniciar procura', command=self.toggle_search)
        self.search_button.pack(side='left', padx=4)

        ttk.Checkbutton(top, text='Alertas ativos', variable=self.alerts_var,
                        command=self._on_alerts_toggled).pack(side='left', padx=12)
        ttk.Checkbutton(top, text='Som', variable=self.sound_var).pack(side='left')

    def _build_info(self):
        info = ttk.Frame(self.root, padding=(10, 0))
        info.pack(fill='x')

        self.status_label = ttk.Label(info, text='Procura parada')
        self.status_label.pack(side='left')

        self.countdown_label = ttk.Label(info, text='')
        self.countdown_label.pack(side='right')

    def _build_patterns(self):
        self.patterns_frame = ttk.LabelFrame(self.root, text='Padroes carregados', padding=6)
        self.patterns_frame.pack(fill='x', padx=10)

        optional = [pattern for pattern in self.patterns if pattern.minutes_optional]
        if optional:
            minutes = '/'.join(str(m) for m in sorted({m for pattern in optional
                                                       for m in pattern.entry_minutes}))
            names   = ', '.join(pattern_title(pattern.name) for pattern in optional)
            ttk.Checkbutton(self.patterns_frame, variable=self.minutes_var,
                            text=f'Filtro de minutos {minutes} (so em {self.patterns_interval}): {names}',
                            command=self._on_minutes_toggled).pack(anchor='w', pady=(0, 4))

        self.pattern_labels = [ttk.Label(self.patterns_frame) for _ in self.patterns]
        for label in self.pattern_labels:
            label.pack(anchor='w')

        self._refresh_patterns()

    def _build_log(self):
        self.log = ScrolledText(self.root, height=14, state='disabled', font=('Courier', 10))
        self.log.pack(fill='both', expand=True, padx=10, pady=10)
        self.log.tag_configure('buy', foreground=COLORS[GREEN], font=('Courier', 10, 'bold'))
        self.log.tag_configure('sell', foreground=COLORS[RED], font=('Courier', 10, 'bold'))
        self.log.tag_configure('error', foreground='#d35400')

    def write_log(self, text, tag=None):
        now = ms_to_datetime(time() * 1000).strftime('%H:%M:%S')

        self.log.configure(state='normal')
        self.log.insert('end', f'[{now}] {text}\n', tag)
        self.log.see('end')
        self.log.configure(state='disabled')

    def _use_entry_minutes(self, interval):
        """O filtro de minutos (minutes_optional) so vale se estiver ligado e no tempo do patterns.json."""
        return self.minutes_var.get() and interval == self.patterns_interval

    def _minutes_rule(self, pattern):
        """Texto da regra pelos candles marcados: 'candle 4 em minutos final 2/7 e candle 9 em final 7/2'."""
        entry  = len(pattern.trigger) + 1
        before = entry - pattern.minutes_candle
        marked = '/'.join(str((m - before) % 10) for m in pattern.entry_minutes)
        last   = '/'.join(str(m) for m in pattern.entry_minutes)

        return f'candle {pattern.minutes_candle} em minutos final {marked} e candle {entry} em final {last}'

    def _refresh_patterns(self):
        interval = self.interval_var.get()

        self.patterns_frame.configure(text=f'Padroes carregados ({interval})')

        for pattern, label in zip(self.patterns, self.pattern_labels):
            if not pattern.entry_minutes:
                timing = 'entrada em qualquer horario'
            elif not pattern.minutes_optional or self._use_entry_minutes(interval):
                timing = self._minutes_rule(pattern)
            elif not self.minutes_var.get():
                timing = 'entrada em qualquer horario (filtro de minutos desligado)'
            else:
                timing = f'horario ignorado (regra so vale em {self.patterns_interval})'

            target = (DIRECTIONS[pattern.direction] if pattern.direction
                      else f'alerta no candle {len(pattern.trigger) + 1}')

            if pattern.interval:
                timing = f'sempre em {pattern.interval}  |  {timing}'

            label.configure(text=f'{pattern.name}:  {" ".join(pattern.trigger)}  ->  {target}  |  {timing}')

    # ------------------------------------------------------------- symbols

    def _on_symbols_loaded(self, symbols):
        """symbols = {nome exibido: identificador na API}, so com os ativos disponiveis."""
        added   = symbols.keys() - self.symbols.keys()
        removed = self.symbols.keys() - symbols.keys()
        first   = not self.symbols

        self.symbols     = dict(sorted(symbols.items()))
        self.symbol_keys = {normalize_symbol(name): name for name in self.symbols}
        self._show_symbols()

        if first:
            self.write_log(f'{len(symbols)} {self.SYMBOLS_LABEL}')
        elif added or removed:
            self.write_log(f'Lista atualizada: {len(symbols)} {self.SYMBOLS_LABEL} '
                           f'(+{len(added)} / -{len(removed)})')

        for name in self._watched_names() & removed:
            self.write_log(f'{name} deixou de estar ativo', 'error')

    def _resolve_symbol(self, text):
        """Nome exibido do ativo digitado, aceitando variacoes (EURUSD, eur/usd); None se nao existe."""
        return self.symbol_keys.get(normalize_symbol(text))

    def _on_interval_selected(self, _event=None):
        self._refresh_patterns()

        # Trocar o tempo com a procura ativa reinicia o stream no novo tempo.
        if self.stream and self.interval_var.get() != self.stream.interval:
            self.stop_search()
            self.start_search()

    # -------------------------------------------------------------- search

    def toggle_search(self):
        if self.stream:
            self.stop_search()
        else:
            self.start_search()

    def stop_search(self):
        if not self.stream:
            return

        self.stream.stop()
        self.write_log(f'Procura parada em {self.stream.name}')
        self.stream     = None
        self.close_time = None

        self.search_button.configure(text='Iniciar procura')
        self.status_label.configure(text='Procura parada')
        self.countdown_label.configure(text='')

    def _on_alerts_toggled(self):
        self.write_log('Alertas ativados' if self.alerts_var.get() else 'Alertas desativados')

    # -------------------------------------------------------------- alerts

    def _show_alert(self, signal, symbol, entry, on_click=None):
        # Sempre verde: o alerta avisa que um padrao se formou, sem destacar a direcao.
        color = COLORS[GREEN]

        alert = tk.Toplevel(self.root)
        alert.title('Padrao encontrado')
        alert.configure(background=color)
        alert.attributes('-topmost', True)

        tk.Label(alert, text=symbol, font=('sans', 26, 'bold'),
                 background=color, foreground='white').pack(padx=30, pady=(20, 6))
        tk.Label(alert, text=pattern_title(signal.pattern.name), font=('sans', 16, 'bold'),
                 background=color, foreground='white').pack(padx=30)
        tk.Label(alert, text=f'entrada {entry}', font=('sans', 12),
                 background=color, foreground='white').pack(padx=30, pady=(4, 10))

        if on_click:
            tk.Label(alert, text='clique para abrir o card', font=('sans', 10),
                     background=color, foreground='white').pack(padx=30, pady=(0, 10))

        ok = ttk.Button(alert, text='Fechar alerta', command=alert.destroy)
        ok.pack(pady=(0, 16))

        if on_click:
            def clicked(event):
                if event.widget is not ok:
                    alert.destroy()
                    on_click()

            # O bind na janela vale para os textos dentro dela; o botao continua so fechando.
            alert.configure(cursor='hand2')
            ok.configure(cursor='arrow')
            alert.bind('<Button-1>', clicked)

        alert.after(ALERT_SECONDS * 1000, alert.destroy)

        if self.sound_var.get():
            for i in range(3):
                self.root.after(i * 300, self.root.bell)

        return alert

    def _tick(self):
        if self.close_time:
            server_now = time() * 1000 - self.time_offset
            seconds    = max(0, int((self.close_time - server_now) / 1000))
            self.countdown_label.configure(text=f'Candle atual fecha em {seconds}s')

        self.root.after(1000, self._tick)

    def on_close(self):
        if self.stream:
            self.stream.stop()

        self.root.destroy()
