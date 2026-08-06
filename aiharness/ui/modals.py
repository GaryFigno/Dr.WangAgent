"""Modal dialogs: tool approval and destructive-action confirmation."""

from __future__ import annotations

import json
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..permissions import Verdict

#: Characters of a tool argument shown in the approval dialog.
ARG_PREVIEW_CHARS = 1200


def _describe_call(tool: str, args: dict[str, Any]) -> str:
    """Render a pending tool call for human review."""
    if tool == "Bash":
        command = str(args.get("command", ""))
        note = str(args.get("description", ""))
        return f"$ {command}" + (f"\n\n{note}" if note else "")
    if tool in ("Write", "Edit"):
        path = args.get("file_path", "?")
        if tool == "Write":
            content = str(args.get("content", ""))
            return f"write {path}\n\n{content[:ARG_PREVIEW_CHARS]}"
        old = str(args.get("old_string", ""))[:ARG_PREVIEW_CHARS // 2]
        new = str(args.get("new_string", ""))[:ARG_PREVIEW_CHARS // 2]
        return f"edit {path}\n\n- {old}\n\n+ {new}"
    try:
        rendered = json.dumps(args, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        rendered = str(args)
    return f"{tool}\n\n{rendered[:ARG_PREVIEW_CHARS]}"


class PermissionModal(ModalScreen[str]):
    """Asks the user to approve one tool call.

    Dismisses with ``"once"``, ``"always"`` or ``"deny"``.
    """

    BINDINGS = [
        ("y", "approve_once", "Approve"),
        ("a", "approve_always", "Always"),
        ("n", "deny", "Deny"),
        ("escape", "deny", "Deny"),
    ]

    def __init__(self, tool: str, args: dict[str, Any], verdict: Verdict):
        super().__init__()
        self.tool = tool
        self.args = args
        self.verdict = verdict

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"Approve {self.tool}?", classes="title")
            if self.verdict.reason:
                yield Static(self.verdict.reason, classes="reason")
            yield Static(Text(_describe_call(self.tool, self.args), no_wrap=False), classes="command")
            if self.verdict.suggested_rule:
                yield Static(
                    f"“Always” adds the rule {self.verdict.suggested_rule} for this session.",
                    classes="reason",
                )
            yield Static("[y] approve   [a] always   [n] deny", classes="reason")
            yield Button("Approve once", id="once", variant="primary")
            yield Button("Always this session", id="always")
            yield Button("Deny", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "deny")

    def action_approve_once(self) -> None:
        self.dismiss("once")

    def action_approve_always(self) -> None:
        self.dismiss("always")

    def action_deny(self) -> None:
        self.dismiss("deny")


class QuestionModal(ModalScreen[dict]):
    """Asks the user one or more multiple-choice questions.

    "Other" is always offered and always last: pre-baked options are a
    convenience, not a cage, and the answer the agent failed to think of is
    often the important one.
    """

    BINDINGS = [("escape", "dismiss_empty", "Skip")]

    def __init__(self, questions: list[Any]):
        super().__init__()
        self.questions = questions
        self._answers: dict[str, str] = {}
        self._index = 0

    @property
    def current(self) -> Any:
        return self.questions[self._index]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("", id="question-progress", classes="reason")
            yield Static("", id="question-text", classes="title")
            yield Vertical(id="question-options")
            yield Static("Type a number to choose, or `esc` to skip.", classes="reason")
            yield Input(placeholder="Or type your own answer…", id="question-other")

    def on_mount(self) -> None:
        self._render_question()
        self.query_one("#question-other", Input).focus()

    def _render_question(self) -> None:
        question = self.current
        self.query_one("#question-progress", Static).update(
            f"[{question.header}]  {self._index + 1}/{len(self.questions)}"
        )
        self.query_one("#question-text", Static).update(question.question)

        container = self.query_one("#question-options", Vertical)
        container.remove_children()
        for number, option in enumerate(question.options, 1):
            label = option.get("label", "")
            description = option.get("description", "")
            button = Button(f"{number}. {label}", id=f"opt-{number - 1}")
            container.mount(button)
            if description:
                container.mount(Static(f"   {description}", classes="reason"))

    def _record(self, answer: str) -> None:
        self._answers[self.current.header] = answer
        self._index += 1
        if self._index >= len(self.questions):
            self.dismiss(self._answers)
        else:
            self.query_one("#question-other", Input).value = ""
            self._render_question()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("opt-"):
            return
        index = int(event.button.id.split("-")[1])
        self._record(self.current.options[index].get("label", ""))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        # A bare number selects an option; anything else is a free answer.
        if text.isdigit() and 1 <= int(text) <= len(self.current.options):
            self._record(self.current.options[int(text) - 1].get("label", ""))
        else:
            self._record(text)

    def action_dismiss_empty(self) -> None:
        self.dismiss(self._answers)


class PlanModal(ModalScreen[tuple]):
    """Shows a plan and takes approval or feedback.

    Dismisses with ``(approved, feedback)``.
    """

    BINDINGS = [
        ("ctrl+y", "approve", "Approve"),
        ("escape", "cancel", "Keep editing"),
    ]

    def __init__(self, plan: Any, chinese: bool = True):
        super().__init__()
        self.plan = plan
        self.chinese = chinese

    def compose(self) -> ComposeResult:
        title = "实施计划" if self.chinese else "Implementation plan"
        hint = (
            "回车提交修改意见 · ctrl+y 批准并开始执行"
            if self.chinese
            else "Enter to send feedback · ctrl+y to approve and start"
        )
        with Vertical(id="dialog"):
            yield Static(f"{title} — rev {self.plan.revision}", classes="title")
            with VerticalScroll(id="plan-body"):
                yield Static(Markdown(self.plan.render(self.chinese)))
            yield Static(hint, classes="reason")
            yield Input(placeholder="要改什么？ / What should change?", id="plan-feedback")
            yield Button("批准 / Approve", id="approve", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#plan-feedback", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            self.dismiss((True, ""))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        feedback = event.value.strip()
        if feedback:
            self.dismiss((False, feedback))

    def action_approve(self) -> None:
        self.dismiss((True, ""))

    def action_cancel(self) -> None:
        self.dismiss((False, "The user closed the plan without commenting. Ask what they want changed."))


class ConfirmModal(ModalScreen[bool]):
    """Confirms an irreversible action such as deleting sessions."""

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "No"),
    ]

    def __init__(self, title: str, detail: str = ""):
        super().__init__()
        self.title_text = title
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.title_text, classes="title")
            if self.detail:
                yield Static(Text(self.detail, no_wrap=False), classes="reason")
            yield Static("[y] confirm   [n] cancel", classes="reason")
            yield Button("Confirm", id="yes", variant="error")
            yield Button("Cancel", id="no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
