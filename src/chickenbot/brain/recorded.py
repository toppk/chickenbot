"""A ring around every call to the model, whoever made it.

Wrapping the provider rather than the callers is the point: the chat path,
the bartender's daily pass, the room read and a barfly remark all funnel
through `reply`, and so will whatever gets written next. Recording at each
call site would mean every new one silently going unrecorded.

What asked for the call is not passed in. It does not need to be: every one
of them already runs inside an `activity(kind=...)` block, so the ambient
record names the reason, the room and the person.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..config import LLMConfig
from ..observe import current
from ..store import LlmCall
from . import Provider, Turn

if TYPE_CHECKING:
    from ..store import Store
    from ..tools import ToolBox

# A tool may answer with a whole readme. Keeping all of it in every row is not
# worth it, but a slice that does not say it is a slice reads as the whole
# answer -- the mistake the readme tool itself was careful not to make.
RESULT_SHOWN = 4000

log = logging.getLogger(__name__)


def why(fields: dict) -> str:
    """The reason, as the activity record already knows it.

    `message` plus `command=ask` is somebody asking; `follow` is a batch it
    was drawn into; `bartender`, `vibe` and `barfly` name themselves.
    """
    kind = str(fields.get("kind", "") or "?")
    detail = str(fields.get("command", "") or "")
    return f"{kind}/{detail}" if detail and detail != kind else kind


def _called(name: str, args: dict, out: object) -> dict[str, Any]:
    result = str(out)
    kept: dict[str, Any] = {"name": name, "args": args, "result": result[:RESULT_SHOWN]}
    if len(result) > RESULT_SHOWN:
        kept["of"] = len(result)  # so a slice is never mistaken for the whole
    return kept


class Recorded:
    """Delegates everything, keeps a copy of what went by."""

    def __init__(self, inner: Provider, store: Store, cfg: LLMConfig) -> None:
        self.inner = inner
        self.store = store
        # Read per call, not captured: otherwise `.tune llm.transcript 100`
        # would do nothing until a restart, which is the opposite of what a
        # knob you reach for while chasing something should do.
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.inner.name

    @property
    def supports_tools(self) -> bool:
        return bool(getattr(self.inner, "supports_tools", False))

    def __getattr__(self, item: str):
        return getattr(self.inner, item)

    async def reply(
        self,
        *,
        system: str,
        history: list[Turn],
        prompt: str,
        search: bool,
        toolbox: ToolBox | None = None,
        session: str = "",
    ) -> str:
        started = time.monotonic()
        answer = ""
        try:
            answer = await self.inner.reply(
                system=system, history=history, prompt=prompt, search=search, toolbox=toolbox, session=session
            )
            return answer
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
            answer = f"<{type(exc).__name__}: {exc}>"
            raise
        finally:
            # Never let bookkeeping lose an answer that was already produced.
            try:
                self._keep(system, history, prompt, answer, toolbox, started)
            except Exception:
                log.exception("could not record the model call")

    def _keep(self, system, history, prompt, answer, toolbox, started) -> None:
        keep, hours = self.cfg.transcript, self.cfg.transcript_hours
        if keep <= 0:
            return
        fields = record.fields if (record := current()) else {}
        turns = "\n".join(f"<{t.role}> {t.text}" for t in history)
        self.store.record_llm_call(
            LlmCall(
                reason=why(fields),
                realm=str(fields.get("realm", "")),
                room=str(fields.get("room", "")),
                nick=str(fields.get("nick", "")),
                request=f"----- system\n{system}\n----- history\n{turns}\n----- turn\n{prompt}",
                response=answer,
                model=str(fields.get("model", "")),
                served=str(fields.get("served", "")),
                # What it asked for and what came back, in the order it asked.
                # The turn is one row; this is what happened inside it.
                tool_calls=[_called(name, args, out) for name, args, out in getattr(toolbox, "log", [])],
                ms=int((time.monotonic() - started) * 1000),
            ),
            keep,
            hours,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()
