from nazman.utils.sizes import parse_size_to_bytes


def test_parse_raw_bytes():
    assert parse_size_to_bytes("1024") == 1024
    assert parse_size_to_bytes("0") == 0


def test_parse_single_letter_suffixes():
    assert parse_size_to_bytes("1K") == 1024
    assert parse_size_to_bytes("2M") == 2 * 1024**2
    assert parse_size_to_bytes("3.5G") == int(3.5 * 1024**3)
    assert parse_size_to_bytes("1T") == 1024**4
    assert parse_size_to_bytes("1P") == 1024**5
    assert parse_size_to_bytes("1k") == 1024


def test_parse_extended_suffixes():
    assert parse_size_to_bytes("1KB") == 1024
    assert parse_size_to_bytes("1MB") == 1024**2
    assert parse_size_to_bytes("1GiB") == 1024**3
    assert parse_size_to_bytes("2.5TB") == int(2.5 * 1024**4)


def test_parse_garbage_returns_zero():
    assert parse_size_to_bytes("") == 0
    assert parse_size_to_bytes(None) == 0
    assert parse_size_to_bytes("abc") == 0
    assert parse_size_to_bytes("  ") == 0