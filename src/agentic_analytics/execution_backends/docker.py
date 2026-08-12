import subprocess

from .base import ExecutionResult


class DockerBackend:
    conformant = True

    def __init__(self, image: str = "agentic-analytics-exec:dev") -> None:
        self.image = image

    def execute(self, code: str, workspace: str, timeout: int) -> ExecutionResult:
        command = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--memory=1g", "--cpus=1", "--pids-limit=128",
                   "--env=HOME=/tmp", "--tmpfs=/tmp:rw,noexec,nosuid,size=128m",
                   "--mount", f"type=bind,src={workspace},dst=/workspace", "--workdir=/workspace",
                   self.image, "python", "-I", "-c", code]
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult("timed_out", exc.stdout or "", exc.stderr or "", error={"type": "timeout"})
        return ExecutionResult("succeeded" if result.returncode == 0 else "failed", result.stdout, result.stderr,
                               {"provider": "docker", "image": self.image},
                               None if result.returncode == 0 else {"type": "nonzero_exit"})

