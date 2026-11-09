"""Воспроизведение речи: PCM через sounddevice, чтобы речь можно было оборвать или приглушить мгновенно.

Раньше mp3 проигрывался через MCI целиком: прервать его можно было только между фразами, а перебивание голосом
было невозможно. Теперь звук декодируется в память (PyAV, он уже есть у faster-whisper) и играется блоками по ~20 мс:
`stop()` срабатывает в пределах одного блока, `duck()` приглушает, пока решаем, перебивает ли человек."""
from __future__ import annotations

import io
import logging
import threading
import time
import wave
from typing import Any, Optional, Tuple

log = logging.getLogger("jarvis")


def decode_audio(data: bytes) -> Tuple[Any, int]:
    """mp3/wav/ogg в моно float32. Возвращает (сэмплы, частота)."""
    import numpy as np
    if data[:4] == b"RIFF":
        with wave.open(io.BytesIO(data)) as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width != 2:
            raise ValueError("поддерживается только 16-битный WAV")
        pcm = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
        if ch > 1:
            pcm = pcm.reshape(-1, ch).mean(axis=1)
        return pcm, sr
    import av
    with av.open(io.BytesIO(data)) as container:
        stream = container.streams.audio[0]
        rate = int(stream.rate or 24000)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)
        parts = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                parts.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            parts.append(out.to_ndarray().reshape(-1))
    if not parts:
        raise ValueError("пустой звук")
    return np.concatenate(parts).astype("float32"), rate


class Player:
    """Один говорящий поток. Потокобезопасные stop/duck."""

    BLOCK = 480

    def __init__(self) -> None:
        self._stop = threading.Event()
        self.gain = 1.0
        self.playing = False
        self.level = 0.0          # громкость того, что сейчас звучит (для порога перебивания)

    def stop(self) -> None:
        self._stop.set()

    def duck(self, gain: float = 0.25) -> None:
        self.gain = gain

    def unduck(self) -> None:
        self.gain = 1.0

    def play(self, pcm: Any, rate: int, cancel: Optional[threading.Event] = None) -> bool:
        """Играет до конца (True) или до остановки (False)."""
        import numpy as np
        import sounddevice as sd
        self._stop.clear()
        pos = 0
        done = threading.Event()

        def callback(outdata, frames, _time, _status):
            nonlocal pos
            if self._stop.is_set() or (cancel is not None and cancel.is_set()):
                outdata.fill(0)
                raise sd.CallbackStop
            chunk = pcm[pos:pos + frames]
            n = len(chunk)
            outdata[:n, 0] = chunk * self.gain
            outdata[n:, 0] = 0
            self.level = float(np.sqrt(np.mean(np.square(chunk)))) if n else 0.0
            pos += n
            if n < frames:
                raise sd.CallbackStop

        self.playing = True
        try:
            stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32", blocksize=self.BLOCK,
                                     callback=callback, finished_callback=done.set)
            with stream:
                limit = time.monotonic() + len(pcm) / float(rate) + 3.0
                while not done.wait(0.02):
                    if self._stop.is_set() or (cancel is not None and cancel.is_set()):
                        break
                    if time.monotonic() > limit:
                        log.warning("Воспроизведение не закончилось вовремя, обрываю")
                        break
            return not (self._stop.is_set() or (cancel is not None and cancel.is_set()))
        finally:
            self.playing = False
            self.level = 0.0
            self.gain = 1.0
