import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


tz = ZoneInfo('America/Sao_Paulo')

GREEN = 'G'
RED   = 'R'
DOJI  = 'D'

DIRECTIONS = {
    GREEN : 'COMPRA (CALL)',
    RED   : 'VENDA (PUT)',
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

    @property
    def direction(self):
        return self.pattern.direction

    @property
    def label(self):
        return DIRECTIONS[self.direction]


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
        self.candles     = {}   # open_time -> candle fechado
        self.pending     = []   # sinais aguardando o fechamento do candle de entrada

    def reset(self):
        self.candles.clear()
        self.pending.clear()

    def load_history(self, candles):
        """Carrega candles fechados sem disparar sinais (sinais atrasados nao servem)."""
        for candle in candles:
            if candle['is_closed']:
                self.candles[candle['open_time']] = candle

        self._trim()

    def add_closed_candle(self, candle):
        """Retorna (novos_sinais, resultados) onde resultados = [(sinal, cor, ganhou)]."""
        if candle['open_time'] in self.candles:
            return [], []

        self.candles[candle['open_time']] = candle
        self._trim()

        results = self._resolve_pending(candle)
        signals = self._find_signals(candle['open_time'] + self.interval_ms)

        self.pending.extend(signals)

        return signals, results

    def last_colors(self, count):
        """Cores dos ultimos candles fechados consecutivos (mais antigo primeiro)."""
        if not self.candles:
            return ''

        open_time = max(self.candles)
        colors    = []

        while len(colors) < count and open_time in self.candles:
            colors.append(candle_color(self.candles[open_time]))
            open_time -= self.interval_ms

        return ''.join(reversed(colors))

    def _find_signals(self, entry_open_time):
        signals = []

        for pattern in self.patterns:
            if self.use_entry_minutes and not pattern.matches_time(entry_open_time):
                continue

            if self.last_colors(len(pattern.trigger)) == pattern.trigger:
                signals.append(Signal(pattern, entry_open_time))

        return signals

    def _resolve_pending(self, candle):
        results   = []
        remaining = []

        for signal in self.pending:
            if signal.entry_open_time == candle['open_time']:
                color = candle_color(candle)
                results.append((signal, color, color == signal.direction))

            elif signal.entry_open_time > candle['open_time']:
                remaining.append(signal)

        self.pending = remaining

        return results

    def _trim(self):
        for open_time in sorted(self.candles)[:-self.max_candles]:
            del self.candles[open_time]
