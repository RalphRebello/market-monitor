import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


tz = ZoneInfo('America/Sao_Paulo')

PATTERNS_FILE = Path(__file__).resolve().parent.parent / 'patterns' / 'patterns.json'

GREEN = 'G'
RED   = 'R'
DOJI  = 'D'

DIRECTIONS = {
    GREEN : 'COMPRA (CALL)',
    RED   : 'VENDA (PUT)',
}

# Resultado de um sinal: na opcao binaria, fechar no mesmo preco da entrada devolve o valor (empate).
WIN  = 'win'
LOSS = 'loss'
TIE  = 'tie'

OUTCOMES = {
    WIN  : 'WIN',
    LOSS : 'LOSS',
    TIE  : 'EMPATE',
}

INTERVAL_MS = {
    '1m'  : 60_000,
    '3m'  : 3 * 60_000,
    '5m'  : 5 * 60_000,
    '15m' : 15 * 60_000,
    '30m' : 30 * 60_000,
    '1h'  : 60 * 60_000,
}


class PatternError(Exception):
    pass


def ms_to_datetime(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, tz)


def candle_color(candle):
    if candle['close'] > candle['open']:
        return GREEN

    if candle['close'] < candle['open']:
        return RED

    return DOJI


@dataclass(frozen=True)
class Pattern:
    name          : str
    trigger       : str          # cores dos candles de gatilho, ex.: 'RGGRRRGR'
    direction     : str          # cor do candle de entrada (G ou R)
    entry_minutes : tuple = ()   # ultimo digito do minuto de abertura da entrada

    def matches_time(self, entry_open_time):
        if not self.entry_minutes:
            return True

        return ms_to_datetime(entry_open_time).minute % 10 in self.entry_minutes


@dataclass
class Signal:
    pattern         : Pattern
    entry_open_time : int
    entry_price     : float = None   # preco no momento do alerta; None = abertura do candle de entrada
    counted         : bool  = True   # False: alerta perdido (entrada ja fechada), fora do placar

    @property
    def direction(self):
        return self.pattern.direction

    @property
    def label(self):
        return DIRECTIONS[self.direction]


def signal_outcome(signal, entry_candle):
    """WIN, LOSS ou TIE comparando o fechamento do candle de entrada com o preco de entrada."""
    entry = entry_candle['open'] if signal.entry_price is None else signal.entry_price
    close = entry_candle['close']

    if close == entry:
        return TIE

    return WIN if (close > entry) == (signal.direction == GREEN) else LOSS


def load_patterns(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))

    interval = data.get('interval', '1m')
    if interval not in INTERVAL_MS:
        raise PatternError(f'Intervalo nao suportado: {interval}')

    patterns = []

    for item in data['patterns']:
        candles = ''.join(item['candles'].upper().replace(',', ' ').split())

        if len(candles) < 2 or set(candles) - {GREEN, RED}:
            raise PatternError(f"Sequencia invalida em '{item['name']}': {item['candles']}")

        patterns.append(Pattern(name          = item['name'],
                                trigger       = candles[:-1],
                                direction     = candles[-1],
                                entry_minutes = tuple(item.get('entry_minutes', ())),
                                ))

    if not patterns:
        raise PatternError(f'Nenhum padrao definido em {path}')

    return interval, patterns


