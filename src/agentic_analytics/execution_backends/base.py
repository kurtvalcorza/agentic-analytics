from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ExecutionResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    runtime: dict[str, str] = field(default_factory=dict)
    error: dict[str, str] | None = None


class ExecutionBackend(Protocol):
    def execute(self, code: str, workspace: str, timeout: int) -> ExecutionResult: ...

