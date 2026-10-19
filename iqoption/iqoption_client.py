import json
import logging
import os
import threading
from time import sleep, time

from iqoptionapi.stable_api import IQ_Option


# A biblioteca registra muitos avisos internos; so erros interessam aqui.
logging.getLogger('iqoptionapi').setLevel(logging.ERROR)

INTERVAL_SECONDS = {
    '1m'  : 60,
    '5m'  : 5 * 60,
    '15m' : 15 * 60,
    '30m' : 30 * 60,
    '1h'  : 60 * 60,
}

# Tipos de ativo como na IQ Option -> tipo usado na API
ASSET_TYPES = {
    'Opcoes binarias' : 'binary',
    'Blitz'           : 'blitz',
    'Opcoes digitais' : 'digital',
    'Forex'           : 'forex',
    'Criptomoedas'    : 'crypto',
    'CFD / Acoes'     : 'cfd',
}

# Pedidos atuais das listas de ativos (os da biblioteca sao recusados com "Invalid contract").
# A resposta chega como mensagem "underlying-list", guardada pela biblioteca em underlying_list_data.
UNDERLYING_REQUESTS = {
    'digital' : ('digital-option-instruments.get-underlying-list', '3.0'),
    'forex'   : ('marginal-forex-instruments.get-underlying-list', '1.0'),
    'crypto'  : ('marginal-crypto-instruments.get-underlying-list', '1.0'),
    'cfd'     : ('marginal-cfd-instruments.get-underlying-list', '1.0'),
}

HISTORY_LIMIT    = 30
REQUEST_TIMEOUT  = 15     # segundos para respostas do websocket da IQ Option
CONNECT_TIMEOUT  = 40
CLOSE_FETCH_WAIT = 0.3    # espera apos o fechamento antes de buscar o candle fechado


class IQOptionError(Exception):
    pass


def iq_candle_to_candle(c, server_now):
    return {
        'open_time'  : c['from'] * 1000,
        'open'       : float(c['open']),
        'high'       : float(c['max']),
        'low'        : float(c['min']),
        'close'      : float(c['close']),
        'volume'     : float(c.get('volume', 0)),
        'close_time' : c['to'] * 1000 - 1,
        'is_closed'  : c['to'] <= server_now,
    }


def to_seconds(timestamp):
    """Aceita timestamps em segundos ou milissegundos."""
    return timestamp / 1000 if timestamp > 1e11 else timestamp


def schedule_periods(schedule):
    for period in schedule:
        if isinstance(period, dict):
            yield to_seconds(period['open']), to_seconds(period['close'])
        else:
            yield to_seconds(period[0]), to_seconds(period[1])


def active_is_open(active, now, schedule_required=False):
    """Ativo habilitado, nao suspenso e dentro do horario de negociacao.

    Opcoes binarias nem sempre informam horario (sem horario = aberto). Nos demais tipos o
    horario e a referencia da propria IQ Option: sem nenhum periodo, o ativo esta fechado.
    """
    enabled = active.get('enabled', active.get('is_enabled'))    # digitais usam is_enabled

    if enabled is False or active.get('is_suspended'):
        return False

    try:
        periods = list(schedule_periods(active.get('schedule') or []))
    except (KeyError, IndexError, TypeError):
        return True    # formato desconhecido: vale so enabled/is_suspended

    if not periods:
        return not schedule_required

    return any(start <= now < end for start, end in periods)


CURRENCIES = {
    'AUD', 'BRL', 'CAD', 'CHF', 'CNH', 'CNY', 'CZK', 'DKK', 'EUR', 'GBP', 'HKD', 'HUF', 'IDR', 'ILS',
    'INR', 'JPY', 'KRW', 'MXN', 'NOK', 'NZD', 'PLN', 'RUB', 'SEK', 'SGD', 'THB', 'TRY', 'USD', 'ZAR',
    'XAU', 'XAG', 'BTC', 'ETH', 'LTC', 'XRP', 'BCH', 'EOS', 'TRX', 'ADA', 'DOT', 'SOL', 'BNB',
}


# Moedas de cotacao: XXXUSD com qualquer XXX (ex.: cripto como ZECUSD) e um par.
QUOTES = {'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD', 'NZD', 'BRL'}


