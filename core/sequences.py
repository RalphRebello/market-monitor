"""Sequencias de padroes: um padrao 'first' arma a sequencia no ativo e o padrao 'then' a completa.

So observa os sinais que o PatternDetector ja produz; nao muda a deteccao dos padroes.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from core.pattern_detector import PatternError


ARMED     = 'armed'
COMPLETED = 'completed'


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
    - 'then' formado com a sequencia armada: completa e desarma, voltando a esperar 'first'.
      So conta se o primeiro candle do gatilho de 'then' vier depois dos candles de 'first'
      (o candle de alerta de 'first' pode ser o primeiro de 'then').
    A espera nao expira.
    """

    def __init__(self, sequences):
        self.sequences = sequences
        self.armed     = {}    # (sequencia, ativo) -> open_time do candle de alerta de 'first'

    def reset(self, sequence=None, asset=None):
        """Sem argumentos zera tudo; com sequence e/ou asset, so o que combinar."""
        self.armed = {key: value for key, value in self.armed.items()
                      if not ((sequence is None or key[0] == sequence) and (asset is None or key[1] == asset))}

    def armed_at(self, sequence, asset):
        return self.armed.get((sequence, asset))

    def observe(self, asset, signal, interval_ms, sequences=None):
        """Recebe um sinal do ativo; retorna [(sequencia, ARMED ou COMPLETED, open_time de 'first')].

        sequences limita as sequencias acompanhadas (padrao: todas).
        """
        events = []
        name   = signal.pattern.name

        for sequence in self.sequences if sequences is None else sequences:
            key = (sequence, asset)

            if name == sequence.then and key in self.armed:
                first_candle = signal.entry_open_time - len(signal.pattern.trigger) * interval_ms

                if first_candle >= self.armed[key]:
                    events.append((sequence, COMPLETED, self.armed.pop(key)))

            if name == sequence.first:
                self.armed[key] = signal.entry_open_time
                events.append((sequence, ARMED, signal.entry_open_time))

        return events
