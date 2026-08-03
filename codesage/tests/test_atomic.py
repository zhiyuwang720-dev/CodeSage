"""Atomic write tests."""

from codesage.config import atomic_write


def test_write_and_read(tmp_path):
    target = tmp_path / "data.json"
    atomic_write(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_no_stray_tmp_files(tmp_path):
    target = tmp_path / "data.json"
    atomic_write(target, "hello")
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_overwrite(tmp_path):
    target = tmp_path / "data.json"
    atomic_write(target, "v1")
    atomic_write(target, "v2")
    assert target.read_text(encoding="utf-8") == "v2"


def test_read_json_lossy(tmp_path):
    from codesage.config import read_json_lossy

    (tmp_path / "ok.json").write_text('{"x": 1}', encoding="utf-8")
    assert read_json_lossy(tmp_path / "ok.json", {}) == {"x": 1}
    assert read_json_lossy(tmp_path / "missing.json", {"d": 1}) == {"d": 1}
    (tmp_path / "bad.json").write_text("[1,2]", encoding="utf-8")  # not a dict
    assert read_json_lossy(tmp_path / "bad.json", {"d": 1}) == {"d": 1}
