import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from iqoption.iqoption_client import (ASSET_TYPES, INTERVAL_SECONDS, IQCandleStream, IQOptionError,
                                      IQSession, display_name)
from binance.monitor_patterns import COLORS, PATTERNS_FILE, MonitorApp
from core.pattern_detector import DIRECTIONS, PatternDetector, PatternError, ms_to_datetime


DEFAULT_ASSET      = 'EUR/USD'
DEFAULT_ASSET_TYPE = 'Opcoes binarias'
ASSETS_REFRESH_MS  = 5 * 60 * 1000

# Termometro: cor da barra conforme a fracao do gatilho ja formada (frio -> quente).
HEAT_COLORS = ((0.5, '#3498db'), (0.75, '#f1c40f'), (1.0, '#e67e22'))
HEAT_DONE   = '#f1c40f'
THERMO_ROW  = 26


class IQMonitorApp(MonitorApp):
    """Mesmo monitor de padroes da Binance, com os candles vindo da IQ Option."""

    def __init__(self, root):
        self.session      = IQSession()
        self.assets_job   = None
        self.iq_events    = queue.Queue()
        self.email_var    = tk.StringVar(value=os.environ.get('IQ_EMAIL', ''))
        self.password_var = tk.StringVar(value=os.environ.get('IQ_PASSWORD', ''))
        self.type_var     = tk.StringVar(value=DEFAULT_ASSET_TYPE)

        super().__init__(root)

        self.root.title('Monitor de Padroes - IQ Option')
        self.root.geometry('1000x780')
        self.symbol_var.set(DEFAULT_ASSET)

        self.interval_box['values'] = list(INTERVAL_SECONDS)
        if self.interval_var.get() not in INTERVAL_SECONDS:
            self.interval_var.set('1m')
            self._refresh_patterns()

        self.root.after(100, self._process_iq_events)

    # ------------------------------------------------------------------ UI

    @property
    def SYMBOLS_LABEL(self):
        return f'ativos abertos ({self.type_var.get()})'

    def _build_ui(self):
        super()._build_ui()

        # Seletor do tipo de ativo, antes do campo "Par" na linha de controles.
        top   = self.symbol_box.master
        first = top.pack_slaves()[0]

        ttk.Label(top, text='Tipo:').pack(side='left', before=first)

        self.type_box = ttk.Combobox(top, textvariable=self.type_var, width=16,
                                     values=list(ASSET_TYPES), state='readonly')
        self.type_box.pack(side='left', padx=(4, 12), before=first)
        self.type_box.bind('<<ComboboxSelected>>', self._on_type_selected)

        thermo = ttk.LabelFrame(self.root, text='Termometro dos padroes', padding=6)
        thermo.pack(fill='x', padx=10, pady=(0, 8), before=self.patterns_frame)

        self.thermo = tk.Canvas(thermo, height=len(self.patterns) * THERMO_ROW + 4,
                                background='#1f2630', highlightthickness=0)
        self.thermo.pack(fill='x')
        self._draw_thermometer()

        login = ttk.Frame(self.root, padding=(10, 10, 10, 0))
        login.pack(fill='x', before=self.root.pack_slaves()[0])

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

    def _draw_strip(self):
        super()._draw_strip()
        self._draw_thermometer()

    def _draw_thermometer(self):
        """Uma linha por padrao: candles do gatilho ja formados, barra de progresso e o que falta."""
        self.thermo.delete('all')

        for row, (pattern, formed, entry) in enumerate(self.detector.pattern_progress()):
            size     = len(pattern.trigger)
            fraction = formed / size
            y        = 4 + row * THERMO_ROW
            mid      = y + 9
            done     = formed == size

            self.thermo.create_text(8, mid, anchor='w', text=pattern.name,
                                    fill='#e8e8e8', font=('TkDefaultFont', 9, 'bold'))

            # Candles do gatilho: preenchidos os ja formados; o proximo esperado com contorno tracejado.
            for i, color in enumerate(pattern.trigger):
                x = 170 + i * 20
                if i < formed:
                    self.thermo.create_rectangle(x, y + 2, x + 14, y + 16, fill=COLORS[color], outline='')
                elif i == formed:
                    self.thermo.create_rectangle(x, y + 2, x + 14, y + 16, outline=COLORS[color],
                                                 dash=(3, 2), width=2)
                else:
                    self.thermo.create_rectangle(x, y + 2, x + 14, y + 16, outline='#4a5568')

            # Termometro: bulbo + tubo, enchendo com cor mais quente conforme o padrao avanca.
            heat = HEAT_DONE if done else next(c for limit, c in HEAT_COLORS if fraction <= limit)
            x0, x1 = 352, 552
            fill = heat if formed else '#4a5568'

            self.thermo.create_rectangle(x0 + 6, y + 4, x1, y + 14, fill='#2c3746', outline='#4a5568')
            if formed:
                self.thermo.create_rectangle(x0 + 6, y + 4, x0 + 6 + (x1 - x0 - 6) * fraction, y + 14,
                                             fill=heat, outline='')
            self.thermo.create_oval(x0 - 4, y, x0 + 14, y + 18, fill=fill, outline='#4a5568')

            self.thermo.create_text(x1 + 8, mid, anchor='w', text=f'{fraction:4.0%}',
                                    fill=heat if formed else '#8a8f98', font=('TkDefaultFont', 9, 'bold'))

            if done:
                status = (f'FORMADO: {DIRECTIONS[pattern.direction]} na entrada '
                          f'{ms_to_datetime(entry).strftime("%H:%M")}')
                color  = COLORS[pattern.direction]
            elif formed:
                missing = size - formed
                status  = (f'{formed}/{size}  ' + ('falta 1 candle' if missing == 1 else f'faltam {missing} candles') +
                           f' | '
                           f'entrada prevista {ms_to_datetime(entry).strftime("%H:%M")}')
                color   = '#e8e8e8'
            else:
                status = f'0/{size}  aguardando o 1o candle ({pattern.trigger[0]})'
                color  = '#8a8f98'

            self.thermo.create_text(x1 + 50, mid, anchor='w', text=status, fill=color,
                                    font=('TkDefaultFont', 9, 'bold' if done else 'normal'))

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
        self.symbol_box['values'] = []

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

                    if not self._resolve_symbol(self.symbol_var.get()) and self.symbols:
                        self.symbol_var.set(next(iter(self.symbols)))

                elif kind == 'assets_error':
                    self.write_log(f'Nao foi possivel carregar {payload[0]}: {payload[1]}', 'error')

        except queue.Empty:
            pass

        self.root.after(100, self._process_iq_events)

    # -------------------------------------------------------------- search

    def start_search(self):
        typed = self.symbol_var.get().strip().upper()
        name  = self._resolve_symbol(typed)

        if not self.session.connected:
            messagebox.showwarning('IQ Option', 'Faca login antes de iniciar a procura.')
            return

        if not self.symbols:
            messagebox.showwarning('IQ Option', 'A lista de ativos ainda nao foi carregada.')
            return

        if name is None:
            messagebox.showwarning('Ativo invalido',
                                   f'{typed} nao esta aberto em {self.type_var.get()} na IQ Option.')
            return

        self.symbol_var.set(name)

        interval = self.interval_var.get()

        self.detector   = PatternDetector(self.patterns, interval,
                                          use_entry_minutes=self._use_entry_minutes(interval))
        self.close_time = None
        self._draw_strip()

        self.stream_id += 1
        self.stream = IQCandleStream(self.stream_id, self.session, name, self.symbols[name],
                                     interval, self.events)
        self.stream.start()

        self.search_button.configure(text='Parar procura')
        self.status_label.configure(text=f'Conectando em {name} ({interval})...')
        self.write_log(f'Procura iniciada em {name} ({interval})')

    def on_close(self):
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
