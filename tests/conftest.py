import os
import tempfile

# Point the app at an isolated sqlite DB before any app import binds the engine.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp.name
os.environ["ADMIN_EMAIL"] = "admin@clusterx.local"
os.environ["ADMIN_PASSWORD"] = "ChangeMe123!"

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _seeded_db():
    from app.seed import run_seed

    counts = run_seed()
    assert counts.get("users", 0) >= 0
    return counts
