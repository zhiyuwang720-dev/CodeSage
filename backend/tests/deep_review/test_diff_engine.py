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
