"""Backtest dos padroes do patterns.json com os candles da propria IQ Option (somente leitura).

Uso (da raiz do projeto):
    .venv/bin/python -m iqoption.backtest                       # ativos salvos no monitor, 7 dias
    .venv/bin/python -m iqoption.backtest --days 14 --entry-minutes
    .venv/bin/python -m iqoption.backtest --assets "EUR/USD (OTC),GBP/USD" --payout 0.85
    .venv/bin/python -m iqoption.backtest --all                 # todos os ativos abertos do tipo

E-mail e senha vem de IQ_EMAIL / IQ_PASSWORD ou sao pedidos no terminal; nada e gravado e nenhuma
operacao e feita.
"""
import argparse
import getpass
import math
import os
import sys
from collections import defaultdict
from time import time

from core.pattern_detector import (INTERVAL_MS, LOSS, PATTERNS_FILE, TIE, WIN, PatternDetector,
                                   load_patterns, ms_to_datetime)
from iqoption.iqoption_client import (ASSET_TYPES, INTERVAL_SECONDS, MAX_CANDLES, IQOptionError, IQSession,
                                      display_name, iq_candle_to_candle)
from iqoption.settings import load_settings


DEFAULT_PAYOUT = 0.85    # quando a IQ Option nao informa o payout do ativo
MIN_SAMPLE     = 30      # abaixo disso o acerto e so ruido


def breakeven(payout):
    """Acerto minimo para nao perder dinheiro: win * payout = loss."""
    return 1 / (1 + payout)


def confidence(win, decided):
    """Intervalo de 95% do acerto (Wilson), mais confiavel que o normal em amostras pequenas."""
    if not decided:
        return 0.0, 0.0

    z      = 1.96
    p      = win / decided
    center = (p + z * z / (2 * decided)) / (1 + z * z / decided)
    half   = z * math.sqrt(p * (1 - p) / decided + z * z / (4 * decided * decided)) / (1 + z * z / decided)

    return center - half, center + half


class Tally:
    """Placar de um recorte (padrao, ativo, hora...): resultados e saldo em unidades apostadas."""

    def __init__(self):
        self.count   = {WIN: 0, LOSS: 0, TIE: 0}
        self.halves  = [[0, 0], [0, 0]]    # [win, decididos] na 1a e na 2a metade do periodo
        self.balance = 0.0

    def add(self, outcome, payout, half):
        self.count[outcome] += 1

        if outcome != TIE:
            won = outcome == WIN
            self.balance            += payout if won else -1
            self.halves[half][0]    += won
            self.halves[half][1]    += 1

    @property
    def decided(self):
        return self.count[WIN] + self.count[LOSS]

    def row(self, label, payout=None):
        n     = self.decided
        total = n + self.count[TIE]
        if not n:
            return f'{label:28} {total:6}  sem resultados'

        rate   = self.count[WIN] / n
        lo, hi = confidence(self.count[WIN], n)
        halves = '  '.join(f'{w / d:5.1%}' if d else '    -' for w, d in self.halves)
        verdict = ''

        if payout is not None:
            need = breakeven(payout)
            if n < MIN_SAMPLE:
                verdict = 'amostra pequena'
            elif lo > need:
                verdict = 'ACIMA do equilibrio'
            elif hi < need:
                verdict = 'abaixo do equilibrio'
            else:
                verdict = 'inconclusivo'

        return (f'{label:28} {total:6} {self.count[WIN]:5} {self.count[LOSS]:5} {self.count[TIE]:4}  '
                f'{rate:6.1%}  [{lo:5.1%}, {hi:5.1%}]  {halves}  {100 * self.balance / total:+7.1f}  {verdict}')


HEADER = (f'{"":28} {"sinais":>6} {"win":>5} {"loss":>5} {"emp":>4}  {"acerto":>6}  {"IC 95%":^16}  '
          f'{"1a met":>5}  {"2a met":>5}  {"saldo/100":>9}')


