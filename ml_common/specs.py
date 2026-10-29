from dataclasses import asdict, dataclass, replace
from typing import Literal

from ml_common.keys import stable_hash

Modality = Literal["text", "image", "image_text"]

HARRIER_PROMPT = (
    "Instruct: Retrieve semantically similar supermarket grocery product titles "
    "for product matching across retailers. Match on product identity — brand, "
    "type, and variety — and ignore differences in pack size, quantity, or weight\n"
    "Query: "
)

# Fields that change the stored vectors; everything else is operational and not hashed.
_HASHED = (
    "model_id",
    "revision",
    "modality",
    "text_field",
    "image_field",
    "prefix",
    "normalize",
    "dim",
    "dtype",
    "image_tokens",
)


@dataclass(frozen=True)
class EmbeddingSpec:
    """Everything that determines a stored vector. `spec_id` names its table.

    `prefix` is the literal text prepended to text inputs (resolved from the model's own prompt
    table when the spec is created, never a prompt name). `dim` is a Matryoshka truncation (None
    keeps the native size); `image_tokens` is the vision token budget per image (None keeps the
    model default; fewer tokens is much faster but coarser). The other operational fields never affect the id.
    """

    name: str
    model_id: str
    revision: str
    modality: Modality
    text_field: str | None = None
    image_field: str | None = None
    prefix: str = ""
    query_prefix: str = ""
    normalize: bool = True
    dim: int | None = None
    dtype: str = "float32"
    image_tokens: int | None = None
    disabled_configs: tuple[str, ...] = ()
    trust_remote_code: bool = False
    batch_size: int = 32

    def __post_init__(self):
        needs_text = self.modality in ("text", "image_text")
        needs_image = self.modality in ("image", "image_text")
        if needs_text != (self.text_field is not None) or needs_image != (
            self.image_field is not None
        ):
            raise ValueError(f"{self.name}: fields do not match modality {self.modality}")

    @property
    def spec_id(self) -> str:
        fields = asdict(self)
        return stable_hash({k: fields[k] for k in _HASHED})

    @property
    def table_prefix(self) -> str:
        return f"embeddings/{self.spec_id}"

    @property
    def catalogue_columns(self) -> list[str]:
        return [c for c in ("id", "supermarket", self.text_field, self.image_field) if c]


def resolve_revision(model_id: str) -> str:
    from huggingface_hub import HfApi

    return HfApi().model_info(model_id).sha


def make_spec(name: str, model_id: str, **fields) -> EmbeddingSpec:
    """Create a spec pinned to the model's current commit, unless `revision` is given."""
    revision = fields.pop("revision", None) or resolve_revision(model_id)
    return EmbeddingSpec(name=name, model_id=model_id, revision=revision, **fields)


_GEMMA = {
    "model_id": "google/embeddinggemma-2",
    "revision": "914f7f89142e33e77833254d9c9b90c3cef7303b",
    "query_prefix": "task: search result | query: ",
}
_GEMMA_DOC = "title: none | text: "

_GEMMA_IMAGE = (
    EmbeddingSpec(
        name="gemma2-image",
        modality="image",
        image_field="image_sha256",
        dtype="bfloat16",
        disabled_configs=("audio_config",),
        batch_size=4,
        **_GEMMA,
    ),
    EmbeddingSpec(
        name="gemma2-image-text",
        modality="image_text",
        text_field="title_cleaned",
        image_field="image_sha256",
        prefix=_GEMMA_DOC,
        dtype="bfloat16",
        disabled_configs=("audio_config",),
        batch_size=4,
        **_GEMMA,
    ),
)
IMAGE_TOKEN_VARIANTS = (140,)

REGISTRY: dict[str, EmbeddingSpec] = {
    s.name: s
    for s in (
        EmbeddingSpec(
            name="harrier-title",
            model_id="microsoft/harrier-oss-v1-270m",
            revision="31de22b673913c7d658c0f03f792d77c2dcf8ebd",
            modality="text",
            text_field="title_cleaned",
            prefix=HARRIER_PROMPT,
            batch_size=128,
            trust_remote_code=True,
        ),
        EmbeddingSpec(
            name="gemma2-text",
            modality="text",
            text_field="title_cleaned",
            prefix=_GEMMA_DOC,
            dtype="float32",
            disabled_configs=("vision_config", "audio_config"),
            **_GEMMA,
        ),
        *_GEMMA_IMAGE,
        *(
            replace(spec, name=f"{spec.name}-t{tokens}", image_tokens=tokens)
            for tokens in IMAGE_TOKEN_VARIANTS
            for spec in _GEMMA_IMAGE
        ),
    )
}


def get_spec(name: str) -> EmbeddingSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown spec {name!r}; known: {sorted(REGISTRY)}") from None
