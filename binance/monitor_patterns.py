import asyncio
import json
import queue
import threading
import tkinter as tk
from time import time
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

import requests
from websockets.asyncio.client import connect

from binance.monitor_resquests_REST_v0 import BinanceAPIError, get_candles, get_server_time_offset
from core.pattern_detector import (DIRECTIONS, DOJI, GREEN, INTERVAL_MS, LOSS, OUTCOMES, PATTERNS_FILE,
                                   RED, WIN, PatternDetector, PatternError, candle_color, load_patterns,
                                   ms_to_datetime, signal_outcome)


WS_URL         = 'wss://stream.binance.com:9443/ws/{symbol}@kline_{interval}'
EXCHANGE_INFO  = 'https://api.binance.com/api/v3/exchangeInfo'
TICKER_24H     = 'https://api.binance.com/api/v3/ticker/24hr'
DEFAULT_SYMBOL = 'BTC/USDT'
HISTORY_LIMIT  = 30
ALERT_SECONDS  = 30
SYMBOLS_REFRESH_MS = 10 * 60 * 1000
STRIP_CANDLES  = 16

# Regra de horario de entrada (entry_minutes do patterns.json, ex.: 2/7) ligada ao abrir.
DEFAULT_ENTRY_MINUTES = False

COLORS = {
    GREEN : '#26a65b',
    RED   : '#e5262d',
    DOJI  : '#8a8f98',
}


# Tag do historico para cada resultado (empate sem destaque).
OUTCOME_TAGS = {
    WIN  : 'buy',
    LOSS : 'sell',
}


def new_stats():
    return {'signals': 0, 'win': 0, 'loss': 0, 'tie': 0}


def hit_rate(stats):
    """Acerto sobre os sinais decididos (empates nao contam); None sem resultados."""
    decided = stats['win'] + stats['loss']
    return stats['win'] / decided if decided else None


def normalize_symbol(text):
    """Chave de busca: 'eur/usd (otc)', 'EURUSD-OTC' e 'EUR/USD (OTC)' viram 'EURUSDOTC'."""
    return ''.join(ch for ch in text.upper() if ch.isalnum())


def get_symbols():
    """Pares SPOT ativos: {nome exibido na Binance (BTC/USDT): simbolo da API (BTCUSDT)}.

    Ativo = em negociacao e com pelo menos um negocio nas ultimas 24h.
    """
    r = requests.get(EXCHANGE_INFO, params={'permissions': 'SPOT'}, timeout=(3.05, 27))

    if r.status_code != requests.codes.ok:
        raise BinanceAPIError(f'Invalid exchange info | StatusCode: {r.status_code}')

    trading = {s['symbol']: f"{s['baseAsset']}/{s['quoteAsset']}" for s in r.json()['symbols']
               if s['status'] == 'TRADING' and s.get('isSpotTradingAllowed', True)}

    r = requests.get(TICKER_24H, params={'type': 'MINI'}, timeout=(3.05, 27))

    if r.status_code != requests.codes.ok:
        raise BinanceAPIError(f'Invalid 24h ticker | StatusCode: {r.status_code}')

    return {trading[t['symbol']]: t['symbol'] for t in r.json()
            if t['symbol'] in trading and t['count'] > 0}


def ws_kline_to_candle(k):
    return {
        'open_time'          : k['t'],
        'open'               : float(k['o']),
        'high'               : float(k['h']),
        'low'                : float(k['l']),
        'close'              : float(k['c']),
        'volume'             : float(k['v']),
        'close_time'         : k['T'],
        'quote_asset_volume' : float(k['q']),
        'number_of_trades'   : int(k['n']),
        'is_closed'          : k['x'],
    }


