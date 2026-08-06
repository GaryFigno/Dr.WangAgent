"""Agent identities, mailboxes and team tools."""

from __future__ import annotations

import asyncio

import pytest

from aiharness.agent.mesh import AgentMesh, Mailbox, MeshError, MessageKind
from aiharness.permissions import PermissionEngine
from aiharness.tools.base import ToolContext
from aiharness.tools.team import InboxTool, SendMessageTool, SpawnAgentTool, TeamTool
from aiharness.toolset import build_registry

from .fake_openai import Reply


@pytest.fixture
def mesh() -> AgentMesh:
    return AgentMesh()


def ctx_for(config, workspace, router, mesh, identity=None, **kwargs) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        config=config,
        permissions=PermissionEngine(config.permissions, workspace),
        router=router,
        mesh=mesh,
        identity=identity,
        **kwargs,
    )


# -- identities ------------------------------------------------------------


def test_roles_are_slugified_and_unique(mesh):
    first = mesh.register("API Layer", "own the API")
    assert first.role == "api-layer"
    with pytest.raises(MeshError):
        mesh.register("api layer", "duplicate")


def test_agents_resolve_by_role_or_id(mesh):
    identity = mesh.register("reviewer", "find defects")
    assert mesh.resolve("reviewer") is identity
    assert mesh.resolve(identity.id) is identity
    assert mesh.resolve("nobody") is None


def test_the_mesh_is_bounded(mesh):
    small = AgentMesh(max_agents=2)
    small.register("a", "x")
    small.register("b", "y")
    with pytest.raises(MeshError):
        small.register("c", "z")


def test_retiring_frees_the_role(mesh):
    mesh.register("builder", "build")
    assert mesh.retire("builder") is True
    assert mesh.resolve("builder") is None
    mesh.register("builder", "build again")  # the name is reusable


def test_file_ownership_conflicts_are_detected(mesh):
    owner = mesh.register("api", "own the API", owns=["api.py", "schema.py"])
    other = mesh.register("client", "own the client", owns=["client.py"])
    assert mesh.conflicts(other.id, ["api.py", "client.py"]) == ["api.py"]
    # An agent never conflicts with itself.
    assert mesh.conflicts(owner.id, ["api.py"]) == []


# -- messaging -------------------------------------------------------------


def test_messages_reach_the_recipient(mesh):
    mesh.register("api", "a")
    mesh.register("client", "b")
    mesh.send("api", "client", "I changed the signature of fetch()")

    inbox = mesh.mailbox("client").drain()
    assert len(inbox) == 1
    assert "signature" in inbox[0].content
    assert mesh.mailbox("client").pending == 0


def test_sending_to_an_unknown_agent_names_the_known_ones(mesh):
    mesh.register("api", "a")
    with pytest.raises(MeshError) as error:
        mesh.send("api", "ghost", "hello")
    assert "api" in str(error.value)


def test_empty_messages_are_refused(mesh):
    mesh.register("a", "x")
    mesh.register("b", "y")
    with pytest.raises(MeshError):
        mesh.send("a", "b", "   ")


def test_long_messages_are_truncated_not_dropped(mesh):
    mesh.register("a", "x")
    mesh.register("b", "y")
    mesh.send("a", "b", "x" * 20000)
    assert "truncated" in mesh.mailbox("b").drain()[0].content


def test_broadcast_skips_the_sender(mesh):
    mesh.register("a", "x")
    mesh.register("b", "y")
    mesh.register("c", "z")
    sent = mesh.broadcast(mesh.resolve("a").id, "heads up")
    assert len(sent) == 2
    assert mesh.mailbox("a").pending == 0
    assert mesh.mailbox("b").pending == 1


def test_mailbox_is_bounded_and_reports_losses():
    mailbox = Mailbox("x", limit=3)
    for index in range(6):
        mailbox.deliver(
            type("M", (), {"reply_to": None, "content": str(index), "id": str(index)})()
        )
    assert mailbox.pending == 3
    assert mailbox.dropped == 3


async def test_waiting_for_a_reply_resolves_when_it_arrives(mesh):
    mesh.register("asker", "a")
    mesh.register("answerer", "b")
    question = mesh.send("asker", "answerer", "what shape is the payload?", kind=MessageKind.REQUEST)

    async def answer() -> None:
        await asyncio.sleep(0.05)
        mesh.send("answerer", "asker", "a dict with id and name", reply_to=question.id)

    asyncio.create_task(answer())
    reply = await mesh.mailbox("asker").wait_for_reply(question.id, timeout=5)
    assert reply is not None
    assert "dict" in reply.content