def fetch_history(session, assets, size, days):
    """Candles fechados dos ultimos `days` dias: {nome: [candles]}, buscados em lotes de MAX_CANDLES."""
    now     = session.server_time()
    start   = now - days * 86400
    ends    = dict.fromkeys(assets, now)
    candles = defaultdict(dict)

    while ends:
        names   = list(ends)
        results = session.get_candles_many([(assets[name], size, MAX_CANDLES, ends[name]) for name in names])

        for name, result in zip(names, results):
            if isinstance(result, IQOptionError) or not result:
                if isinstance(result, IQOptionError):
                    print(f'  {name}: {result}', file=sys.stderr)
                del ends[name]
                continue

            for raw in result:
                candle = iq_candle_to_candle(raw, now)
                if candle['is_closed'] and candle['open_time'] >= start * 1000:
                    candles[name][candle['open_time']] = candle

            first = min(raw['from'] for raw in result)
            if first <= start or first >= ends[name]:
                del ends[name]
            else:
                ends[name] = first - 1

        loaded = sum(len(c) for c in candles.values())
        print(f'\r  {loaded} candles carregados...', end='', file=sys.stderr)

    print(file=sys.stderr)

    return {name: [c[t] for t in sorted(c)] for name, c in candles.items()}


def simulate(candles_by_asset, patterns, interval, use_entry_minutes, payouts):
    """Roda o detector do monitor candle a candle; entrada na abertura do candle 9."""
    tallies = {'pattern': defaultdict(Tally), 'asset': defaultdict(Tally), 'hour': defaultdict(Tally),
               'total': defaultdict(Tally)}

    all_times = [c['open_time'] for candles in candles_by_asset.values() for c in candles]
    if not all_times:
        return tallies

    middle = (min(all_times) + max(all_times)) / 2

    for name, candles in candles_by_asset.items():
        detector = PatternDetector(patterns, interval, use_entry_minutes=use_entry_minutes, max_candles=50)
        payout   = payouts[name]

        for candle in candles:
            _, results = detector.add_closed_candle(candle)

            for signal, entry_candle, outcome in results:
                half = int(entry_candle['open_time'] >= middle)
                hour = ms_to_datetime(entry_candle['open_time']).strftime('%Hh')

                for kind, key in (('pattern', signal.pattern.name), ('asset', name), ('hour', hour),
                                  ('total', 'TOTAL')):
                    tallies[kind][key].add(outcome, payout, half)

    return tallies


def payout_for(payouts, api_name, interval, override):
    if override is not None:
        return override

    options = payouts.get(api_name, {})
    # Turbo cobre expiracoes de 1 a 5 minutos; acima disso vale o payout de binary.
    order   = ('turbo', 'binary', 'blitz') if INTERVAL_SECONDS[interval] <= 300 else ('binary', 'turbo', 'blitz')

    return next((options[o] for o in order if o in options), DEFAULT_PAYOUT)


def parse_args():
    parser = argparse.ArgumentParser(description='Backtest dos padroes com candles da IQ Option.')
    parser.add_argument('--type', default=None, help='tipo de ativo (padrao: o salvo no monitor, ou binary): '
                                                     + ', '.join(ASSET_TYPES.values()))
    parser.add_argument('--assets', help='ativos separados por virgula, como aparecem no monitor')
    parser.add_argument('--all', action='store_true', help='todos os ativos abertos do tipo')
    parser.add_argument('--days', type=float, default=7, help='dias de historico (padrao 7)')
    parser.add_argument('--interval', default=None, help='tempo dos candles (padrao: o do patterns.json)')
    parser.add_argument('--entry-minutes', action='store_true', help='aplica a regra de horario (ex.: 2/7)')
    parser.add_argument('--payout', type=float, help='payout fixo (0.85 = 85%%) em vez do informado pela IQ')

    return parser.parse_args()


