"""ASR backends: a minimal Protocol plus three swappable implementations.

Every backend takes 16kHz mono float32 audio in [-1, 1] and returns plain text.
No backend does its own resampling -- that happens once in audio.py before the
chunk reaches here, so swapping backends never changes the audio pipeline.

`mlx` is the default on Apple Silicon and `whisper` (faster-whisper) elsewhere.
The two are the same model, large-v3-turbo, on different runtimes: this machine
has no CUDA, so the CTranslate2 path can only run on CPU here, while MLX runs on
the M-series GPU through Metal.
"""

from __future__ import annotations

import os
import sys
from typing import Protocol

import numpy as np

# Defensive, not load-bearing: NeMo's [asr] extra pulls in wandb + NVIDIA's
# nv_one_logger as transitive deps. We never construct a Trainer or a wandb
# logger, so neither is ever invoked -- this just makes sure nothing they do
# at import time tries to phone home.
os.environ.setdefault("WANDB_MODE", "disabled")
os.environ.setdefault("WANDB_DISABLED", "true")


class Backend(Protocol):
    def load(self) -> None: ...
    def warmup(self) -> None: ...
    def transcribe(self, audio: np.ndarray) -> str: ...


class ParakeetBackend:
    """NVIDIA Parakeet TDT 0.6B v3 via NeMo. fp16, single resident model on `device`.

    Known broken on this machine's Python 3.14 + `nemo_toolkit[asr]` combination: importing
    `nemo.collections.asr` crashes at import time via a chain of transitive-dependency
    version conflicts (protobuf gencode/runtime mismatch from a source-built `onnx`, then
    `ml_dtypes` missing attributes `onnx` expects, then `numba` rejecting the numpy version
    the fix pulled in). Each targeted fix surfaced a new incompatibility deeper in the same
    chain -- not a single pin to bump. See README for the exact errors. Kept here because the
    interface is still correct and this may well work in a fresh Python 3.11/3.12 venv where
    the ML ecosystem's wheels are more mature; not attempted here per the "don't fight it more
    than a couple of tries" instruction this project was built under.
    """

    MODEL_NAME = "nvidia/parakeet-tdt-0.6b-v3"

    def __init__(self, device: str = "cuda:0") -> None:
        self.device = device
        self._model = None

    def load(self) -> None:
        import nemo.collections.asr as nemo_asr

        model = nemo_asr.models.ASRModel.from_pretrained(model_name=self.MODEL_NAME)
        model = model.to(self.device)
        model.eval()
        self._model = model.half()

    def warmup(self) -> None:
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        import torch

        with torch.inference_mode():
            hyps = self._model.transcribe([audio], verbose=False)
        result = hyps[0]
        return result.text if hasattr(result, "text") else str(result)


class WhisperBackend:
    """faster-whisper large-v3-turbo via CTranslate2, on CUDA or CPU.

    On CUDA it runs fp16 -- int8 is disabled upstream for Blackwell/sm_120, which crashes with
    CUBLAS_STATUS_NOT_SUPPORTED. On CPU it runs int8, the only quantization worth having there.

    The CPU path exists so this backend stays usable on a machine without CUDA (it's the
    fallback if MLX won't install), not because it's fast: expect seconds per utterance on
    large-v3-turbo rather than the fractions of a second MLX gets out of the same model.
    """

    MODEL_NAME = "large-v3-turbo"

    def __init__(self, device: str = "cuda:0", model: str | None = None) -> None:
        self.device = device
        self.model_name = model or self.MODEL_NAME
        self._model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        if self.device.startswith("cpu"):
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            return

        device_index = int(self.device.split(":")[1]) if ":" in self.device else 0
        self._model = WhisperModel(
            self.model_name,
            device="cuda",
            device_index=device_index,
            compute_type="float16",
        )

    def warmup(self) -> None:
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _info = self._model.transcribe(audio, language="en", vad_filter=False)
        return " ".join(seg.text.strip() for seg in segments).strip()


class MLXWhisperBackend:
    """whisper large-v3-turbo via Apple's MLX, on the M-series GPU. The default on macOS.

    MLX takes no device argument: Apple Silicon has unified memory and MLX places work on the
    GPU itself, so there's no cuda:0/cuda:1 equivalent to select and `--device` is ignored here.

    `mlx_whisper.transcribe` keeps the loaded model in a module-level holder keyed by repo, so
    `load()` priming it means the first real utterance doesn't pay the load cost -- the same
    load-then-warm-up contract the other backends follow.
    """

    MODEL_REPO = "mlx-community/whisper-large-v3-turbo"

    def __init__(self, device: str = "mlx", model: str | None = None) -> None:
        self.device = device
        self.model_repo = model or self.MODEL_REPO
        self._transcribe = None

    def load(self) -> None:
        import mlx.core as mx
        import mlx_whisper
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.get_model(self.model_repo, mx.float16)
        self._transcribe = mlx_whisper.transcribe

    def warmup(self) -> None:
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> str:
        result = self._transcribe(
            audio,
            path_or_hf_repo=self.model_repo,
            language="en",
            # Each utterance is an independent segment closed by silence, so conditioning on
            # the previous one only invites the model to run away with a hallucinated context.
            condition_on_previous_text=False,
        )
        return result["text"].strip()


def default_backend() -> str:
    """MLX on Apple Silicon, faster-whisper anywhere else."""
    return "mlx" if sys.platform == "darwin" else "whisper"


def default_device(backend: str) -> str:
    if backend == "mlx":
        return "mlx"
    return "cpu" if sys.platform == "darwin" else "cuda:0"


def build_backend(name: str, device: str, model: str | None = None) -> Backend:
    if name == "parakeet":
        return ParakeetBackend(device)
    if name == "whisper":
        return WhisperBackend(device, model)
    if name == "mlx":
        return MLXWhisperBackend(device, model)
    raise ValueError(f"unknown backend: {name!r}")
