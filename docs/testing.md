# Running the tests

The suite tests two OS-level boundaries: the confined DuckDB data-plane worker (Landlock,
seccomp, rlimits) and the Docker execution backend. Both need Linux, so run the full suite
on Linux or in WSL2. CI runs the same steps (`.github/workflows/quality.yml`).

## What you need

| Requirement | Needed for | Check |
| :--- | :--- | :--- |
| Python 3.12+ and [uv](https://docs.astral.sh/uv/) | everything | `uv --version` |
| Linux kernel with Landlock enabled | every test that starts a data-plane worker | see below |
| Docker daemon you can reach without `sudo` | tests marked `docker` | `docker info` |
| The `agentic-analytics-exec:test` image | tests marked `docker` | `docker image inspect agentic-analytics-exec:test` |

**Landlock.** Check that the kernel offers it, from the repository root after `uv sync`:

```bash
uv run python -c "from agentic_analytics.dataplane.sandbox import landlock_abi; print(landlock_abi())"
```

A number above 0 means the data-plane tests can run. 0 means the kernel has no Landlock (or
it is not in the kernel's LSM list), and every data-plane worker fails closed with
`sandbox_unavailable`.

**Windows.** Native Windows is not supported: importing the package raises
`UnsupportedPlatformError`. Run the suite inside a WSL2 distribution. It has been run from a
clone in the Linux filesystem (for example `~/src`); a checkout under `/mnt/c` is untested.
`ruff` works on Windows, but `mypy src` there reports POSIX-only names (`fcntl`,
`os.killpg`, `resource.RLIMIT_*`) that are not errors on Linux.

## Steps

```bash
uv sync --frozen --extra dev --python 3.12
docker build -t agentic-analytics-exec:test -f docker/Dockerfile.exec .
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy src
uv run --frozen pytest --cov=agentic_analytics --cov-report=term-missing --cov-fail-under=85
```

The full run takes about 2–3 minutes. The first Docker test in a run is the slowest, because
it starts a cold container that imports pandas and matplotlib.

Rebuild the image whenever `docker/Dockerfile.exec` or `uv.lock` changes; the image installs
the `exec` dependency group from the lockfile. The supervisor that runs inside
the container (`execution_backends/supervisor.py`) is mounted from your checkout on every run,
so changing it needs no rebuild.

## When something is missing

Rather than failing many tests one by one, the suite skips the affected tests with one reason
(`pytest -ra` lists it):

- **No Landlock:** every test is skipped. To run the tests that do not assert confinement
  anyway, set `AGENTIC_ANALYTICS_DATA_PLANE_SANDBOX=off`. That is the same opt-out a server
  would use, and it removes the worker's confinement, so keep it to test runs.
- **No Docker, daemon not running, or no `:test` image:** only the tests marked `docker` are
  skipped, and the reason says which one and how to build the image.

CI must never pass with these skips, so it sets two variables that turn them into a usage error:

| Variable | Effect when set to `1` |
| :--- | :--- |
| `AGENTIC_ANALYTICS_TEST_REQUIRE_SANDBOX` | fail the run if Landlock is unavailable |
| `AGENTIC_ANALYTICS_TEST_REQUIRE_DOCKER` | fail the run if the `docker` tests cannot run |

Set both locally when you need proof that the whole suite ran, for example before a release.

## Useful subsets

```bash
uv run --frozen pytest tests/unit                 # fast, no Docker
uv run --frozen pytest -m "not docker"            # everything except the Docker backend
uv run --frozen pytest -m docker                  # only the Docker backend
```

Running a subset does not reach the 85% coverage floor, so leave out `--cov-fail-under` there.
