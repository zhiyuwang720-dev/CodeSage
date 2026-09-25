"""Small fixed-head fixtures for conservative multi-language one-hop hints."""

from pathlib import Path

from app.domains.deep_review.schemas.config import DeepReviewConfig
from app.domains.deep_review.services.blast_radius import analyze_blast_radius
from tests.deep_review.test_blast_radius import git


def repository(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "head")
    return repo, git(repo, "rev-parse", "HEAD")


async def test_go_module_and_package_fixed_head(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "go.mod": "module example.com/demo\n\ngo 1.23\n",
        "pkg/changed.go": "package pkg\nconst Value = 1\n",
        "pkg/sibling.go": "package pkg\n",
        "cmd/main.go": 'package main\nimport "example.com/demo/pkg"\n',
        "cmd/false.go": 'package main\n// import "example.com/demo/pkg"\n',
    })
    (repo / "cmd/main.go").write_text("package main\n", encoding="utf-8")
    result, _ = await analyze_blast_radius(["pkg/changed.go"], str(repo), head, DeepReviewConfig())
    assert {(hint.path, hint.relation) for hint in result.hints} == {
        ("cmd/main.go", "static_import"), ("pkg/sibling.go", "same_package"),
    }
    assert result.coverage_by_language == {"go": "analyzed"}


async def test_typescript_javascript_relative_imports_and_ambiguity(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "src/util.ts": "export const x = 1;\n",
        "src/main.ts": 'import {x} from "./util";\n',
        "src/reexport.mjs": 'export {x} from "./util.ts";\n',
        "src/ignore.ts": '// import {x} from "./util";\n',
        "src/ambig.ts": 'import {x} from "./both";\n',
        "src/both.ts": "export const x = 1;\n",
        "src/both.js": "export const x = 1;\n",
    })
    result, _ = await analyze_blast_radius(["src/util.ts"], str(repo), head, DeepReviewConfig())
    assert set(result.displayed_paths) == {"src/main.ts", "src/reexport.mjs"}
    assert result.coverage_by_language == {"typescript": "analyzed"}


async def test_c_family_quote_include_only(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "include/api.h": "#pragma once\n",
        "include/use.cc": '#include "api.h"\n',
        "src/no_edge.c": '#include <api.h>\n',
        "src/comment.c": '// #include "../include/api.h"\n',
    })
    result, _ = await analyze_blast_radius(["include/api.h"], str(repo), head, DeepReviewConfig())
    assert [(hint.path, hint.relation) for hint in result.hints] == [("include/use.cc", "header_include")]
    assert result.coverage_by_language == {"c_family": "analyzed"}


async def test_java_explicit_import_and_static_owner_only(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "src/main/java/p/Thing.java": "package p; public class Thing { public static int VALUE; }\n",
        "src/main/java/q/Use.java": "package q;\nimport p.Thing;\nclass Use {}\n",
        "src/main/java/q/Static.java": "package q;\nimport static p.Thing.VALUE;\nclass Static {}\n",
        "src/main/java/q/Wild.java": "package q;\nimport p.*;\nclass Wild {}\n",
        "src/main/java/q/Comment.java": "package q;\n// import p.Thing;\nclass Comment {}\n",
        "src/main/java/q/String.java": 'package q;\nclass String { String x = "import p.Thing;"; }\n',
        "src/main/java/module-info.java": "module demo { requires p; }\n",
    })
    result, _ = await analyze_blast_radius(
        ["src/main/java/p/Thing.java"], str(repo), head, DeepReviewConfig(),
    )
    assert set(result.displayed_paths) == {
        "src/main/java/q/Use.java", "src/main/java/q/Static.java",
    }
    assert result.coverage_by_language == {"java": "analyzed"}


async def test_java_duplicate_fqcn_and_module_import_do_not_guess(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "a/p/Thing.java": "package p; public class Thing {}\n",
        "b/p/Thing.java": "package p; public class Thing {}\n",
        "a/q/Use.java": "package q;\nimport p.Thing;\nclass Use {}\n",
        "a/q/Module.java": "package q;\nimport module p;\nclass Module {}\n",
    })
    result, _ = await analyze_blast_radius(["a/p/Thing.java"], str(repo), head, DeepReviewConfig())
    assert not result.hints
    assert "java_duplicate_fqcn" in result.diagnostics


async def test_mixed_language_budget_degrades_only_limited_adapter(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "a.py": "VALUE = 1\n", "b.py": "from a import VALUE\n",
        "src/x.ts": "export const x = 1;\n",
        "src/y.ts": 'import {x} from "./x";\n',
    })
    result, _ = await analyze_blast_radius(
        ["a.py", "src/x.ts"], str(repo), head,
        DeepReviewConfig(max_import_graph_files=1),
    )
    assert result.coverage_by_language == {"python": "degraded", "typescript": "degraded"}
    assert result.truncated


async def test_pyi_and_javascript_suffixes_route_to_analyzers(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "pkg/value.pyi": "VALUE: int\n", "pkg/user.py": "from pkg.value import VALUE\n",
        "web/util.cjs": "module.exports = 1;\n",
        "web/use.cjs": 'const x = require("./util.cjs");\n',
    })
    result, _ = await analyze_blast_radius(
        ["pkg/value.pyi", "web/util.cjs"], str(repo), head, DeepReviewConfig(),
    )
    assert result.coverage_by_language == {"python": "analyzed", "javascript": "analyzed"}
    assert set(result.displayed_paths) == {"pkg/user.py", "web/use.cjs"}


async def test_same_directory_test_pair_is_weak_hint_not_language_coverage(tmp_path: Path) -> None:
    repo, head = repository(tmp_path, {
        "pkg/value.go": "package pkg\n",
        "pkg/value_test.go": "package pkg\n",
    })
    result, _ = await analyze_blast_radius(
        ["pkg/value.go"], str(repo), head,
        DeepReviewConfig(include_paths=["pkg/value_test.go"]),
    )
    assert ("pkg/value_test.go", "test_pair") in {
        (hint.path, hint.relation) for hint in result.hints
    }
    assert result.coverage_by_language == {"go": "degraded"}  # no go.mod