class KlineStream(threading.Thread):
    """Recebe os candles em tempo real via WebSocket e envia eventos para a fila da GUI."""

    def __init__(self, stream_id, symbol, interval, events, name=None):
        super().__init__(daemon=True)
        self.stream_id = stream_id
        self.symbol    = symbol           # simbolo da API (BTCUSDT)
        self.name      = name or symbol   # nome exibido (BTC/USDT)
        self.interval  = interval
        self.events    = events
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def emit(self, kind, payload=None):
        self.events.put((self.stream_id, kind, payload))

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        backoff = 1
        url     = WS_URL.format(symbol=self.symbol.lower(), interval=self.interval)

        while not self._stop_event.is_set():
            try:
                # Recarrega o historico a cada (re)conexao para nao deixar buracos.
                time_offset = await asyncio.to_thread(get_server_time_offset)
                candles     = await asyncio.to_thread(get_candles,
                                                      {'symbol'   : self.symbol,
                                                       'interval' : self.interval,
                                                       'limit'    : HISTORY_LIMIT},
                                                      time_offset)
                self.emit('history', {'candles': candles, 'time_offset': time_offset})

                async with connect(url, open_timeout=10) as ws:
                    self.emit('status', f'Conectado em {self.name} ({self.interval})')
                    backoff = 1

                    while not self._stop_event.is_set():
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=1)
                        except TimeoutError:
                            continue

                        data = json.loads(message)
                        if data.get('e') == 'kline':
                            self.emit('kline', ws_kline_to_candle(data['k']))

            except BinanceAPIError as error:
                self.emit('fatal', f'Binance API error: {error}')
                return

            except Exception as error:
                if self._stop_event.is_set():
                    break

                self.emit('status', f'Conexao perdida ({error}). Reconectando em {backoff}s...')
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

        self.emit('status', 'Procura parada')


