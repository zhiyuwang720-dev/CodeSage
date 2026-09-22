import pytest

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.schemas.input import ChangeType, FileChange
from app.domains.deep_review.services.directory_filter import DirectoryFilter, FilterError, normalize_path


def change(path: str, kind: ChangeType = ChangeType.MODIFIED, diff: str = "+x\n") -> FileChange:
    return FileChange(path=path, change_type=kind, diff=diff)


def test_normalize_path_rejects_escape_and_absolute() -> None:
    assert normalize_path("a\\b.py") == "a/b.py"
    with pytest.raises(FilterError):
        normalize_path("../a.py")
    with pytest.raises(FilterError):
        normalize_path("/a.py")
    with pytest.raises(FilterError):
        normalize_path("C:/a.py")


def test_secret_and_template_paths() -> None:
    result = DirectoryFilter(DeepReviewConfig()).filter(
        [change(".env"), change(".env.example"), change("keys/id_rsa")]
    )
    by_path = {item.path: item for item in result.decisions}
    assert by_path[".env"].action == "exclude"
    assert by_path[".env"].reason == "secret_path"
    assert by_path["keys/id_rsa"].reason == "secret_path"
    assert by_path[".env.example"].action == "review"


def test_user_include_can_reinclude_default_but_not_secret() -> None:
    config = DeepReviewConfig(include_paths=["**/fixtures/**"])
    result = DirectoryFilter(config).filter([change("src/fixtures/a.py"), change(".env")])
    assert result.review_paths == ["src/fixtures/a.py"]
    assert result.context_paths == []


def test_negation_and_last_user_rule_wins() -> None:
    config = DeepReviewConfig(exclude_paths=["src/**", "!src/safe.py"], include_paths=["src/unsafe.py"])
    result = DirectoryFilter(config).filter([change("src/safe.py"), change("src/unsafe.py")])
    assert result.review_paths == ["src/safe.py", "src/unsafe.py"]


def test_include_does_not_bypass_extension_or_binary() -> None:
    config = DeepReviewConfig(include_paths=["assets/**"])
    result = DirectoryFilter(DeepReviewConfig(include_paths=["assets/**"])).filter(
        [change("assets/readme.md"), change("assets/image.png", ChangeType.BINARY, "Binary files\n")]
    )
    assert [item.reason for item in result.decisions] == ["unsupported_extension", "binary_file"]


def test_deleted_is_context_and_too_large_excluded() -> None:
    config = DeepReviewConfig(max_file_bytes=4)
    result = DirectoryFilter(config).filter([change("old.py", ChangeType.DELETED), change("big.py", diff="+12345\n")])
    assert result.context_paths == ["old.py"]
    assert result.decisions[1].reason == "file_too_large"


def test_unsupported_and_default_excluded() -> None:
    result = DirectoryFilter(DeepReviewConfig()).filter([change("README.md"), change("package-lock.json")])
    assert [item.reason for item in result.decisions] == ["unsupported_extension", "default_exclude"]
