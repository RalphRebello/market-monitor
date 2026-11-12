"""Sequencias de padroes: um padrao 'first' arma a sequencia no ativo e o padrao 'then' a completa,
desde que o candle de entrada de 'then' feche verde.

So observa os sinais que o PatternDetector ja produz; nao muda a deteccao dos padroes.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from core.pattern_detector import GREEN, PatternError, candle_color


ARMED      = 'armed'        # 'first' formado: sequencia armada
CONFIRMING = 'confirming'   # 'then' formado: aguardando o candle de entrada dele fechar verde
COMPLETED  = 'completed'    # candle de entrada de 'then' fechou verde: alerta amarelo
FAILED     = 'failed'       # candle de entrada de 'then' nao fechou verde: segue armada


@dataclass(frozen=True)
class Sequence:
    name  : str
    first : str    # nome do padrao que arma a sequencia
    then  : str    # nome do padrao que completa a sequencia


def load_sequences(path, patterns):
    """Sequencias de patterns.json ('sequences'); os dois padroes precisam existir em 'patterns'."""
    data  = json.loads(Path(path).read_text(encoding='utf-8'))
    names = {pattern.name for pattern in patterns}
    found = []

    for item in data.get('sequences', []):
        sequence = Sequence(name=item['name'], first=item['first'], then=item['then'])

        for name in (sequence.first, sequence.then):
            if name not in names:
                raise PatternError(f"Padrao '{name}' da sequencia '{sequence.name}' nao existe")

        if sequence.first == sequence.then:
            raise PatternError(f"Sequencia '{sequence.name}' usa o mesmo padrao duas vezes")

        found.append(sequence)

    return found


class SequenceTracker:
    """Estado de cada sequencia em cada ativo.

    - 'first' formado: arma (ou rearma) a sequencia no ativo, a partir do candle de alerta dele.
    - 'then' formado com a sequencia armada: passa a aguardar o candle de entrada de 'then'.
      So conta se o primeiro candle do gatilho de 'then' vier depois dos candles de 'first'
      (o candle de alerta de 'first' pode ser o primeiro de 'then').
    - Candle de entrada de 'then' fechou verde: completa e desarma, voltando a esperar 'first'.
      Fechou vermelho ou doji: nao completa e a sequencia segue armada, esperando outro 'then'.
    A espera nao expira.
    """

    def __init__(self, sequences):
        self.sequences  = sequences
        self.armed      = {}    # (sequencia, ativo) -> open_time do candle de alerta de 'first'
        self.confirming = {}    # (sequencia, ativo) -> open_time do candle de entrada de 'then'

    def reset(self, sequence=None, asset=None):
        """Sem argumentos zera tudo; com sequence e/ou asset, so o que combinar."""
        def keep(key):
            return not ((sequence is None or key[0] == sequence) and (asset is None or key[1] == asset))

        self.armed      = {key: value for key, value in self.armed.items() if keep(key)}
        self.confirming = {key: value for key, value in self.confirming.items() if keep(key)}

    def armed_at(self, sequence, asset):
        return self.armed.get((sequence, asset))

    def confirming_at(self, sequence, asset):
        """open_time do candle de entrada de 'then' que ainda precisa fechar verde, ou None."""
        return self.confirming.get((sequence, asset))

    def observe(self, asset, signal, interval_ms, sequences=None):
        """Recebe um sinal do ativo; retorna [(sequencia, ARMED ou CONFIRMING, open_time)].

        ARMED traz o candle de alerta de 'first'; CONFIRMING, o candle de entrada de 'then'.
        sequences limita as sequencias acompanhadas (padrao: todas).
        """
        events = []
        name   = signal.pattern.name

        for sequence in self.sequences if sequences is None else sequences:
            key = (sequence, asset)

            if name == sequence.then and key in self.armed:
                first_candle = signal.entry_open_time - len(signal.pattern.trigger) * interval_ms

                if first_candle >= self.armed[key]:
                    self.confirming[key] = signal.entry_open_time
                    events.append((sequence, CONFIRMING, signal.entry_open_time))

            if name == sequence.first:
                # Rearmar recomeca a espera: um 'then' anterior, ainda sem confirmar, deixa de valer.
                self.armed[key] = signal.entry_open_time
                self.confirming.pop(key, None)
                events.append((sequence, ARMED, signal.entry_open_time))

        return events

    def confirm(self, asset, candle, sequences=None):
        """Recebe um candle fechado do ativo; retorna [(sequencia, COMPLETED ou FAILED, open_time do candle)].

        So responde quando o candle e o de entrada de 'then' que estava aguardando.
        """
        events = []

        for sequence in self.sequences if sequences is None else sequences:
            key = (sequence, asset)

            if self.confirming.get(key) != candle['open_time']:
                continue

            del self.confirming[key]

            if candle_color(candle) == GREEN:
                del self.armed[key]
                events.append((sequence, COMPLETED, candle['open_time']))
            else:
                events.append((sequence, FAILED, candle['open_time']))

        return events
