"""C14: native Windows is refused with one clear error; POSIX hosts start."""

import pytest

from agentic_analytics.support import UnsupportedPlatformError, check_supported_platform


def test_native_windows_is_refused_with_a_pointer_to_wsl2() -> None:
    with pytest.raises(UnsupportedPlatformError, match="WSL2"):
        check_supported_platform("win32")


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_hosts_are_accepted(platform: str) -> None:
    check_supported_platform(platform)
