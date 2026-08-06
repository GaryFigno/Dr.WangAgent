"""Post-edit Apply/Reject board (T38)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiharness.edits import EditReviewBoard
from aiharness.tools.base import ToolContext
from aiharness.tools.fs import EditTool, WriteTool


@pytest.fixture
def board():
    return EditReviewBoard()


def test_reject_restores_previous_content(board, tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("before\n", encoding="utf-8")
    path.write_text("after\n", encoding="utf-8")
    item = board.add(
        path=path,
        rel="a.txt",
        kind="write",
        before="before\n",
        after="after\n",
        created=False,
    )
    ok, _ = board.reject(item.id)
    assert ok
    assert path.read_text(encoding="utf-8") == "before\n"
    assert item.status == "rejected"


def test_reject_deletes_created_file(board, tmp_path: Path):
    path = tmp_path / "new.txt"
    path.write_text("fresh\n", encoding="utf-8")
    item = board.add(
        path=path,
        rel="new.txt",
        kind="write",
        before=None,
        after="fresh\n",
        created=True,
    )
    ok, _ = board.reject(item.id)
    assert ok
    assert not path.exists()


def test_stacked_edits_must_reject_newest_first(board, tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("v2\n", encoding="utf-8")
    first = board.add(
        path=path, rel="a.txt", kind="edit", before="v0\n", after="v1\n", old="v0", new="v1"
    )
    second = board.add(
        path=path, rel="a.txt", kind="edit", before="v1\n", after="v2\n", old="v1", new="v2"
    )
    ok, msg = board.reject(first.id)
    assert not ok
    assert "更新" in msg
    ok, _ = board.reject(second.id)
    assert ok
    assert path.read_text(encoding="utf-8") == "v1\n"
    path.write_text("v1\n", encoding="utf-8")  # already restored
    # first's after was v1; disk matches — reject first next
    ok, _ = board.reject(first.id)
    assert ok
    assert path.read_text(encoding="utf-8") == "v0\n"


def test_apply_is_ack_only(board, tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("done\n", encoding="utf-8")
    item = board.add(
        path=path, rel="a.txt", kind="write", before="old\n", after="done\n", created=False
    )
    ok, _ = board.apply(item.id)
    assert ok
    assert path.read_text(encoding="utf-8") == "done\n"
    assert board.pending() == []


def test_reject_all_unwinds_stack(board, tmp_path: Path):
    path = tmp_path / "a.txt"
    path.write_text("v2\n", encoding="utf-8")
    board.add(path=path, rel="a.txt", kind="edit", before="v0\n", after="v1\n")
    board.add(path=path, rel="a.txt", kind="edit", before="v1\n", after="v2\n")
    count, errors = board.reject_all()
    assert count == 2
    assert errors == []
    assert path.read_text(encoding="utf-8") == "v0\n"


@pytest.mark.asyncio
async def test_write_tool_queues_review(workspace, config, router):
    from aiharness.permissions import PermissionEngine

    board = EditReviewBoard()
    ctx = ToolContext(
        workspace=workspace,
        config=config,
        permissions=PermissionEngine(config.permissions, workspace),
        router=router,
        edit_review=board,
        current_call_id="c1",
    )
    result = await WriteTool().run(
        {"file_path": "queued.txt", "content": "hello\n"}, ctx
    )
    assert not result.is_error
    assert len(board.pending()) == 1
    assert board.pending()[0].created is True
    assert board.pending()[0].call_id == "c1"
    await router.aclose()


@pytest.mark.asyncio
async def test_edit_tool_queues_review(workspace, config, router):
    from aiharness.permissions import PermissionEngine

    board = EditReviewBoard()
    ctx = ToolContext(
        workspace=workspace,
        config=config,
        permissions=PermissionEngine(config.permissions, workspace),
        router=router,
        edit_review=board,
        current_call_id="c2",
    )
    path = workspace / "hello.txt"
    ctx.read_files[str(path.resolve())] = path.stat().st_mtime
    result = await EditTool().run(
        {"file_path": "hello.txt", "old_string": "line one", "new_string": "line ONE"},
        ctx,
    )
    assert not result.is_error
    item = board.pending()[0]
    assert item.kind == "edit"
    assert "line ONE" in item.after
    assert "line one" in item.before
    await router.aclose()

