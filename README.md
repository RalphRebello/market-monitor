# Monitor de padrões para opções binárias

Monitores com interface gráfica que procuram, em tempo real, as sequências de candles definidas em
`patterns/patterns.json` e geram alertas de **COMPRA (CALL)** ou **VENDA (PUT)**.

| Monitor | Pasta | Como rodar (da raiz do projeto) | Documentação |
|---|---|---|---|
| Binance | `binance/` | `.venv/bin/python -m binance.monitor_patterns` | [binance/README.md](binance/README.md) |
| IQ Option | `iqoption/` | `.venv/bin/python -m iqoption.monitor_iqoption` | [iqoption/README.md](iqoption/README.md) |

## Recursos

Nos dois monitores:

- detecção dos padrões de `patterns/patterns.json` a cada candle fechado, com alerta (janela + som),
  placar de sinais/WIN/LOSS/empate com o acerto, e histórico;
- faixa dos últimos candles, numerada como o gatilho (1..8) e a entrada (9);
- regra opcional de horário de entrada (minutos final 2/7);
- padrões que se formaram antes de a procura começar aparecem no histórico, sem alerta.

Só no monitor da IQ Option ([detalhes](iqoption/README.md)):

- **até 20 ativos ao mesmo tempo**, escolhidos no campo **Ativos**, cada um num cartão com a faixa de
  candles, o placar do ativo e o **termômetro dos padrões** (quanto de cada gatilho já se formou, quantos
  candles faltam e o horário previsto da entrada);
- janela responsiva: os cartões se reorganizam em colunas conforme a largura, com rolagem quando não cabem;
- candles de todos os ativos buscados em lote, numa única conexão, logo após cada fechamento;
- trocar um ativo ou a quantidade com a procura ligada não reinicia os demais ativos;
- alerta também para padrão já formado cuja entrada ainda está aberta ao iniciar a procura
  (`entrada ... (em andamento, fecha em Ns)`);
- reconexão automática, inclusive quando a conexão para de responder sem fechar;
- escolhas (tipo, ativos, tempo, opções, tamanho da janela) salvas em `~/.config/monitor_padroes/` e
  procura retomada após o login; e-mail e senha nunca são gravados;
- a roda do mouse não troca ativo, tipo, tempo ou quantidade por acidente.

## Estrutura

```
binance_monitor/
├── binance/            # monitor da Binance (GUI, WebSocket, cliente REST)
├── iqoption/           # variação IQ Option (login, cliente, requirements próprios)
├── core/               # código compartilhado: detecção dos padrões
├── patterns/           # padrões compartilhados: patterns.json + imagens de referência
└── requiriments.txt    # dependências do monitor da Binance
```

Os comandos usam `-m` porque os monitores importam o código compartilhado de `core/` (e o da IQ Option
reaproveita a janela de `binance/`); rodar o arquivo direto (`python binance/monitor_patterns.py`) falha
nesses imports.

## Instalação

```bash
sudo apt install python3-tk                       # Linux, se o Tkinter não estiver instalado
python3 -m venv .venv
.venv/bin/pip install -r requiriments.txt         # Binance
.venv/bin/pip install -r iqoption/requirements.txt  # IQ Option (inclui as dependências da Binance)
```
