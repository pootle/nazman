from nazman.utils.paths import ensure_dir


def test_ensure_dir_creates_nested_path(tmp_path):
    target = tmp_path / "a" / "b" / "restore"
    result = ensure_dir(target)
    assert result == target
    assert target.is_dir()


def test_ensure_dir_existing_dir_is_a_noop(tmp_path):
    target = tmp_path / "restore"
    target.mkdir()
    ensure_dir(target)
    assert target.is_dir()


def test_ensure_dir_replaces_a_regular_file(tmp_path):
    target = tmp_path / "restore"
    target.write_text("stale")
    ensure_dir(target)
    assert target.is_dir()


def test_ensure_dir_replaces_a_dangling_symlink(tmp_path):
    target = tmp_path / "restore"
    target.symlink_to(tmp_path / "missing")
    ensure_dir(target)
    assert target.is_dir()
    assert not target.is_symlink()