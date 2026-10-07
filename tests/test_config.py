import os
import stat
from pathlib import Path

from nazman.config import set_setting


def _conf_path(settings) -> Path:
    return Path(settings.model_config["env_file"])


def test_set_setting_creates_conf_owner_only(override_settings):
    """A conf created at runtime must never be world-readable.

    It holds the admin password hash and the Telegram bot token.
    """
    path = _conf_path(override_settings)
    assert not path.exists()

    assert set_setting("alerts_enabled", True) is True

    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "alerts_enabled=true" in path.read_text()


def test_set_setting_keeps_an_existing_files_mode(override_settings):
    """Rewriting a live install's conf must not change its permissions."""
    path = _conf_path(override_settings)
    path.write_text("pool_usage_threshold = 95\n")
    os.chmod(path, 0o644)

    assert set_setting("pool_usage_threshold", 85) is True

    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert "pool_usage_threshold=85" in path.read_text()


def test_build_script_creates_conf_owner_only():
    """build.sh must not leave a freshly created conf at the default 0644."""
    script = (Path(__file__).resolve().parents[1] / "build.sh").read_text()
    assert "install -m 600 /dev/null /etc/nazman/nazman.conf" in script
    assert "chmod 600 /etc/nazman/nazman.conf" in script
