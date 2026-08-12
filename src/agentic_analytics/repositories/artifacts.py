from pathlib import Path

from agentic_analytics.models import Artifact

from .base import JsonRecordRepository


class ArtifactRepository(JsonRecordRepository[Artifact]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "artifacts", Artifact)
