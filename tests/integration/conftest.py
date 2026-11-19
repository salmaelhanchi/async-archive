from pathlib import Path

import pytest

from tests.integration.serving import SettingsFactory, make_settings


@pytest.fixture
def settings(tmp_path: Path) -> SettingsFactory:
    return make_settings(tmp_path)
