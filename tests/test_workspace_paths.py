"""Local path index and @ refs."""

from pathlib import Path

from aiharness.workspace.paths import build_refs_block, list_paths, list_tree


def test_list_paths_finds_workspace_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "a.js").write_text("1", encoding="utf-8")
    paths = list_paths(tmp_path, query="main")
    assert any(p["path"] == "src/main.py" for p in paths)
    assert not any("node_modules" in p["path"] for p in paths)


def test_list_tree_one_level(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    nodes = list_tree(tmp_path)
    names = {n["name"] for n in nodes}
    assert "a.txt" in names
    assert "sub" in names


def test_build_refs_block_includes_file(tmp_path: Path):
    (tmp_path / "note.md").write_text("hello ref\n", encoding="utf-8")
    block, sources = build_refs_block(tmp_path, ["note.md"])
    assert "hello ref" in block
    assert sources == ["ref:note.md"]
