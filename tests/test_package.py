"""Install package must contain the integration and no local evaluation data."""

import json
from zipfile import ZipFile

from scripts.build_release import build


def test_install_archive(tmp_path):
    output = build(tmp_path / "integration.zip")
    with ZipFile(output) as archive:
        names = archive.namelist()
        root = "custom_components/home_heating_optimisation/"
        assert all(n.startswith(root) for n in names)
        assert root + "__init__.py" in names
        assert root + "config_flow.py" in names
        manifest = json.loads(archive.read(root + "manifest.json"))
        assert manifest["domain"] == "home_heating_optimisation"
        assert manifest["version"] == "0.1.0"
        assert json.loads(archive.read(root + "strings.json")) == json.loads(
            archive.read(root + "translations/en.json")
        )
        assert all(
            not any(v in n for v in (".env", "__pycache__", ".storage", "local-data"))
            for n in names
        )
    first = output.read_bytes()
    build(output)
    assert output.read_bytes() == first