async def test_waiting_for_a_reply_times_out_cleanly(mesh):
    mesh.register("asker", "a")
    reply = await mesh.mailbox("asker").wait_for_reply("never-sent", timeout=0.05)
    assert reply is None


# -- the tools -------------------------------------------------------------


async def test_send_and_inbox_round_trip(config, workspace, router, mesh):
    sender = mesh.register("api", "own the API")
    mesh.register("client", "own the client")

    send_ctx = ctx_for(config, workspace, router, mesh, identity=sender)
    result = await SendMessageTool().run(
        {"to": "client", "content": "fetch() now returns a dict", "kind": "info"}, send_ctx
    )
    assert not result.is_error

    read_ctx = ctx_for(config, workspace, router, mesh, identity=mesh.resolve("client"))
    inbox = await InboxTool().run({}, read_ctx)
    assert "returns a dict" in inbox.content
    # Draining empties it.
    assert "No new messages" in (await InboxTool().run({}, read_ctx)).content
    await router.aclose()


async def test_peek_leaves_messages_in_place(config, workspace, router, mesh):
    mesh.register("a", "x")
    target = mesh.register("b", "y")
    mesh.send("a", "b", "still here")

    ctx = ctx_for(config, workspace, router, mesh, identity=target)
    await InboxTool().run({"peek": True}, ctx)
    assert mesh.mailbox("b").pending == 1
    await router.aclose()


async def test_send_to_unknown_agent_is_an_error_result(config, workspace, router, mesh):
    ctx = ctx_for(config, workspace, router, mesh)
    result = await SendMessageTool().run({"to": "ghost", "content": "hi"}, ctx)
    assert result.is_error
    await router.aclose()


async def test_team_tool_lists_the_roster(config, workspace, router, mesh):
    mesh.register("api", "own the API", owns=["api.py"])
    mesh.register("reviewer", "find defects")
    result = await TeamTool().run({}, ctx_for(config, workspace, router, mesh))
    assert "api" in result.content
    assert "reviewer" in result.content
    assert "api.py" in result.content
    await router.aclose()


async def test_spawn_refuses_overlapping_file_ownership(config, workspace, router, mesh):
    mesh.register("api", "own the API", owns=["shared.py"])
    ctx = ctx_for(config, workspace, router, mesh)
    result = await SpawnAgentTool().run(
        {"role": "client", "brief": "do the client", "owns": ["shared.py"]}, ctx
    )
    assert result.is_error
    assert "already owned" in result.content
    await router.aclose()


async def test_spawn_creates_a_child_session(fake, config, workspace, router, mesh, sessions):
    created: list = []

    def make_session(title: str):
        handle = sessions.create(workspace)
        handle.rename(title)
        created.append(handle)
        return handle

    fake.default = Reply(text="finished my part")
    ctx = ctx_for(config, workspace, router, mesh, make_session=make_session)
    result = await SpawnAgentTool().run(
        {"role": "builder", "brief": "implement the thing", "owns": ["a.py"]}, ctx
    )

    assert not result.is_error
    assert len(created) == 1
    identity = mesh.resolve("builder")
    assert identity.session_id == created[0].meta.id
    assert identity.owns == ["a.py"]
    await router.aclose()


async def test_spawn_requires_role_and_brief(config, workspace, router, mesh):
    ctx = ctx_for(config, workspace, router, mesh)
    assert (await SpawnAgentTool().run({"role": "", "brief": "x"}, ctx)).is_error
    assert (await SpawnAgentTool().run({"role": "a", "brief": ""}, ctx)).is_error
    await router.aclose()


def test_teammates_cannot_spawn_more_agents():
    """Only the lead assembles the team; members get messaging only."""
    registry = build_registry()
    subagent_names = {spec["function"]["name"] for spec in registry.specs(subagent=True)}
    assert "SpawnAgent" not in subagent_names
    assert "Orchestrate" not in subagent_names
    assert "SendMessage" in registry.names()


def test_lead_gets_the_full_team_toolkit():
    names = build_registry().names()
    for tool in ("SpawnAgent", "SendMessage", "Inbox", "Team"):
        assert tool in names
