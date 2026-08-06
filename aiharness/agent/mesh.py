"""A mesh of cooperating agents.

The subagents in :mod:`aiharness.agent.subagent` are fire-and-forget: you
hand one a task, it works alone, it reports back, it is gone. That covers
most delegation, and it is the right default because isolated agents cannot
confuse each other.

Some work does not fit that shape. When several agents hold different parts
of one codebase, the one editing the API needs to tell the one editing the
client that a signature changed. This module adds the minimum needed for
that, and no more:

* **Identities** — a named role, bound to a model and account, with its own
  persisted session so the user can read what it did.
* **Mailboxes** — agents send each other messages; a send can block for a
  reply or drop it in the inbox and continue.
* **A registry** — so an agent can discover who else is on the job.

What it deliberately does *not* add: shared memory, broadcast storms, or
agents spawning peers without limit. Every identity is created by the lead
agent or by the user, and the depth guard from the subagent module still
applies.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from ..constants import (
    MAILBOX_LIMIT,
    MAX_CHILD_SESSIONS,
    MAX_MESSAGE_CHARS,
    MESSAGE_REPLY_TIMEOUT,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


class MessageKind(Enum):
    """Why one agent is writing to another."""

    INFO = "info"  # here is something you need to know
    REQUEST = "request"  # please do this / please answer this
    REPLY = "reply"  # answering an earlier request
    HANDOFF = "handoff"  # this work is now yours


@dataclass
class AgentMessage:
    """One message between two agents."""

    sender: str
    recipient: str
    content: str
    kind: MessageKind = MessageKind.INFO
    reply_to: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    at: float = field(default_factory=time.time)

    def render(self) -> str:
        header = f"[{self.kind.value} from {self.sender}]"
        if self.reply_to:
            header += f" (re: {self.reply_to})"
        return f"{header}\n{self.content}"


@dataclass
class AgentIdentity:
    """One participant in the mesh."""

    id: str
    role: str
    brief: str
    #: Model spec: ``model``, ``model@account`` or ``role:name``.
    model: str = ""
    session_id: str = ""
    #: Files this identity owns; used to keep parallel edits from colliding.
    owns: list[str] = field(default_factory=list)
    parent: str | None = None
    busy: bool = False
    created_at: float = field(default_factory=time.time)

    def describe(self) -> str:
        parts = [f"**{self.role}** (`{self.id}`)"]
        if self.model:
            parts.append(f"on {self.model}")
        if self.owns:
            parts.append(f"owns {', '.join(self.owns)}")
        parts.append("busy" if self.busy else "idle")
        return " · ".join(parts)


class Mailbox:
    """One agent's inbox.

    Bounded on purpose: an agent that has fallen behind by fifty messages is
    not going to catch up, and an unbounded queue turns that into a memory
    leak instead of a visible problem.
    """

    def __init__(self, owner: str, limit: int = MAILBOX_LIMIT):
        self.owner = owner
        self.limit = limit
        self._messages: list[AgentMessage] = []
        self._waiters: dict[str, asyncio.Future] = {}
        self.dropped = 0

    def deliver(self, message: AgentMessage) -> None:
        """Place a message in the inbox, waking any waiter it answers."""
        if message.reply_to and message.reply_to in self._waiters:
            future = self._waiters.pop(message.reply_to)
            if not future.done():
                future.set_result(message)
                return
        self._messages.append(message)
        if len(self._messages) > self.limit:
            self.dropped += len(self._messages) - self.limit
            del self._messages[: -self.limit]

    def drain(self) -> list[AgentMessage]:
        """Take everything waiting, leaving the inbox empty."""
        messages, self._messages = self._messages, []
        return messages

    def peek(self) -> list[AgentMessage]:
        return list(self._messages)

    @property
    def pending(self) -> int:
        return len(self._messages)

    async def wait_for_reply(
        self, message_id: str, timeout: float = MESSAGE_REPLY_TIMEOUT
    ) -> AgentMessage | None:
        """Block until a reply to ``message_id`` arrives, or time out."""
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters[message_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._waiters.pop(message_id, None)
            return None


class MeshError(Exception):
    """Raised for unroutable messages and identity limits."""


class AgentMesh:
    """Tracks identities and moves messages between them."""

    def __init__(self, *, max_agents: int = MAX_CHILD_SESSIONS):
        self.max_agents = max_agents
        self._identities: dict[str, AgentIdentity] = {}
        self._mailboxes: dict[str, Mailbox] = {}
        self._log: list[AgentMessage] = []

    # -- identities -------------------------------------------------------

    def register(
        self,
        role: str,
        brief: str,
        *,
        model: str = "",
        owns: list[str] | None = None,
        parent: str | None = None,
        agent_id: str | None = None,
    ) -> AgentIdentity:
        """Add a participant.

        Args:
          role: Short name the other agents address it by, e.g. ``api``.
          brief: What this identity is responsible for.
          model: Optional model spec pinning which model plays this role.
          owns: Files this identity is allowed to edit.
          parent: The identity that created this one.
          agent_id: Explicit id; generated when omitted.

        Returns:
          The registered identity.

        Raises:
          MeshError: If the mesh is full or the role name is taken.
        """
        if len(self._identities) >= self.max_agents:
            raise MeshError(
                f"the mesh already holds {self.max_agents} agents; retire one first"
            )
        slug = _slugify(role)
        if any(identity.role == slug for identity in self._identities.values()):
            raise MeshError(f"role '{slug}' is already taken")

        identity = AgentIdentity(
            id=agent_id or f"{slug}-{uuid.uuid4().hex[:4]}",
            role=slug,
            brief=brief,
            model=model,
            owns=list(owns or []),
            parent=parent,
        )
        self._identities[identity.id] = identity
        self._mailboxes[identity.id] = Mailbox(identity.id)
        return identity

    def retire(self, agent_id: str) -> bool:
        """Remove an identity and discard its inbox."""
        resolved = self.resolve(agent_id)
        if resolved is None:
            return False
        self._identities.pop(resolved.id, None)
        self._mailboxes.pop(resolved.id, None)
        return True

    def resolve(self, key: str) -> AgentIdentity | None:
        """Find an identity by id or by role name."""
        if key in self._identities:
            return self._identities[key]
        slug = _slugify(key)
        return next((i for i in self._identities.values() if i.role == slug), None)

    def all(self) -> list[AgentIdentity]:
        return sorted(self._identities.values(), key=lambda i: i.created_at)

    def mailbox(self, agent_id: str) -> Mailbox | None:
        identity = self.resolve(agent_id)
        return self._mailboxes.get(identity.id) if identity else None

    # -- messaging --------------------------------------------------------

    def send(
        self,
        sender: str,
        recipient: str,
        content: str,
        *,
        kind: MessageKind = MessageKind.INFO,
        reply_to: str | None = None,
    ) -> AgentMessage:
        """Route one message.

        Raises:
          MeshError: If the recipient is unknown or the message is empty.
        """
        target = self.resolve(recipient)
        if target is None:
            known = ", ".join(i.role for i in self.all()) or "(nobody)"
            raise MeshError(f"no agent '{recipient}'. Known agents: {known}")
        body = content.strip()
        if not body:
            raise MeshError("refusing to send an empty message")
        if len(body) > MAX_MESSAGE_CHARS:
            body = body[:MAX_MESSAGE_CHARS] + "\n\n[message truncated]"

        message = AgentMessage(
            sender=sender, recipient=target.id, content=body, kind=kind, reply_to=reply_to
        )
        self._mailboxes[target.id].deliver(message)
        self._log.append(message)
        return message

    def broadcast(
        self, sender: str, content: str, *, kind: MessageKind = MessageKind.INFO
    ) -> list[AgentMessage]:
        """Send to everyone except the sender."""
        sent = []
        for identity in self.all():
            if identity.id == sender:
                continue
            sent.append(self.send(sender, identity.id, content, kind=kind))
        return sent

    def history(self, limit: int = 50) -> list[AgentMessage]:
        return self._log[-limit:]

    # -- coordination helpers ---------------------------------------------

    def conflicts(self, agent_id: str, files: list[str]) -> list[str]:
        """Return files another identity already owns.

        Parallel agents editing the same file overwrite each other, so
        ownership is checked before work is assigned rather than discovered
        afterwards in a confusing diff.
        """
        me = self.resolve(agent_id)
        clashes: list[str] = []
        for identity in self.all():
            if me is not None and identity.id == me.id:
                continue
            clashes.extend(path for path in files if path in identity.owns)
        return sorted(set(clashes))

    def summary(self, chinese: bool = False) -> str:
        """A short roster for display or for pinning into a prompt."""
        if not self._identities:
            return "没有其他 agent。" if chinese else "No other agents."
        lines = []
        for identity in self.all():
            mailbox = self._mailboxes[identity.id]
            suffix = f" · {mailbox.pending} unread" if mailbox.pending else ""
            lines.append(f"- {identity.describe()}{suffix}\n  {identity.brief}")
        return "\n".join(lines)


def _slugify(name: str) -> str:
    """Normalise a role name so agents can address each other reliably."""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in name.strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "agent"


@dataclass
class TeamMember:
    """One role in a project team, as configured or as planned."""

    role: str
    brief: str
    model: str = ""
    owns: list[str] = field(default_factory=list)


#: A default team for a code project. Roles are deliberately few — more roles
#: means more coordination overhead, and coordination is where multi-agent
#: setups usually lose the time they hoped to save.
DEFAULT_TEAM = [
    TeamMember(
        role="architect",
        brief=(
            "Decide the shape of the change and keep the parts consistent. "
            "Does not write implementation code; reviews interfaces between "
            "the other members' work."
        ),
    ),
    TeamMember(
        role="builder",
        brief="Implement the agreed design in the files assigned to you.",
    ),
    TeamMember(
        role="reviewer",
        brief=(
            "Read what the others actually wrote and find what is wrong with "
            "it. Report defects with path:line and a concrete trigger."
        ),
    ),
]
