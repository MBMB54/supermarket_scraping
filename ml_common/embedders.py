import io
import logging
from dataclasses import dataclass
from typing import Protocol

from ml_common.specs import EmbeddingSpec

logger = logging.getLogger(__name__)

IMAGE_PLACEHOLDER = "<|image|>"


@dataclass(frozen=True)
class EmbedInput:
    text: str | None = None
    image: bytes | None = None


@dataclass(frozen=True)
class EmbedResult:
    vector: list[float] | None
    error: str | None = None


class Embedder(Protocol):
    def encode(self, inputs: list[EmbedInput]) -> list[EmbedResult]:
        """One result per input, in order. A bad item yields an error result, never a skip."""
        ...


def pick_device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _torch_dtype(requested: str, device: str):
    """bfloat16 only where it is native (cuda/mps); CPU falls back to float32."""
    import torch

    if requested == "bfloat16" and device in ("cuda", "mps"):
        return torch.bfloat16
    return torch.float32


class SentenceTransformerEmbedder:
    """Embeds with a sentence-transformers model described by an `EmbeddingSpec`.

    The model loads on first use. Text goes through `prompt=spec.prefix`; image inputs are decoded
    from bytes; image_text inputs use the interleaved dict form with an image placeholder.
    """

    def __init__(self, spec: EmbeddingSpec, device: str | None = None):
        self.spec = spec
        self.device = device
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self.device = self.device or pick_device()
            spec = self.spec
            logger.info("Loading %s@%s on %s", spec.model_id, spec.revision[:8], self.device)
            model = SentenceTransformer(
                spec.model_id,
                revision=spec.revision,
                device=self.device,
                trust_remote_code=spec.trust_remote_code,
                model_kwargs={"dtype": _torch_dtype(spec.dtype, self.device)},
                config_kwargs={name: None for name in spec.disabled_configs} or None,
            )
            if spec.image_tokens:
                model[0].processor.image_processor.max_soft_tokens = spec.image_tokens
            self._model = model
        return self._model

    def _payload(self, item: EmbedInput):
        from PIL import Image

        spec = self.spec
        if spec.modality == "text":
            return item.text
        if item.image is None:
            raise ValueError("image bytes missing")
        image = Image.open(io.BytesIO(item.image))
        image.load()
        image = image.convert("RGB")
        if spec.modality == "image":
            return image
        return {"text": f"{spec.prefix}{item.text} {IMAGE_PLACEHOLDER}", "image": [image]}

    def _encode(self, payloads: list, prompt: str | None, normalize: bool):
        spec = self.spec
        return self.model.encode(
            payloads,
            prompt=prompt,
            batch_size=spec.batch_size,
            normalize_embeddings=normalize,
            truncate_dim=spec.dim,
            convert_to_numpy=True,
            show_progress_bar=False,
        )

    def encode(self, inputs: list[EmbedInput]) -> list[EmbedResult]:
        import numpy as np
        from PIL import Image

        results: list[EmbedResult | None] = [None] * len(inputs)
        payloads, positions = [], []
        for i, item in enumerate(inputs):
            try:
                payloads.append(self._payload(item))
                positions.append(i)
            except (OSError, ValueError, Image.DecompressionBombError) as e:
                results[i] = EmbedResult(None, f"{type(e).__name__}: {e}")
        if payloads:
            prompt = self.spec.prefix if self.spec.modality == "text" else None
            vectors = self._encode(payloads, prompt or None, self.spec.normalize)
            for i, vec in zip(positions, vectors, strict=True):
                if np.isfinite(vec).all():
                    results[i] = EmbedResult(vec.astype("float32").tolist())
                else:
                    results[i] = EmbedResult(None, "non-finite embedding")
        return results

    def encode_queries(self, queries: list[str]):
        return self._encode(queries, self.spec.query_prefix or None, self.spec.normalize)
