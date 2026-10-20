"""Escolhas do monitor da IQ Option salvas em disco (sem depender da interface grafica)."""
import json
import os
from pathlib import Path


# Escolhas da janela (tipo, ativos, tempo, opcoes), restauradas ao abrir. Senha e e-mail nao sao gravados.
SETTINGS_FILE = Path(os.environ.get('IQ_SETTINGS_FILE') or
                     Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') /
                     'monitor_padroes' / 'iqoption.json')


def load_settings():
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

    return settings if isinstance(settings, dict) else {}


def save_settings(settings):
    """Grava num arquivo temporario e troca, para nao deixar um arquivo pela metade."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(SETTINGS_FILE)
