"""Vision capability probing and overrides."""

from __future__ import annotations

from aiharness.config.schema import ModelDef
from aiharness.session.attachments import model_supports_vision
from aiharness.setup import build_model, infer_vision_from_entry


def test_infer_vision_from_kimi_style_flag():
    assert infer_vision_from_entry({"id": "kimi-k3", "supports_image_in": True}) is True
    assert infer_vision_from_entry({"id": "x", "supports_image_in": False}) is False


def test_infer_vision_from_modalities():
    assert infer_vision_from_entry({"id": "m", "modalities": ["text", "image"]}) is True
    assert infer_vision_from_entry({"id": "m", "modalities": ["text"]}) is False
    assert infer_vision_from_entry({"id": "m", "note": "hello"}) is None


def test_build_model_stamps_k3_vision():
    model = build_model("k3", "k3", ["acc"])
    assert model.supports_vision is True
    assert model.vision_mode == "auto"
    assert model_supports_vision(model)


def test_auto_falls_back_to_name_heuristic_when_stamp_false():
    # Probe can omit/deny vision; known names like k3 still light up in auto.
    # Users who need to block it set vision_mode=off.
    model = build_model("k3", "k3", ["acc"], supports_vision=False)
    assert model.supports_vision is False
    assert model_supports_vision(model) is True


def test_force_off_blocks_k3():
    model = ModelDef(id="k3", model="k3", accounts=["a"], supports_vision=True, vision_mode="off")
    assert model_supports_vision(model) is False
