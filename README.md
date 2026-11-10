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
- cartões na tela adicionados com o botão **+** e fechados com o **✕**, sem limite, cada um com uma faixa de
  candles por tamanho de padrão (gatilho e entrada numerados) e o **termômetro dos padrões** (quanto de cada
  gatilho já se formou, quantos candles faltam e o horário previsto da entrada);
- clique no popup para abrir o card do ativo que alertou;
- caixa para ligar e desligar cada padrão no painel **Padrões carregados**, sem reiniciar a procura;
- **sequência Padrão 5 → Padrão 1**: depois de um Padrão 5, o Padrão 1 no mesmo ativo gera um popup
  **amarelo** e a sequência zera; o cartão mostra `Aguardando Padrao 5` / `Aguardando Padrao 1`;
- filtro de minutos 2/7 nos Padrões 1 a 4, que liga e desliga no painel (só em `1m`); o Padrão 6 tem regra
  própria e fixa (candle 1 em minuto final 1), e o Padrão 5 vale em qualquer horário;
- janela responsiva: os cartões se reorganizam em colunas conforme a largura, com rolagem quando não cabem;
- candles de todos os ativos buscados em lote, numa única conexão, logo após cada fechamento (medido:
  alerta ~1,5 s após o fechamento com 144 ativos de binárias e ~3 s com os 729 de CFD);
- adicionar, fechar ou trocar cartões com a procura ligada não reinicia a procura;
- alerta também para padrão já formado cuja entrada ainda está aberta ao iniciar a procura
  (`entrada ... (atrasado Ns, fecha em Ns)`); os que já passaram são ignorados;
- reconexão automática, inclusive quando a conexão para de responder sem fechar;
- escolhas (tipo, ativos, tempo, opções, padrões e sequências ligados, tamanho da janela) salvas em `~/.config/monitor_padroes/` e
  procura retomada após o login; e-mail e senha nunca são gravados;
- backtest dos padrões com os candles da IQ Option (`python -m iqoption.backtest`);
- executável único para Windows, gerado a partir do Linux (`windows/build.sh`).

## Estrutura

```
market-monitor/
├── iqoption/           # monitor (GUI, cliente da IQ Option, backtest, requirements)
├── core/               # detecção dos padrões e sequências (sem interface)
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
