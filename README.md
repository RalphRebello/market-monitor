# Monitor de padrões para opções binárias

Monitores com interface gráfica que procuram, em tempo real, as sequências de candles definidas em
`patterns/patterns.json` e geram alertas de **COMPRA (CALL)** ou **VENDA (PUT)**.

| Monitor | Pasta | Como rodar (da raiz do projeto) | Documentação |
|---|---|---|---|
| Binance | `binance/` | `.venv/bin/python -m binance.monitor_patterns` | [binance/README.md](binance/README.md) |
| IQ Option | `iqoption/` | `.venv/bin/python -m iqoption.monitor_iqoption` | [iqoption/README.md](iqoption/README.md) |

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
