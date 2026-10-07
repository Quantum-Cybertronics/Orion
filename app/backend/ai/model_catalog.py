"""Finds the chat models available in ``models/`` and remembers the user's pick.

Only file *names* ever cross the API boundary. The browser sends a name, and
it is looked up in the list built here, so a request can never point ORION at
an arbitrary path on disk.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("orion.models")

SELECTION_FILENAME = "selected_model.txt"

# "model-00002-of-00005.gguf": llama.cpp loads a split model from its first
# part and finds the rest itself, so only part 1 is a selectable model.
_SHARD = re.compile(r"-(\d+)-of-\d+\.gguf$", re.IGNORECASE)


@dataclass(frozen=True)
class ModelInfo:
    id: str  # the file name, e.g. "qwen2.5-3b-instruct-q4_k_m.gguf"
    path: Path
    size: int  # bytes

    @property
    def name(self) -> str:
        """Friendly label: the file name without ``.gguf``."""
        return self.path.stem

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "size": self.size}


def _is_chat_model(path: Path) -> bool:
    name = path.name.lower()

    # Vision projector files are companions to a model, not models themselves;
    # llama-server refuses to start if it is handed one as the main model.
    if "mmproj" in name:
        return False

    shard = _SHARD.search(name)

    return shard is None or int(shard.group(1)) == 1


def describe_model(path: Path) -> ModelInfo:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    return ModelInfo(id=path.name, path=path, size=size)


def models_dir(base_dir: Path) -> Path:
    return base_dir / "models"


def list_models(base_dir: Path) -> list[ModelInfo]:
    """Every selectable ``.gguf`` in ``models/``, sorted by name."""
    folder = models_dir(base_dir)

    if not folder.is_dir():
        return []

    try:
        files = [
            path
            for path in folder.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".gguf"
            and _is_chat_model(path)
        ]
    except OSError:
        logger.exception("Could not read the models folder")
        return []

    return [describe_model(path) for path in sorted(files, key=lambda p: p.name.lower())]


# -- remembered choice ------------------------------------------------------

def load_selection(data_dir: Path) -> str | None:
    try:
        text = (data_dir / SELECTION_FILENAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None

    return text or None


def save_selection(data_dir: Path, model_id: str) -> None:
    """Remember the choice across restarts. Failure is logged, never raised."""
    try:
        (data_dir / SELECTION_FILENAME).write_text(model_id, encoding="utf-8")
    except OSError:
        logger.warning("Could not save the selected model", exc_info=True)


def choose_initial_model(base_dir: Path, data_dir: Path) -> Path | None:
    """The saved choice if that file still exists, else the first model."""
    models = list_models(base_dir)

    if not models:
        return None

    saved = load_selection(data_dir)

    for model in models:
        if model.id == saved:
            return model.path

    return models[0].path