def main():
    args                  = parse_args()
    settings              = load_settings()
    patterns_interval, patterns = load_patterns(PATTERNS_FILE)
    interval              = args.interval or patterns_interval

    if interval not in INTERVAL_SECONDS or interval not in INTERVAL_MS:
        sys.exit(f'Tempo nao suportado: {interval}')

    asset_type = args.type or ASSET_TYPES.get(settings.get('asset_type'), 'binary')
    if asset_type not in ASSET_TYPES.values():
        sys.exit(f'Tipo desconhecido: {asset_type}')

    use_minutes = args.entry_minutes and interval == patterns_interval
    if args.entry_minutes and not use_minutes:
        print(f'Regra de horario ignorada: so vale em {patterns_interval}', file=sys.stderr)

    email    = os.environ.get('IQ_EMAIL') or input('E-mail da IQ Option: ')
    password = os.environ.get('IQ_PASSWORD') or getpass.getpass('Senha: ')

    session = IQSession()
    try:
        print('Conectando...', file=sys.stderr)
        session.connect(email, password)

        api_names = {display_name(name): (name, active_id)
                     for name, (active_id, is_open) in session.get_assets(asset_type).items()
                     if is_open or not args.all}
        payouts   = session.get_payouts(asset_type)

        if args.all:
            chosen = list(api_names)
        elif args.assets:
            chosen = [name.strip() for name in args.assets.split(',') if name.strip()]
        else:
            chosen = settings.get('assets', [])[:settings.get('count', 1)] or ['EUR/USD']

        unknown = [name for name in chosen if name not in api_names]
        if unknown:
            print(f'Ignorados (nao existem em {asset_type}): {", ".join(unknown)}', file=sys.stderr)

        chosen = [name for name in chosen if name in api_names]
        if not chosen:
            sys.exit('Nenhum ativo valido para testar.')

        print(f'Baixando {args.days:g} dias de {interval} de {len(chosen)} ativo(s)...', file=sys.stderr)
        started = time()
        candles = fetch_history(session, {name: api_names[name][1] for name in chosen},
                                INTERVAL_SECONDS[interval], args.days)
        print(f'  em {time() - started:.0f}s', file=sys.stderr)

    except IQOptionError as error:
        sys.exit(f'Erro na IQ Option: {error}')

    finally:
        session.close()

    asset_payouts = {name: payout_for(payouts, api_names[name][0], interval, args.payout) for name in candles}
    tallies       = simulate(candles, patterns, interval, use_minutes, asset_payouts)
    mean_payout   = sum(asset_payouts.values()) / len(asset_payouts) if asset_payouts else DEFAULT_PAYOUT
    total_candles = sum(len(c) for c in candles.values())

    print(f'\nBacktest: {len(candles)} ativo(s), {total_candles} candles de {interval}, {args.days:g} dias, '
          f'regra de horario {"ligada" if use_minutes else "desligada"}')
    print(f'Entrada na abertura do candle 9; empate = valor devolvido. Payout medio {mean_payout:.0%} '
          f'-> acerto minimo {breakeven(mean_payout):.1%}')
    print('saldo/100 = lucro em unidades apostadas a cada 100 sinais (1 unidade por entrada)\n')

    sections = (('Total', 'total'), ('Por padrao', 'pattern'), ('Por ativo', 'asset'),
                ('Por hora da entrada (horario de Brasilia)', 'hour'))

    for title, kind in sections:
        rows = tallies[kind]
        if not rows:
            continue

        print(f'== {title}')
        print(HEADER)

        keys = sorted(rows, key=lambda key: -rows[key].decided) if kind == 'asset' else sorted(rows)
        for key in keys:
            payout = asset_payouts[key] if kind == 'asset' else mean_payout
            label  = f'{key} ({payout:.0%})' if kind == 'asset' else key
            print(rows[key].row(label, payout))

        print()

    print('Como ler: so ha vantagem quando o IC 95% inteiro fica acima do acerto minimo (ACIMA do '
          'equilibrio)\ne o acerto se repete nas duas metades do periodo. Com muitos recortes (ativos x '
          'horas), alguns\nficam acima por acaso: confirme rodando de novo com outro periodo antes de confiar.')


if __name__ == '__main__':
    main()
