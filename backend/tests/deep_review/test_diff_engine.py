from app.domains.deep_review.schemas.input import ChangeType, FileChange
from app.domains.deep_review.services.diff_engine import build_anatomy, cluster_changes, compute_stats, parse_hunks


DIFF = """@@ -1,3 +1,4 @@
 unchanged
-old
+new
 unchanged
"""


def test_parse_hunks_and_stats() -> None:
    change = FileChange(path="src/a.py", change_type=ChangeType.MODIFIED, diff=DIFF, additions=1, deletions=1)
    hunks = parse_hunks(change)
    assert len(hunks) == 1
    assert hunks[0].new_start == 1
    assert "+new" in hunks[0].content
    stats = compute_stats([change])
    assert stats.total_files == 1
    assert stats.total_additions == 1
    assert stats.total_deletions == 1
    assert stats.files_modified == 1


def test_clusters_and_anatomy() -> None:
    files = [
        FileChange(path="src/a.py", change_type=ChangeType.MODIFIED, diff=DIFF),
        FileChange(path="src/b.py", change_type=ChangeType.ADDED, diff=DIFF),
        FileChange(path="c.py", change_type=ChangeType.MODIFIED, diff=DIFF),
    ]
    clusters = cluster_changes(files)
    assert [cluster.name for cluster in clusters] == ["root", "src"]
    anatomy = build_anatomy(files, ["caller.py"])
    assert anatomy.directories == ["root", "src"]
    assert anatomy.related_paths == ["caller.py"]
    assert "Files: 3" in anatomy.summary


def test_cluster_ids_are_stable_and_path_derived() -> None:
    files = [
        FileChange(path="src/a.py", change_type=ChangeType.MODIFIED, diff=DIFF),
        FileChange(path="src/b.py", change_type=ChangeType.ADDED, diff=DIFF),
        FileChange(path="root-file.py", change_type=ChangeType.MODIFIED, diff=DIFF),
    ]
    first = cluster_changes(files)
    second = cluster_changes(list(reversed(files)))

    assert [item.id for item in first] == [item.id for item in second]
    assert len({item.id for item in first}) == len(first)
    assert first == second


def test_directory_depth_groups_sibling_directories() -> None:
    files = [
        FileChange(path="src/a/one.py", change_type=ChangeType.MODIFIED, diff=DIFF),
        FileChange(path="src/b/two.py", change_type=ChangeType.MODIFIED, diff=DIFF),
        FileChange(path="docs/readme.md", change_type=ChangeType.MODIFIED, diff=DIFF),
    ]

    clusters = cluster_changes(files, directory_depth=1)

    assert [item.name for item in clusters] == ["docs", "src"]
    assert clusters[1].files == ["src/a/one.py", "src/b/two.py"]


def test_oversized_directory_splits_recursively_without_loss() -> None:
    files = [
        FileChange(path="src/a/a1.py", change_type=ChangeType.MODIFIED, diff=DIFF),
        FileChange(path="src/a/a2.py", change_type=ChangeType.MODIFIED, diff=DIFF),
        FileChange(path="src/b/b1.py", change_type=ChangeType.MODIFIED, diff=DIFF),
        FileChange(path="src/b/b2.py", change_type=ChangeType.MODIFIED, diff=DIFF),
    ]

    clusters = cluster_changes(files, directory_depth=1, max_files_per_cluster=2)

    assert [item.name for item in clusters] == ["src/a", "src/b"]
    assert sorted(path for item in clusters for path in item.files) == sorted(
        item.path for item in files
    )
    assert all(len(item.files) <= 2 for item in clusters)
