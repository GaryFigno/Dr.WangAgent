"""Pasted-image attachments: persist, wire expansion, vision degrade."""

from __future__ import annotations

import base64

import pytest
from PIL import Image

from aiharness.agent.loop import Agent, Notice
from aiharness.config.schema import ModelDef
from aiharness.providers.base import Message
from aiharness.session.attachments import (
    ImageAttachment,
    expand_message_for_wire,
    model_supports_vision,
    parse_inbound_images,
    save_attachments,
)

from .fake_openai import Reply


def _png_bytes(colour: str = "red") -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", (8, 8), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def test_parse_inbound_images_preserves_order():
    one = base64.b64encode(_png_bytes("red")).decode()
    two = base64.b64encode(_png_bytes("blue")).decode()
    images = parse_inbound_images(
        [
            {"mime": "image/png", "data": one, "name": "a.png"},
            {"mime": "image/png", "data": two, "name": "b.png"},
        ]
    )
    assert [image.name for image in images] == ["a.png", "b.png"]
    assert images[0].data != images[1].data


def test_save_attachments_writes_ordered_files(tmp_path):
    images = [
        ImageAttachment(mime="image/png", data=_png_bytes("red"), name="first.png"),
        ImageAttachment(mime="image/png", data=_png_bytes("blue"), name="second.png"),
    ]
    refs = save_attachments(tmp_path, images)
    assert [ref.index for ref in refs] == [1, 2]
    assert (tmp_path / refs[0].file).is_file()
    assert (tmp_path / refs[1].file).is_file()


def test_model_supports_vision_respects_flag_and_markers():
    assert model_supports_vision(ModelDef(id="x", model="gpt-4o", accounts=["a"]))
    assert model_supports_vision(
        ModelDef(id="x", model="deepseek-chat", accounts=["a"], supports_vision=True)
    )
    assert not model_supports_vision(
        ModelDef(id="x", model="deepseek-chat", accounts=["a"])
    )
    assert model_supports_vision(
        ModelDef(id="x", model="custom", accounts=["a"], tags=["vision"])
    )
    # Kimi K3 / Coding Plan shorthand
    assert model_supports_vision(ModelDef(id="k3", model="k3", accounts=["a"]))
    assert model_supports_vision(ModelDef(id="main", model="kimi-k3", accounts=["a"]))
    # DeepSeek V4 public API is text-only — do not treat as vision by name
    assert not model_supports_vision(
        ModelDef(id="deepseek-v4-flash", model="deepseek-v4-flash", accounts=["a"])
    )
    assert not model_supports_vision(
        ModelDef(id="deepseek-v4-pro", model="deepseek-v4-pro", accounts=["a"])
    )
    # User lock wins over heuristics
    assert not model_supports_vision(
        ModelDef(id="k3", model="k3", accounts=["a"], vision_mode="off")
    )
    assert model_supports_vision(
        ModelDef(
            id="plain",
            model="deepseek-v4-flash",
            accounts=["a"],
            vision_mode="on",
        )
    )


def test_expand_message_for_wire_builds_image_parts(tmp_path):
    refs = save_attachments(
        tmp_path,
        [ImageAttachment(mime="image/png", data=_png_bytes(), name="shot.png")],
    )
    message = Message(
        role="user",
        content="look",
        meta={"attachments": [ref.to_meta() for ref in refs]},
    )
    wired = expand_message_for_wire(message, tmp_path, include_images=True)
    assert isinstance(wired.content, list)
    assert wired.content[0] == {"type": "text", "text": "look"}
    assert wired.content[1]["type"] == "image_url"
    assert wired.content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    payload = Message(role="user", content=wired.content).to_wire()
    assert isinstance(payload["content"], list)


@pytest.mark.asyncio
async def test_images_persist_and_degrade_without_vision(
    fake, agent_parts, sessions
):
    config, router, tools, permissions, workspace = agent_parts
    fake.push(Reply(text="ok without vision"))
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    images = [ImageAttachment(mime="image/png", data=_png_bytes(), name="x.png")]

    events = [event async for event in agent.run("describe", images=images)]
    notices = [event for event in events if isinstance(event, Notice)]
    assert any("不支持图片" in event.text for event in notices)

    user = agent.messages[0]
    assert user.meta.get("attachments")
    assert "[图1: x.png]" in user.content
    wire = agent._wire_messages()
    user_wire = next(message for message in wire if message.role == "user")
    assert isinstance(user_wire.content, str)

    reloaded = sessions.open(session.meta.id)
    assert reloaded is not None
    saved = reloaded.full_history[0]
    assert saved.meta["attachments"]
    assert (session.directory / saved.meta["attachments"][0]["file"]).is_file()
    await router.aclose()


@pytest.mark.asyncio
async def test_images_go_multimodal_when_vision_enabled(
    fake, agent_parts, sessions
):
    config, router, tools, permissions, workspace = agent_parts
    config.models[0].supports_vision = True
    fake.push(Reply(text="I see a red square"))
    session = sessions.create(workspace)
    agent = Agent(config, router, tools, permissions, workspace, session=session)
    images = [
        ImageAttachment(mime="image/png", data=_png_bytes("red"), name="a.png"),
        ImageAttachment(mime="image/png", data=_png_bytes("blue"), name="b.png"),
    ]

    async for _ in agent.run("what colours?", images=images):
        pass
    wire = agent._wire_messages()
    user_wire = next(message for message in wire if message.role == "user")
    assert isinstance(user_wire.content, list)
    image_parts = [part for part in user_wire.content if part.get("type") == "image_url"]
    assert len(image_parts) == 2
    await router.aclose()