def display_name(name):
    """Nome como aparece na IQ Option: EURUSD -> EUR/USD, EURUSD-OTC -> EUR/USD (OTC), AIG-op -> AIG."""
    base, *suffixes = name.split('-')
    suffix = ' '.join(s for s in suffixes if s != 'op')    # "-op" nao aparece na plataforma

    if base.isalpha() and base.isupper():
        if len(base) == 6 and base[:3] in CURRENCIES and base[3:] in CURRENCIES:
            base = f'{base[:3]}/{base[3:]}'
        elif 6 <= len(base) <= 9 and base[-3:] in QUOTES:    # cripto: ATOMUSD -> ATOM/USD
            base = f'{base[:-3]}/{base[-3:]}'

    return f'{base} ({suffix})' if suffix else base


def dump_assets(asset_type, data, now):
    """Com IQ_DUMP_ASSETS=<arquivo>, grava os dados brutos dos ativos para diagnostico.

    So contem a lista de ativos do tipo consultado (nada da conta); serve para conferir
    por que um ativo aparece como aberto ou fechado.
    """
    path = os.environ.get('IQ_DUMP_ASSETS')
    if not path:
        return

    snapshot = {
        'asset_type'  : asset_type,
        'server_time' : now,
        'local_time'  : time(),
        'data'        : data,
    }

    with open(path, 'w', encoding='utf-8') as file:
        json.dump(snapshot, file, indent=2, default=str)


def run_with_timeout(func, timeout, *args):
    """Executa func numa thread; a biblioteca faz esperas sem limite e pode travar."""
    result = {}

    def target():
        try:
            result['value'] = func(*args)
        except Exception as error:
            result['error'] = error

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        raise IQOptionError(f'Sem resposta da IQ Option em {timeout}s')

    if 'error' in result:
        raise IQOptionError(str(result['error'])) from result['error']

    return result['value']


class IQSession:
    """Conexao unica com a IQ Option (a biblioteca usa estado global, entao so pode haver uma)."""

    def __init__(self):
        self.api  = None
        self.lock = threading.Lock()

    @property
    def connected(self):
        return self.api is not None and self.api.check_connect()

    def connect(self, email, password):
        with self.lock:
            self.api = IQ_Option(email, password)
            check, reason = run_with_timeout(self.api.connect, CONNECT_TIMEOUT)

            if not check:
                self.api = None
                raise IQOptionError(f'Falha no login: {str(reason)[:300]}')

    def reconnect(self):
        with self.lock:
            if self.api is None:
                raise IQOptionError('Sessao nao iniciada')

            check, reason = run_with_timeout(self.api.connect, CONNECT_TIMEOUT)

            if not check:
                raise IQOptionError(f'Falha ao reconectar: {str(reason)[:300]}')

    def close(self):
        if self.api is not None:
            try:
                self.api.api.close()
            except Exception:
                pass

    def server_time(self):
        """Hora do servidor da IQ Option, em segundos.

        Ate a primeira sincronizacao a biblioteca devolve time()/1000 (uma data de 1970);
        nesse caso, e em qualquer valor muito distante do relogio local, usa o relogio local.
        """
        server = self.api.get_server_timestamp()

        if abs(server - time()) > 3600:
            return time()

        return server

    def get_assets(self, asset_type='binary'):
        """Ativos de um tipo de ASSET_TYPES: {nome da API (EURUSD-OTC): (active_id, aberto)}."""
        if asset_type in ('binary', 'blitz'):
            with self.lock:
                data = run_with_timeout(self.api.get_all_init_v2, REQUEST_TIMEOUT + 30)
        else:
            data = self._request('underlying_list_data', self._send_underlying_request, asset_type)

        if not data:
            raise IQOptionError('Lista de ativos indisponivel')

        now = self.server_time()
        dump_assets(asset_type, data, now)

        try:
            return self._parse_assets(asset_type, data, now)
        except (KeyError, TypeError, ValueError) as error:
            raise IQOptionError(f'Formato inesperado da lista de ativos ({error})') from error

    def _parse_assets(self, asset_type, data, now):
        assets = {}

        def add(name, active_id, is_open):
            if active_id is None:
                return

            _, was_open = assets.get(name, (None, False))
            assets[name] = (int(active_id), is_open or was_open)

        if asset_type in ('binary', 'blitz'):
            options = ('binary', 'turbo') if asset_type == 'binary' else ('blitz',)

            for option in options:
                for active_id, active in data[option]['actives'].items():
                    add(str(active['name']).split('.')[-1], active_id, active_is_open(active, now))

        else:
            # Digitais vem em "underlying"; forex, cripto e CFD em "items".
            for item in data.get('underlying') or data.get('items') or []:
                add(item.get('name') or item['underlying'], item['active_id'],
                    active_is_open(item, now, schedule_required=True))

        return assets

    def _send_underlying_request(self, asset_type):
        name, version = UNDERLYING_REQUESTS[asset_type]
        self.api.api.send_websocket_request('sendMessage',
                                            {'name': name, 'version': version, 'body': {}})

    def _request(self, attribute, send, *args):
        """Envia um pedido pelo websocket e espera a resposta em self.api.api.<attribute>, com timeout.

        Substitui as funcoes da biblioteca para listas de ativos, que usam pedidos desatualizados
        e repetem o pedido para sempre.
        """
        with self.lock:
            setattr(self.api.api, attribute, None)
            send(*args)

            start = time()
            while getattr(self.api.api, attribute) is None:
                if time() - start > REQUEST_TIMEOUT or not self.connected:
                    raise IQOptionError('Sem resposta ao buscar a lista de ativos')
                sleep(0.05)

            return getattr(self.api.api, attribute)

    def get_candles(self, active_id, size, count, end_time):
        """Candles ate end_time (segundos). Substitui o get_candles da biblioteca, que nao tem timeout."""
        with self.lock:
            candles_api = self.api.api.candles
            candles_api.candles_data = None

            self.api.api.getcandles(active_id, size, count, end_time)

            start = time()
            while candles_api.candles_data is None:
                if time() - start > REQUEST_TIMEOUT or not self.connected:
                    raise IQOptionError('Sem resposta ao buscar candles')
                sleep(0.05)

            return candles_api.candles_data