class MonitorApp:

    SYMBOLS_LABEL = 'pares ativos'

    def __init__(self, root):
        self.root = root
        self.root.title('Monitor de Padroes - Opcoes Binarias')
        self.root.geometry('900x620')

        # O intervalo do JSON e o padrao e o unico em que entry_minutes (regra 2/7) se aplica.
        self.patterns_interval, self.patterns = load_patterns(PATTERNS_FILE)
        self.detector = PatternDetector(self.patterns, self.patterns_interval,
                                        use_entry_minutes=DEFAULT_ENTRY_MINUTES)

        self.events      = queue.Queue()
        self.stream      = None
        self.stream_id   = 0
        self.symbols     = {}    # nome exibido -> identificador na API da plataforma
        self.symbol_keys = {}    # normalize_symbol(nome) -> nome exibido
        self.time_offset = 0
        self.close_time  = None
        self.stats       = new_stats()

        self.symbol_var   = tk.StringVar(value=DEFAULT_SYMBOL)
        self.interval_var = tk.StringVar(value=self.patterns_interval)
        self.alerts_var   = tk.BooleanVar(value=True)
        self.sound_var    = tk.BooleanVar(value=True)
        self.minutes_var  = tk.BooleanVar(value=DEFAULT_ENTRY_MINUTES)

        self._build_ui()
        self._load_symbols()

        self.root.after(100, self._process_events)
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        self._build_controls()
        self._build_info()
        self._build_strip()
        self._build_patterns()
        self._build_log()

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

    def _build_asset_fields(self, top):
        """Campos de escolha do ativo, no inicio da linha de controles."""
        ttk.Label(top, text='Par:').pack(side='left')

        self.symbol_box = ttk.Combobox(top, textvariable=self.symbol_var, width=18)
        self.symbol_box.pack(side='left', padx=(4, 12))
        self.symbol_box.bind('<KeyRelease>', self._filter_symbols)
        self.symbol_box.bind('<<ComboboxSelected>>', self._on_symbol_selected)
        self.symbol_box.bind('<Return>', self._on_symbol_selected)

    def _build_info(self):
        info = ttk.Frame(self.root, padding=(10, 0))
        info.pack(fill='x')

        self.status_label = ttk.Label(info, text='Procura parada')
        self.status_label.pack(side='left')

        self.countdown_label = ttk.Label(info, text='')
        self.countdown_label.pack(side='right')

    def _build_strip(self):
        strip = ttk.LabelFrame(self.root, text='Ultimos candles fechados', padding=6)
        strip.pack(fill='x', padx=10, pady=8)

        self.strip = tk.Canvas(strip, height=58, background='#1f2630', highlightthickness=0)
        self.strip.pack(fill='x')

    def _build_patterns(self):
        self.patterns_frame = ttk.LabelFrame(self.root, text='Padroes carregados', padding=6)
        self.patterns_frame.pack(fill='x', padx=10)

        minutes = sorted({m for pattern in self.patterns for m in pattern.entry_minutes})
        if minutes:
            ttk.Checkbutton(self.patterns_frame, variable=self.minutes_var,
                            text=f'Exigir horario de entrada (minutos final '
                                 f'{"/".join(str(m) for m in minutes)}, so em {self.patterns_interval})',
                            command=self._on_minutes_toggled).pack(anchor='w', pady=(0, 4))

        self.pattern_labels = [ttk.Label(self.patterns_frame) for _ in self.patterns]
        for label in self.pattern_labels:
            label.pack(anchor='w')

        self._refresh_patterns()

    def _build_log(self):
        self.stats_label = ttk.Label(self.root, padding=(10, 6))
        self.stats_label.pack(fill='x')
        self._update_stats()

        self.log = ScrolledText(self.root, height=14, state='disabled', font=('monospace', 10))
        self.log.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.log.tag_configure('buy', foreground=COLORS[GREEN], font=('monospace', 10, 'bold'))
        self.log.tag_configure('sell', foreground=COLORS[RED], font=('monospace', 10, 'bold'))
        self.log.tag_configure('error', foreground='#d35400')

    def write_log(self, text, tag=None):
        now = ms_to_datetime(time() * 1000).strftime('%H:%M:%S')

        self.log.configure(state='normal')
        self.log.insert('end', f'[{now}] {text}\n', tag)
        self.log.see('end')
        self.log.configure(state='disabled')

    def _use_entry_minutes(self, interval):
        """A regra de horario (entry_minutes) so vale se estiver ligada e no tempo do patterns.json."""
        return self.minutes_var.get() and interval == self.patterns_interval

    def _on_minutes_toggled(self):
        if self.stream:
            self.detector.use_entry_minutes = self._use_entry_minutes(self.stream.interval)

        self._refresh_patterns()
        self._draw_strip()
        self.write_log('Regra de horario de entrada ' +
                       ('ligada' if self.minutes_var.get() else 'desligada: qualquer horario'))

    def _refresh_patterns(self):
        interval      = self.interval_var.get()
        check_minutes = self._use_entry_minutes(interval)

        self.patterns_frame.configure(text=f'Padroes carregados ({interval})')

        for pattern, label in zip(self.patterns, self.pattern_labels):
            if not pattern.entry_minutes:
                timing = 'entrada em qualquer horario'
            elif check_minutes:
                minutes = '/'.join(str(m) for m in pattern.entry_minutes)
                timing  = f'entrada em minutos final {minutes}'
            elif not self.minutes_var.get():
                timing = 'entrada em qualquer horario (regra de horario desligada)'
            else:
                timing = f'horario ignorado (regra so vale em {self.patterns_interval})'

            label.configure(text=f'{pattern.name}:  {" ".join(pattern.trigger)}  ->  '
                                 f'{DIRECTIONS[pattern.direction]}  |  {timing}')

    def _update_stats(self):
        s    = self.stats
        rate = hit_rate(s)
        self.stats_label.configure(text=f"Sinais: {s['signals']}   Win: {s['win']}   Loss: {s['loss']}   "
                                        f"Empate: {s['tie']}" +
                                        (f'   Acerto: {rate:.0%}' if rate is not None else ''))

    def _draw_strip(self):
        """Candles fechados; os ultimos (gatilho) numerados 1..8 e o candle de entrada como 9."""
        self.strip.delete('all')

        trigger_len = max(len(pattern.trigger) for pattern in self.patterns)
        colors      = self.detector.last_colors(STRIP_CANDLES)
        if not colors:
            return

        forming = self.detector.forming_patterns()
        window  = min(trigger_len, len(colors))
        first   = len(colors) - window
        x0      = lambda i: 8 + i * 30

        # Fundo destacando os candles comparados com os padroes (amarelo quando o padrao formou).
        self.strip.create_rectangle(x0(first) - 4, 2, x0(len(colors)) - 4, 56, fill='#2c3746',
                                    outline='#f1c40f' if forming else '', width=2)

        for i, color in enumerate(colors):
            x = x0(i)
            self.strip.create_rectangle(x, 22, x + 22, 52, fill=COLORS[color], outline='')

            if i >= first:
                number = trigger_len - (len(colors) - 1 - i)
                self.strip.create_text(x + 11, 12, text=str(number), fill='#e8e8e8',
                                       font=('TkDefaultFont', 9, 'bold'))

        # Candle de entrada (ainda aberto): contorno tracejado; seta e cor da operacao se o padrao formou.
        x       = x0(len(colors))
        outline = COLORS[forming[0].direction] if forming else '#8a8f98'

        self.strip.create_text(x + 11, 12, text=str(trigger_len + 1), fill=outline,
                               font=('TkDefaultFont', 9, 'bold'))
        self.strip.create_rectangle(x, 22, x + 22, 52, outline=outline, dash=(3, 2), width=2)

        if forming:
            pattern = forming[0]
            tip, base = (29, 45) if pattern.direction == GREEN else (45, 29)
            self.strip.create_polygon(x + 4, base, x + 18, base, x + 11, tip, fill=outline)
            self.strip.create_text(x + 32, 37, anchor='w', fill=outline,
                                   font=('TkDefaultFont', 10, 'bold'),
                                   text=f'{pattern.name}  |  entrada no candle {trigger_len + 1}: '
                                        f'{DIRECTIONS[pattern.direction]}')

    # ------------------------------------------------------------- symbols

    def _load_symbols(self):
        def worker():
            try:
                self.events.put((None, 'symbols', get_symbols()))
            except (BinanceAPIError, requests.exceptions.RequestException) as error:
                self.events.put((None, 'symbols_error', str(error)))

        threading.Thread(target=worker, daemon=True).start()

        # Pares podem ser suspensos ou listados a qualquer momento.
        self.root.after(SYMBOLS_REFRESH_MS, self._load_symbols)

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

    def _show_symbols(self):
        self.symbol_box['values'] = list(self.symbols)[:300]

    def _watched_names(self):
        """Ativos acompanhados pela procura em andamento."""
        return {self.stream.name} if self.stream else set()

    def _resolve_symbol(self, text):
        """Nome exibido do ativo digitado, aceitando variacoes (EURUSD, eur/usd); None se nao existe."""
        return self.symbol_keys.get(normalize_symbol(text))

    def _filter_symbols(self, event):
        if event.keysym in ('Return', 'Up', 'Down', 'Escape'):
            return

        typed = normalize_symbol(self.symbol_var.get())
        self.symbol_box['values'] = [name for key, name in self.symbol_keys.items()
                                     if typed in key][:300]

    def _on_symbol_selected(self, _event=None):
        name = self._resolve_symbol(self.symbol_var.get())
        if name:
            self.symbol_var.set(name)

        # Trocar o par com a procura ativa reinicia o stream no novo par.
        if self.stream and name != self.stream.name:
            self.stop_search()
            self.start_search()

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

    def start_search(self):
        typed = self.symbol_var.get().strip().upper()
        name  = self._resolve_symbol(typed)

        if not typed:
            messagebox.showwarning('Par invalido', 'Informe o par a ser monitorado.')
            return

        if self.symbols and name is None:
            messagebox.showwarning('Par invalido', f'{typed} nao esta ativo na Binance (SPOT).')
            return

        # Sem a lista de pares (falha ao carregar), usa o que foi digitado.
        symbol = self.symbols[name] if name else normalize_symbol(typed)
        name   = name or typed
        self.symbol_var.set(name)

        interval = self.interval_var.get()

        self.detector   = PatternDetector(self.patterns, interval,
                                          use_entry_minutes=self._use_entry_minutes(interval))
        self.close_time = None
        self._draw_strip()

        self.stream_id += 1
        self.stream = KlineStream(self.stream_id, symbol, interval, self.events, name=name)
        self.stream.start()

        self.search_button.configure(text='Parar procura')
        self.status_label.configure(text=f'Conectando em {name} ({interval})...')
        self.write_log(f'Procura iniciada em {name} ({interval})')

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

    # -------------------------------------------------------------- events

    def _process_events(self):
        try:
            while True:
                stream_id, kind, payload = self.events.get_nowait()

                if kind == 'symbols':
                    self._on_symbols_loaded(payload)

                elif kind == 'symbols_error':
                    self.write_log(f'Nao foi possivel carregar os pares: {payload}', 'error')

                # Ignora eventos de streams antigos (par trocado ou procura parada).
                elif self.stream is None or stream_id != self.stream_id:
                    continue

                elif kind == 'history':
                    self.time_offset = payload['time_offset']
                    self._log_results(self.detector.load_history(payload['candles']))
                    self._update_stats()
                    self._draw_strip()
                    self._log_history_signals()

                elif kind == 'kline':
                    self._on_kline(payload)

                elif kind == 'status':
                    self.status_label.configure(text=payload)
                    self.write_log(payload)

                elif kind == 'fatal':
                    self.write_log(payload, 'error')
                    self.stop_search()

        except queue.Empty:
            pass

        self.root.after(100, self._process_events)

    def _on_kline(self, candle):
        if not candle['is_closed']:
            self.close_time = candle['close_time']
            return

        signals, results = self.detector.add_closed_candle(candle)
        self._draw_strip()
        self._log_results(results)

        for signal in signals:
            self.stats['signals'] += 1
            self._on_signal(signal)

        self._update_stats()

    def _log_results(self, results):
        for signal, candle, outcome in results:
            self.stats[outcome] += 1
            self.write_log(f'Resultado {signal.pattern.name}: candle {candle_color(candle)} -> '
                           f'{OUTCOMES[outcome]}', OUTCOME_TAGS.get(outcome))

    def _log_history_signals(self):
        """Registra (sem alerta) os padroes que se formaram antes de a procura comecar."""
        for signal, entry_candle in self.detector.history_signals():
            entry = ms_to_datetime(signal.entry_open_time).strftime('%H:%M')

            if entry_candle is None:
                outcome = 'entrada em andamento'
            else:
                outcome = (f'candle {candle_color(entry_candle)} -> '
                           f'{OUTCOMES[signal_outcome(signal, entry_candle)]}')

            self.write_log(f'Historico (sem alerta): {signal.label} em {self.stream.name} | '
                           f'entrada {entry} | {signal.pattern.name} | {outcome}')

    def _on_signal(self, signal):
        symbol = self.stream.name
        entry  = ms_to_datetime(signal.entry_open_time).strftime('%H:%M')
        text   = (f'{signal.label} em {symbol} ({self.stream.interval}) | entrada {entry} | '
                  f'{signal.pattern.name}')

        self.write_log(f'SINAL: {text}', 'buy' if signal.direction == GREEN else 'sell')

        if self.alerts_var.get():
            self._show_alert(signal, symbol, entry)

    def _show_alert(self, signal, symbol, entry):
        color = COLORS[signal.direction]

        alert = tk.Toplevel(self.root)
        alert.title('Padrao encontrado')
        alert.configure(background=color)
        alert.attributes('-topmost', True)

        tk.Label(alert, text=signal.label, font=('sans', 26, 'bold'),
                 background=color, foreground='white').pack(padx=30, pady=(20, 6))
        tk.Label(alert, text=f'{symbol}  |  entrada {entry}', font=('sans', 14),
                 background=color, foreground='white').pack(padx=30)
        tk.Label(alert, text=signal.pattern.name, font=('sans', 11),
                 background=color, foreground='white').pack(padx=30, pady=(4, 10))
        ttk.Button(alert, text='OK', command=alert.destroy).pack(pady=(0, 16))

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


def main():
    root = tk.Tk()

    try:
        app = MonitorApp(root)
    except (PatternError, OSError, KeyError, json.JSONDecodeError) as error:
        messagebox.showerror('Erro nos padroes', f'Falha ao carregar {PATTERNS_FILE}:\n{error}')
        root.destroy()
        return

    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
