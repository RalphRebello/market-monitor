# Monitor de padrões para opções binárias: IQ Option

Monitor com interface gráfica que procura, em tempo real, as sequências de candles definidas em
`patterns/patterns.json` em **todos os ativos abertos** de um tipo da **IQ Option** e avisa, com um popup e no
histórico, em qual ativo e qual padrão se formou e o horário da entrada. A documentação completa está em [iqoption/README.md](iqoption/README.md).

```bash
.venv/bin/python -m iqoption.monitor_iqoption     # da raiz do projeto
```

## Recursos

- detecção dos padrões de `patterns/patterns.json` a cada candle fechado, com alerta (janela + som)
  mostrando o ativo, o padrão e a entrada, e o mesmo texto no histórico;
- procura em **todos os ativos abertos do tipo** escolhido (binárias, digitais, forex...), com alerta em
  qualquer um deles, esteja ou não na tela;
- cartões na tela adicionados com o botão **+** e fechados com o **✕**, sem limite, cada um com a faixa de
  candles (gatilho 1..8 e entrada 9) e o **termômetro dos padrões** (quanto de cada
  gatilho já se formou, quantos candles faltam e o horário previsto da entrada);
- regra opcional de horário de entrada (minutos final 2/7);
- janela responsiva: os cartões se reorganizam em colunas conforme a largura, com rolagem quando não cabem;
- candles de todos os ativos buscados em lote, numa única conexão, logo após cada fechamento (medido:
  alerta ~1,5 s após o fechamento com 144 ativos de binárias e ~3 s com os 729 de CFD);
- adicionar, fechar ou trocar cartões com a procura ligada não reinicia a procura;
- alerta também para padrão já formado cuja entrada ainda está aberta ao iniciar a procura
  (`entrada ... (atrasado Ns, fecha em Ns)`); os que já passaram são ignorados;
- reconexão automática, inclusive quando a conexão para de responder sem fechar;
- escolhas (tipo, ativos, tempo, opções, tamanho da janela) salvas em `~/.config/monitor_padroes/` e
  procura retomada após o login; e-mail e senha nunca são gravados;
- backtest dos padrões com os candles da IQ Option (`python -m iqoption.backtest`);
- executável único para Windows, gerado a partir do Linux (`windows/build.sh`).

## Estrutura

```
market-monitor/
├── iqoption/           # monitor (GUI, cliente da IQ Option, backtest, requirements)
├── core/               # detecção dos padrões (sem interface)
├── patterns/           # patterns.json + imagens de referência
└── windows/            # build do executável para Windows
```

Os comandos usam `-m` porque o monitor importa o código de `core/`; rodar o arquivo direto
(`python iqoption/monitor_iqoption.py`) falha nesses imports.

## Instalação

```bash
sudo apt install python3-tk                       # Linux, se o Tkinter não estiver instalado
python3 -m venv .venv
.venv/bin/pip install -r iqoption/requirements.txt
```
