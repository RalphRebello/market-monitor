"""Som de alarme do alerta da sequencia: bipes gerados em codigo (sem arquivo de audio nem dependencia).

Toca sem travar a interface: no Windows com winsound, no Linux com paplay, pw-play ou aplay.
Sem nenhum deles, cai no bell do Tk.
"""
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


RATE   = 44_100
VOLUME = 0.7     # 0..1 do volume maximo do arquivo

_wav_path = None


def _siren():
    """Amostras do alarme (~1,2s): bipes agudos alternando 1760 e 1320 Hz."""
    samples = []
    phase   = 0.0

    def tone(seconds, start_hz, end_hz=None):
        nonlocal phase
        end_hz = start_hz if end_hz is None else end_hz
        count  = int(seconds * RATE)

        for i in range(count):
            hz     = start_hz + (end_hz - start_hz) * i / count
            phase += 2 * math.pi * hz / RATE
            # Seno com harmonicos impares: timbre aspero de alarme, que se destaca mais que um seno puro.
            value  = math.sin(phase) + math.sin(3 * phase) / 3 + math.sin(5 * phase) / 5
            fade   = min(1.0, i / 200, (count - i) / 200)    # sem estalos nas pontas
            samples.append(value * fade / 1.53)

    def silence(seconds):
        samples.extend([0.0] * int(seconds * RATE))

    for _ in range(4):
        tone(0.12, 1760)
        silence(0.03)
        tone(0.12, 1320)
        silence(0.03)

    return samples


def _wav():
    """Arquivo WAV da sirene, gerado uma vez por execucao na pasta temporaria."""
    global _wav_path

    if _wav_path is None or not _wav_path.exists():
        path = Path(tempfile.gettempdir()) / 'monitor_iq_sequencia.wav'

        with wave.open(str(path), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(b''.join(struct.pack('<h', int(value * VOLUME * 32767)) for value in _siren()))

        _wav_path = path

    return _wav_path


def play_sequence_alarm(root):
    """Toca a sirene em segundo plano; se nao houver como tocar, usa o bell do Tk."""
    try:
        path = _wav()

        if sys.platform == 'win32':
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            return

        for player in ('paplay', 'pw-play', 'aplay'):
            if shutil.which(player):
                subprocess.Popen([player, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return

    except Exception:
        pass

    for i in range(6):
        root.after(i * 150, root.bell)
