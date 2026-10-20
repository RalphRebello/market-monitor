import json
import logging
import os
import queue
import threading
from collections import deque
from itertools import count
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
MAX_ASSETS       = 20     # ativos acompanhados ao mesmo tempo
REQUEST_PREFIX   = 'monitor-'    # request_id dos pedidos de candles deste monitor
MAX_RETRY_WAIT   = 30     # espera maxima (s) para tentar de novo um ativo sem candle novo
FAST_RETRY_WAIT  = 0.5    # logo apos o fechamento, tenta de novo rapido: cada segundo atrasa o alerta
FAST_RETRIES     = 6
MAX_CANDLES      = 1000   # candles por pedido aceitos pela IQ Option
CLOCK_WINDOW     = 10     # segundos de amostras para acertar o relogio do servidor (timeSync ~1/s)


class IQOptionError(Exception):
    pass


class NoReplyError(IQOptionError):
    """A IQ Option nao respondeu: em geral, conexao caida (mesmo que o websocket pareca aberto)."""


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
        self.api         = None
        self.lock        = threading.Lock()
        self.replies     = {}                       # request_id -> resposta (None enquanto espera)
        self.replied     = threading.Condition()
        self.request_ids = count(1)
        self.pipeline    = True    # False se o servidor nao devolver o request_id nas respostas
        self.unmatched   = 0       # respostas de candles sem um request_id deste monitor
        self.offsets     = deque()             # (hora local, hora local - hora do servidor)
        self.clock_lock  = threading.Lock()
        self.init_data   = None                # ultima lista de binarias/blitz (get_all_init_v2)

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

        A biblioteca guarda a hora da ultima mensagem timeSync, que so chega de tempos em tempos: entre
        uma e outra o valor fica parado e atrasado (ate ~1,5 s). Por isso vale o menor desvio entre o
        relogio local e o do servidor visto nos ultimos CLOCK_WINDOW segundos, aplicado ao relogio local.

        Ate a primeira sincronizacao a biblioteca devolve time()/1000 (uma data de 1970);
        nesse caso, e em qualquer valor muito distante do relogio local, usa o relogio local.
        """
        try:
            server = self.api.get_server_timestamp()
        except Exception as error:
            raise IQOptionError(f'Falha ao ler a hora do servidor: {error}') from error

        now = time()
        if abs(server - now) > 3600:
            return now

        with self.clock_lock:
            self.offsets.append((now, now - server))

            while self.offsets[0][0] < now - CLOCK_WINDOW:
                self.offsets.popleft()

            return now - min(offset for _, offset in self.offsets)

    def _init_data(self):
        with self.lock:
            self.init_data = run_with_timeout(self.api.get_all_init_v2, REQUEST_TIMEOUT + 30)

        return self.init_data

    def get_payouts(self, asset_type='binary'):
        """Payout (0.87 = 87%) por ativo e prazo: {nome da API: {'binary': p, 'turbo': p}}.

        So opcoes binarias e blitz informam a comissao na lista de ativos (reaproveita a ultima lista
        baixada por get_assets); nos demais tipos devolve {}. Turbo e o prazo de 1 a 5 minutos; binary,
        os prazos maiores.
        """
        if asset_type not in ('binary', 'blitz'):
            return {}

        payouts = {}

        for option, name, _, active in init_actives(self.init_data or self._init_data() or {}, asset_type):
            try:
                commission = float(active['option']['profit']['commission'])
            except (KeyError, TypeError, ValueError):
                continue

            payouts.setdefault(name, {})[option] = (100 - commission) / 100

        return payouts

    def get_assets(self, asset_type='binary'):
        """Ativos de um tipo de ASSET_TYPES: {nome da API (EURUSD-OTC): (active_id, aberto)}."""
        if asset_type in ('binary', 'blitz'):
            data = self._init_data()
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
            for _, name, active_id, active in init_actives(data, asset_type, strict=True):
                add(name, active_id, active_is_open(active, now))

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

            try:
                self.api.api.getcandles(active_id, size, count, end_time)
            except Exception as error:
                raise NoReplyError(f'Falha ao pedir candles: {error}') from error

            start = time()
            while candles_api.candles_data is None:
                if time() - start > REQUEST_TIMEOUT or not self.connected:
                    raise NoReplyError('Sem resposta ao buscar candles')
                sleep(0.05)

            return candles_api.candles_data

    def get_candles_many(self, requests):
        """Candles de varios ativos de uma vez: requests = [(active_id, size, count, end_time)].

        Os pedidos vao todos juntos e cada resposta e reconhecida pelo request_id, entao 20 ativos
        levam praticamente o tempo de um. Devolve, na mesma ordem, a lista de candles ou um
        IQOptionError para cada pedido.
        """
        if not self.pipeline:
            return [self._try_get_candles(*request) for request in requests]

        with self.lock:
            if not self.connected:
                raise NoReplyError('Sem conexao com a IQ Option')

            try:
                self._hook_replies()
            except AttributeError as error:    # websocket ainda nao criado ou ja descartado
                raise NoReplyError(f'Websocket indisponivel: {error}') from error

            ids = [f'{REQUEST_PREFIX}{next(self.request_ids)}' for _ in requests]

            with self.replied:
                self.replies.update(dict.fromkeys(ids))
                self.unmatched = 0

            try:
                try:
                    for request_id, (active_id, size, count, end_time) in zip(ids, requests):
                        body = {'active_id': int(active_id), 'size': size, 'to': int(end_time), 'count': count}
                        self.api.api.send_websocket_request('sendMessage',
                                                            {'name': 'get-candles', 'version': '2.0',
                                                             'body': body},
                                                            request_id=request_id)
                except Exception as error:    # ex.: websocket fechado no meio do envio
                    raise NoReplyError(f'Falha ao pedir candles: {error}') from error

                deadline = time() + REQUEST_TIMEOUT

                with self.replied:
                    while any(self.replies[request_id] is None for request_id in ids):
                        left = deadline - time()
                        if left <= 0 or not self.connected:
                            break
                        self.replied.wait(min(left, 0.5))

                    answers   = [self.replies[request_id] for request_id in ids]
                    unmatched = self.unmatched

            finally:
                with self.replied:
                    for request_id in ids:
                        self.replies.pop(request_id, None)

        if all(answer is None for answer in answers):
            # Chegaram candles, mas sem o request_id: o servidor nao o devolve, entao passa a
            # pedir um ativo por vez. Sem resposta nenhuma, a conexao caiu.
            if unmatched and self.connected:
                self.pipeline = False
                return self.get_candles_many(requests)

            raise NoReplyError('Sem resposta da IQ Option ao buscar candles')

        return [candles_from_reply(answer) for answer in answers]

    def _try_get_candles(self, *request):
        try:
            return self.get_candles(*request)
        except IQOptionError as error:
            return error

    def _hook_replies(self):
        """Intercepta as respostas do websocket com um request_id deste monitor.

        A biblioteca guarda so a ultima resposta de candles (candles_data), o que obriga a pedir um
        ativo por vez. A cada login ela cria um websocket novo, entao o gancho e refeito quando falta.
        """
        wss = self.api.api.websocket_client.wss

        if getattr(wss, 'monitor_hooked', False):
            return

        original = wss.on_message

        def on_message(app, message):
            try:
                if isinstance(message, str) and '"candles"' in message:
                    reply = json.loads(message)

                    with self.replied:
                        if reply.get('request_id') in self.replies:
                            self.replies[reply['request_id']] = reply
                            self.replied.notify_all()
                        elif (reply.get('name') == 'candles'
                              and not str(reply.get('request_id', '')).startswith(REQUEST_PREFIX)):
                            self.unmatched += 1    # respostas atrasadas de lotes anteriores nao contam
            except (ValueError, AttributeError):
                pass

            original(message)

        wss.on_message     = on_message
        wss.monitor_hooked = True


def init_actives(data, asset_type, strict=False):
    """Ativos de binarias/blitz da lista get_all_init_v2: (prazo, nome da API, active_id, dados).

    O nome vem como "front.EURUSD-OTC". strict: falta de um prazo na lista e erro (KeyError).
    """
    for option in (('binary', 'turbo') if asset_type == 'binary' else ('blitz',)):
        actives = data[option]['actives'] if strict else (data.get(option) or {}).get('actives', {})

        for active_id, active in actives.items():
            yield option, str(active['name']).split('.')[-1], active_id, active


def candles_from_reply(reply):
    if reply is None:
        return NoReplyError('Sem resposta ao buscar candles')

    msg = reply.get('msg')
    if isinstance(msg, dict) and isinstance(msg.get('candles'), list):
        return msg['candles']

    return IQOptionError(f'Resposta inesperada ao buscar candles: {str(msg)[:120]}')


class IQMultiStream(threading.Thread):
    """Acompanha os candles de varios ativos (ate MAX_ASSETS) numa unica thread.

    Envia para a fila da GUI eventos (stream_id, tipo, dados):
      history  {'name', 'candles', 'time_offset'}  candles recentes do ativo (sem alerta)
      kline    {'name', 'candle', 'live', 'time_offset'}  candle recem-fechado e o aberto (preco atual)
      revised  {'name', 'candle'}                  candle ja enviado que mudou na IQ Option (versao final)
      open     {'close_time'}                      abertura de um candle (contagem regressiva)
      asset    {'name', 'text', 'error'}           situacao de um ativo
      status   texto                               situacao geral
    """

    def __init__(self, stream_id, session, assets, interval, events):
        super().__init__(daemon=True)
        self.stream_id   = stream_id
        self.session     = session
        self.assets      = dict(assets)    # nome exibido (EUR/USD) -> active_id
        self.commands    = queue.Queue()   # ('add', nome, active_id) / ('remove', nome, None)
        self.interval    = interval
        self.size        = INTERVAL_SECONDS[interval]
        self.size_ms     = self.size * 1000
        self.events      = events
        self._stop_event = threading.Event()
        self.offset      = 0.0    # time() - hora do servidor, em segundos

        self.last_closed = dict.fromkeys(self.assets)       # None: falta carregar o historico
        self.retry_at    = dict.fromkeys(self.assets, 0)    # ms do servidor
        self.misses      = dict.fromkeys(self.assets, 0)
        self.asset_text  = dict.fromkeys(self.assets)
        self.last_prices = dict.fromkeys(self.assets)    # (open, close) do ultimo candle enviado

    @property
    def name(self):
        return next(iter(self.assets)) if len(self.assets) == 1 else f'{len(self.assets)} ativos'

    def stop(self):
        self._stop_event.set()

    def add_asset(self, name, active_id):
        """Passa a acompanhar mais um ativo sem reiniciar os outros (chamado pela GUI)."""
        self.commands.put(('add', name, active_id))

    def remove_asset(self, name):
        self.commands.put(('remove', name, None))

    def _apply_commands(self):
        """Aplica as mudancas de ativos pedidas pela GUI, sempre entre um lote e outro."""
        while True:
            try:
                command, name, active_id = self.commands.get_nowait()
            except queue.Empty:
                return

            if command == 'add':
                self.assets[name]      = active_id
                self.last_closed[name] = None    # o historico do ativo novo vem no proximo lote
                self.retry_at[name]    = 0
                self.misses[name]      = 0
                self.asset_text[name]  = None
                self.last_prices[name] = None
            else:
                for table in (self.assets, self.last_closed, self.retry_at, self.misses, self.asset_text,
                              self.last_prices):
                    table.pop(name, None)

    def emit(self, kind, payload=None):
        self.events.put((self.stream_id, kind, payload))

    def server_now(self):
        return time() - self.offset

    def run(self):
        backoff      = 1
        online       = False
        current_open = None
        reconnect    = False    # apos uma falha, reconecta mesmo que o websocket pareca aberto

        while not self._stop_event.is_set():
            try:
                if reconnect or not self.session.connected:
                    self.emit('status', 'Reconectando na IQ Option...')
                    self.session.reconnect()
                    reconnect = False

                self._apply_commands()
                self.offset = time() - self.session.server_time()    # amostra o relogio a cada volta
                open_time   = int(self.server_now() * 1000 // self.size_ms) * self.size_ms

                if open_time != current_open:
                    current_open = open_time
                    self.emit('open', {'close_time': open_time + self.size_ms - 1})

                due = self._due(open_time)
                if due:
                    self._fetch(due, open_time)

                if not online:
                    online, backoff = True, 1
                    self.emit('status', f'Conectado em {self.name} ({self.interval})')

            # Qualquer erro (inclusive excecoes inesperadas da biblioteca com a conexao caida) leva a
            # uma nova conexao; a thread nunca termina sozinha enquanto a procura estiver ligada.
            except Exception as error:
                if self._stop_event.is_set():
                    break

                online, reconnect = False, True
                self.emit('status', f'Conexao perdida ({error}). Reconectando em {backoff}s...')
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue

            self._stop_event.wait(0.2)

        self.emit('status', 'Procura parada')

    def _due(self, open_time):
        """Ativos que precisam de candles: sem historico ou com um candle fechado ainda nao recebido."""
        now_ms = self.server_now() * 1000
        closed = now_ms - open_time >= CLOSE_FETCH_WAIT * 1000

        return [name for name, last in self.last_closed.items()
                if now_ms >= self.retry_at[name]
                and (last is None or (closed and open_time - self.size_ms > last))]

    def _fetch(self, due, open_time):
        """Busca os candles dos ativos pendentes num unico lote de pedidos."""
        now         = self.server_now()
        modes       = []
        requests    = []

        for name in due:
            last    = self.last_closed[name]
            missing = None if last is None else (open_time - last) // self.size_ms - 1

            # Sem historico ou com muitos candles perdidos (reconexao): recarrega sem alertas.
            history = missing is None or missing > 3
            modes.append(history)
            requests.append((self.assets[name], self.size,
                             HISTORY_LIMIT if history else missing + 2, now))

        results = self.session.get_candles_many(requests)

        # Nenhum ativo respondeu: e a conexao, nao os ativos.
        if all(isinstance(r, NoReplyError) for r in results):
            raise results[0]

        for name, history, result in zip(due, modes, results):
            if isinstance(result, IQOptionError):
                self._set_asset(name, str(result), error=True)
                self._retry_later(name, fast=False)
                continue

            candles = [iq_candle_to_candle(c, now) for c in result]
            last    = self.last_closed[name]
            closed  = sorted((c for c in candles if c['is_closed'] and (last is None or c['open_time'] > last)),
                             key=lambda c: c['open_time'])
            live    = next((c for c in candles if not c['is_closed']), None)

            if history:
                self.emit('history', {'name': name, 'candles': candles, 'time_offset': self.offset * 1000})
            else:
                self._check_revision(name, candles)

                for candle in closed:
                    self.emit('kline', {'name': name, 'candle': candle, 'time_offset': self.offset * 1000,
                                        'live': live if candle is closed[-1] else None})

            if closed:
                self.last_closed[name] = closed[-1]['open_time']
                self.last_prices[name] = (closed[-1]['open'], closed[-1]['close'])
                self.misses[name]      = 0
                self.retry_at[name]    = 0
            else:
                # Ativo fechado ou atraso do servidor: espera cada vez mais antes de tentar de novo.
                self._retry_later(name)

            last = self.last_closed[name]
            if last is None or last < (now - 3 * self.size) * 1000:
                self._set_asset(name, 'sem cotacao recente (ativo fechado?)', error=True)
            else:
                self._set_asset(name, 'ao vivo')

    def _check_revision(self, name, candles):
        """O lote seguinte traz de novo o ultimo candle enviado: se o preco mudou, envia a versao final.

        O candle e lido logo apos o fechamento; um tick atrasado pode mudar o fechamento (e ate a cor).
        """
        last   = self.last_closed[name]
        before = self.last_prices[name]
        again  = next((c for c in candles if c['open_time'] == last and c['is_closed']), None)

        if again is not None and before is not None and (again['open'], again['close']) != before:
            self.last_prices[name] = (again['open'], again['close'])
            self.emit('revised', {'name': name, 'candle': again})

    def _retry_later(self, name, fast=True):
        """fast: candle atrasado (tenta de novo logo); erro do ativo vai direto para a espera crescente."""
        self.misses[name]  += 1
        misses              = self.misses[name] if fast else self.misses[name] + FAST_RETRIES
        wait                = (FAST_RETRY_WAIT if misses <= FAST_RETRIES
                               else min(2 * (misses - FAST_RETRIES), MAX_RETRY_WAIT))
        self.retry_at[name] = (self.server_now() + wait) * 1000

    def _set_asset(self, name, text, error=False):
        if self.asset_text[name] != text:
            self.asset_text[name] = text
            self.emit('asset', {'name': name, 'text': text, 'error': error})
