import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _manifest_entries() -> set:
    return {
        line.strip()
        for line in (ROOT / "deploy.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _repo_files(pattern: str) -> set:
    return {
        str(path.relative_to(ROOT))
        for path in ROOT.glob(pattern)
        if "__pycache__" not in path.parts
    }


def test_deploy_manifest_covers_the_python_package():
    """deploy.sh copies only what deploy.txt lists.

    A new module or template that is not listed never reaches the live
    install, which fails at import time or renders a 500.
    """
    listed = _manifest_entries()

    for pattern in ("nazman/**/*.py", "templates/*.html", "docs/*.md", "static/js/*.js", "static/css/*.css"):
        missing = sorted(_repo_files(pattern) - listed)
        assert not missing, f"missing from deploy.txt: {missing}"