class PatternDetector:
    """Guarda os candles fechados e procura os padroes a cada novo fechamento."""

    def __init__(self, patterns, interval='1m', use_entry_minutes=True, max_candles=200):
        self.patterns          = patterns
        self.interval_ms       = INTERVAL_MS[interval]
        self.use_entry_minutes = use_entry_minutes
        self.max_candles       = max_candles
        self.candles     = {}      # open_time -> candle fechado
        self.pending     = []      # sinais aguardando o fechamento do candle de entrada
        self.resolved    = {}      # entrada -> [[sinal, resultado]] ja conferidos (para revise)
        self.signaled    = set()   # (padrao, entrada) ja sinalizados, ao vivo ou no historico

    def reset(self):
        self.candles.clear()
        self.pending.clear()
        self.resolved.clear()
        self.signaled.clear()

    def load_history(self, candles):
        """Carrega candles fechados sem disparar sinais (sinais atrasados nao servem; ver history_signals).

        Retorna os resultados dos sinais pendentes cuja entrada veio no historico (ex.: apos uma
        reconexao), no mesmo formato de add_closed_candle.
        """
        results = []

        for candle in sorted(candles, key=lambda c: c['open_time']):
            if candle['is_closed']:
                results += self._resolve_pending(candle)
                self.candles[candle['open_time']] = candle

        self._trim()

        return results

    def add_closed_candle(self, candle):
        """Retorna (novos_sinais, resultados) onde resultados = [(sinal, candle_de_entrada, WIN/LOSS/TIE)]."""
        if candle['open_time'] in self.candles:
            return [], []

        self.candles[candle['open_time']] = candle
        self._trim()

        results = self._resolve_pending(candle)
        signals = self._find_signals(candle['open_time'] + self.interval_ms)

        self.pending.extend(signals)

        return signals, results

    def history_signals(self):
        """Padroes ja formados nos candles carregados, que nao geram alerta.

        Retorna [(sinal, candle_de_entrada ou None)]; None quando a entrada ainda nao fechou.
        Cada padrao/entrada so e informado uma vez, mesmo que o historico seja recarregado.
        """
        found = []

        for open_time in sorted(self.candles):
            for signal in self._find_signals(open_time + self.interval_ms):
                found.append((signal, self.candles.get(signal.entry_open_time)))

        return found

    def revise(self, candle):
        """Troca um candle ja guardado pela versao final.

        Retorna (cor_antiga ou None se a cor nao mudou, [(sinal, resultado_antigo, resultado_novo)]) com
        os resultados ja conferidos neste candle que mudaram (o resultado depende do fechamento, nao so da cor).
        """
        old = self.candles.get(candle['open_time'])
        if old is None:
            return None, []

        self.candles[candle['open_time']] = candle
        old_color = candle_color(old)
        changed   = []

        for item in self.resolved.get(candle['open_time'], []):
            signal, before = item
            after          = signal_outcome(signal, candle)

            if after != before:
                item[1] = after
                changed.append((signal, before, after))

        return (old_color if old_color != candle_color(candle) else None), changed

    def follow(self, signal):
        """Acompanha o resultado de um sinal do historico cuja entrada ainda nao fechou."""
        if signal not in self.pending:
            self.pending.append(signal)

    def last_colors(self, count, last_open_time=None):
        """Cores dos candles fechados consecutivos ate last_open_time (padrao: o mais recente)."""
        if not self.candles:
            return ''

        open_time = max(self.candles) if last_open_time is None else last_open_time
        colors    = []

        while len(colors) < count and open_time in self.candles:
            colors.append(candle_color(self.candles[open_time]))
            open_time -= self.interval_ms

        return ''.join(reversed(colors))

    def forming_patterns(self):
        """Padroes cujo gatilho bate com os ultimos candles fechados; a entrada e o proximo candle."""
        if not self.candles:
            return []

        entry_open_time = max(self.candles) + self.interval_ms

        return [pattern for pattern in self.patterns
                if (not self.use_entry_minutes or pattern.matches_time(entry_open_time))
                and self.last_colors(len(pattern.trigger)) == pattern.trigger]

    def pattern_progress(self):
        """Quanto de cada gatilho ja se formou: [(padrao, candles_formados, entrada_prevista)].

        candles_formados e o maior k em que os ultimos k candles fechados repetem os k primeiros do
        gatilho; se ele continuar formando, a entrada sera em entrada_prevista (open_time). Com a regra
        de horario ligada, so conta o k cuja entrada prevista cai num minuto aceito pelo padrao.
        """
        if not self.candles:
            return [(pattern, 0, None) for pattern in self.patterns]

        last     = max(self.candles)
        progress = []

        for pattern in self.patterns:
            size = len(pattern.trigger)

            for formed in range(size, -1, -1):
                entry_open_time = last + (size - formed + 1) * self.interval_ms

                if formed and self.last_colors(formed) != pattern.trigger[:formed]:
                    continue

                if formed and self.use_entry_minutes and not pattern.matches_time(entry_open_time):
                    continue

                progress.append((pattern, formed, entry_open_time if formed else None))
                break

        return progress

    def _find_signals(self, entry_open_time):
        signals = []

        for pattern in self.patterns:
            key = (pattern.name, entry_open_time)

            if key in self.signaled:
                continue

            if self.use_entry_minutes and not pattern.matches_time(entry_open_time):
                continue

            trigger_end = entry_open_time - self.interval_ms
            if self.last_colors(len(pattern.trigger), trigger_end) == pattern.trigger:
                self.signaled.add(key)
                signals.append(Signal(pattern, entry_open_time))

        return signals

    def _resolve_pending(self, candle):
        results   = []
        remaining = []

        for signal in self.pending:
            if signal.entry_open_time == candle['open_time']:
                outcome = signal_outcome(signal, candle)
                results.append((signal, candle, outcome))
                self.resolved.setdefault(candle['open_time'], []).append([signal, outcome])

            elif signal.entry_open_time > candle['open_time']:
                remaining.append(signal)

        self.pending = remaining

        return results

    def _trim(self):
        for open_time in sorted(self.candles)[:-self.max_candles]:
            del self.candles[open_time]

        oldest = min(self.candles, default=0)
        for open_time in [t for t in self.resolved if t < oldest]:
            del self.resolved[open_time]