class IQCandleStream(threading.Thread):
    """Acompanha os candles de um ativo e envia eventos no mesmo formato do KlineStream da Binance."""

    def __init__(self, stream_id, session, name, active_id, interval, events):
        super().__init__(daemon=True)
        self.stream_id   = stream_id
        self.session     = session
        self.name        = name    # nome exibido (EUR/USD)
        self.active_id   = active_id
        self.interval    = interval
        self.size        = INTERVAL_SECONDS[interval]
        self.events      = events
        self._stop_event = threading.Event()
        self.offset      = 0.0    # time() - hora do servidor, em segundos

    def stop(self):
        self._stop_event.set()

    def emit(self, kind, payload=None):
        self.events.put((self.stream_id, kind, payload))

    def server_now(self):
        return time() - self.offset

    def fetch(self, count):
        self.offset = time() - self.session.server_time()
        now         = self.server_now()
        raw         = self.session.get_candles(self.active_id, self.size, count, now)

        return [iq_candle_to_candle(c, now) for c in raw]

    def run(self):
        backoff = 1

        while not self._stop_event.is_set():
            try:
                if not self.session.connected:
                    self.emit('status', 'Reconectando na IQ Option...')
                    self.session.reconnect()

                candles = self.fetch(HISTORY_LIMIT)
                self.emit('history', {'candles': candles, 'time_offset': self.offset * 1000})
                self.emit('status', f'Conectado em {self.name} ({self.interval})')
                backoff = 1

                closed = [c['open_time'] for c in candles if c['is_closed']]
                if not closed:
                    raise IQOptionError('Nenhum candle recebido')

                last_closed = max(closed)
                if last_closed < (self.server_now() - 3 * self.size) * 1000:
                    self.emit('status', f'{self.name} sem cotacao recente (ativo fechado?)')

                self._follow(last_closed)

            except IQOptionError as error:
                if self._stop_event.is_set():
                    break

                self.emit('status', f'Conexao perdida ({error}). Reconectando em {backoff}s...')
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)

        self.emit('status', 'Procura parada')

    def _follow(self, last_closed):
        """Avisa o candle em andamento e busca cada candle assim que ele fecha."""
        size_ms        = self.size * 1000
        current_open   = None

        while not self._stop_event.is_set():
            now_ms    = self.server_now() * 1000
            open_time = int(now_ms // size_ms) * size_ms

            if open_time != current_open:
                current_open = open_time
                self.emit('kline', {'open_time'  : open_time,
                                    'close_time' : open_time + size_ms - 1,
                                    'is_closed'  : False})

            if open_time - size_ms > last_closed:
                self._stop_event.wait(CLOSE_FETCH_WAIT)
                previous = last_closed

                for candle in self.fetch(3):
                    if candle['is_closed'] and candle['open_time'] > last_closed:
                        self.emit('kline', candle)
                        last_closed = candle['open_time']

                # Sem candle novo (ativo fechado ou atraso do servidor): espera mais antes de tentar.
                if last_closed == previous:
                    self._stop_event.wait(2)

            self._stop_event.wait(0.2)
