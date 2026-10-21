from typer.testing import CliRunner

import tester
from tester.cli import app


def test_dependencies_import():
    import dotenv, httpx, matplotlib, pandas, pydantic, yaml  # noqa: F401


def test_version_command():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == tester.__version__
