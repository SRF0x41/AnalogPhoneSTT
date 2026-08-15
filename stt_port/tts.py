"""Text-to-speech: the return half of the line.

Deliberately the same shape as `backends.py` -- a Protocol, an implementation, and a factory
-- because that file already solves "swappable model behind a stable interface" and a second
pattern for the same problem would be worse than reusing this one. `load()` then `warmup()`
then many `synthesize()` calls, exactly like the ASR side.

Output is float32 mono at the model's own rate (`SAMPLE_RATE`); converting to the 8kHz PCM16
the analog line carries is `audio.resample_to_target` and `audio.float32_to_pcm16`, not this
module's job -- same division of labour as the ASR backends, which never resample either.

## Why not macOS `say`

It is built in, needs no model, no dependency and no memory, which on an 8GB machine already
holding whisper-large-v3-turbo is a real temptation. It is still ruled out: the point of this
work is choosing and controlling the voice model. If a model will not fit, the answer is a
smaller model, not the system voice.

## The espeak caveat

Kokoro phonemizes English through `misaki`, which falls back to espeak for words outside its
dictionary. Without espeak-ng installed system-wide, misaki logs `EspeakFallback not Enabled:
OOD words will be skipped` -- and *skipped* means silently absent from the audio, not
mispronounced. Common words are unaffected; unusual proper nouns may vanish. Install it with
`brew install espeak-ng` if that matters.
"""

from __future__ import annotations

import sys
from typing import Protocol

import numpy as np


class TtsBackend(Protocol):
    SAMPLE_RATE: int

    def load(self) -> None: ...
    def warmup(self) -> None: ...
    def synthesize(self, text: str) -> np.ndarray: ...


class KokoroBackend:
    """Kokoro-82M via MLX, on the Apple GPU.

    Chosen for size before quality: 82M parameters, ~165MB at 4-bit, which is what makes it
    viable next to Whisper's ~1.6GB on an 8GB machine. Measured ~7x realtime on this hardware
    (720ms to synthesize 5.5s of speech), so it is comfortably off the critical path.

    The model is loaded here rather than through `mlx_audio.tts.generate.generate_audio`,
    which writes wav files and returns None -- this needs the samples in memory to resample
    them onto the wire.
    """

    MODEL_REPO = "prince-canuma/Kokoro-82M-4bit"
    DEFAULT_VOICE = "af_heart"
    SAMPLE_RATE = 24000

    def __init__(self, model: str | None = None, voice: str | None = None) -> None:
        self.model_repo = model or self.MODEL_REPO
        self.voice = voice or self.DEFAULT_VOICE
        self._model = None

    def load(self) -> None:
        # Imported here, not at module scope, so `--tts none` costs nothing and the test
        # suite still runs on a machine without mlx-audio -- the same contract backends.py
        # keeps for mlx_whisper.
        from mlx_audio.tts.utils import load_model

        self._model = load_model(self.model_repo)

    def warmup(self) -> None:
        """Pay the pipeline's one-off costs now: voice download, G2P init, first graph."""
        self.synthesize("Ready.")

    def synthesize(self, text: str) -> np.ndarray:
        """Text -> mono float32 at `SAMPLE_RATE`. Empty text yields empty audio, not an error.

        Kokoro splits long text into segments and yields them; they are concatenated here so
        callers get one utterance's worth of audio and never have to know about the split.
        """
        if not text.strip():
            return np.zeros(0, dtype=np.float32)
        segments = list(self._model.generate(text=text, voice=self.voice))
        if not segments:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([np.asarray(s.audio, dtype=np.float32) for s in segments])


def build_tts(name: str, model: str | None = None, voice: str | None = None) -> TtsBackend:
    if name == "kokoro":
        return KokoroBackend(model, voice)
    raise ValueError(f"unknown tts backend: {name!r}")


def main(argv: list[str] | None = None) -> None:
    """Synthesize one phrase to a wav, at the line's rate. The offline smoke test.

        python -m stt_port.tts --say "testing one two three" --out /tmp/t.wav
    """
    import argparse
    import time
    import wave

    from . import audio

    p = argparse.ArgumentParser(description="Synthesize one phrase to an 8kHz PCM16 wav.")
    p.add_argument("--say", required=True)
    p.add_argument("--out", default="/tmp/tts.wav")
    p.add_argument("--backend", default="kokoro")
    p.add_argument("--model", default=None)
    p.add_argument("--voice", default=None)
    p.add_argument("--rate", type=int, default=8000, help="output rate (default: the wire's)")
    args = p.parse_args(argv)

    backend = build_tts(args.backend, args.model, args.voice)
    t0 = time.perf_counter()
    backend.load()
    t_load = time.perf_counter()
    samples = backend.synthesize(args.say)
    t_gen = time.perf_counter()

    wire = audio.resample_to_target(samples, backend.SAMPLE_RATE, args.rate)
    with wave.open(args.out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(args.rate)
        f.writeframes(audio.float32_to_pcm16(wire))

    seconds = len(wire) / args.rate
    print(
        f"load={(t_load - t0) * 1000:.0f}ms synth={(t_gen - t_load) * 1000:.0f}ms "
        f"audio={seconds:.2f}s @{args.rate}Hz -> {args.out}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
