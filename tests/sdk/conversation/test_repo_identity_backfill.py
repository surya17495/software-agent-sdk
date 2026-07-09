"""Live repo-identity backfill into observability trace metadata."""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from pydantic import SecretStr

from openhands.sdk.agent import Agent
from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.llm import LLM


def _make_conversation(workspace: str) -> LocalConversation:
    llm = LLM(model="gpt-4o-mini", api_key=SecretStr("k"), usage_id="test")
    return LocalConversation(agent=Agent(llm=llm, tools=[]), workspace=workspace)


def _init_repo_with_origin(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "remote",
            "add",
            "origin",
            "https://github.com/OpenHands/OpenHands.git",
        ],
        check=True,
    )
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _drain_probe(conv: LocalConversation) -> None:
    """Run the probe synchronously by invoking the worker directly."""
    conv._probe_repo_identity_worker()


def test_probe_backfills_repo_identity():
    with tempfile.TemporaryDirectory() as tmp:
        # Repo cloned into a subdir of the workspace base (clone-later flow).
        ws = Path(tmp) / "ws"
        (ws / "OpenHands").mkdir(parents=True)
        _init_repo_with_origin(ws / "OpenHands")

        conv = _make_conversation(str(ws))
        conv._observability_root_span = MagicMock()
        conv._update_observability_metadata = MagicMock()  # type: ignore[method-assign]

        _drain_probe(conv)

        conv._update_observability_metadata.assert_called_once()
        identity = conv._update_observability_metadata.call_args.args[0]
        assert identity["repo"] == "OpenHands/OpenHands"
        assert identity["git_provider"] == "github"
        assert conv._repo_identity == identity
        conv.close()


def test_probe_is_noop_without_root_span():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir(parents=True)
        conv = _make_conversation(str(ws))
        conv._observability_root_span = None
        conv._update_observability_metadata = MagicMock()  # type: ignore[method-assign]

        # Scheduler must not spawn a probe when observability is off.
        conv._maybe_probe_repo_identity()
        conv._update_observability_metadata.assert_not_called()
        conv.close()


def test_probe_debounces_repeat_calls():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir(parents=True)
        conv = _make_conversation(str(ws))
        conv._observability_root_span = MagicMock()
        spawned = []
        conv._probe_repo_identity_worker = lambda: spawned.append(1)  # type: ignore[method-assign]

        conv._maybe_probe_repo_identity()
        conv._maybe_probe_repo_identity()  # within the debounce window
        # Give any spawned threads a moment.
        import time

        time.sleep(0.05)
        assert len(spawned) == 1
        conv.close()


def test_close_does_final_probe():
    """A repo cloned late (no event to trigger a re-probe) is captured at close."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        (ws / "repo").mkdir(parents=True)
        _init_repo_with_origin(ws / "repo")

        conv = _make_conversation(str(ws))
        conv._observability_root_span = MagicMock()
        updates = []
        conv._update_observability_metadata = lambda md: updates.append(md)  # type: ignore[method-assign]

        conv.close()

        assert updates and updates[-1]["repo"] == "OpenHands/OpenHands"


def test_probe_does_not_update_when_identity_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        (ws / "repo").mkdir(parents=True)
        _init_repo_with_origin(ws / "repo")

        conv = _make_conversation(str(ws))
        conv._observability_root_span = MagicMock()
        conv._update_observability_metadata = MagicMock()  # type: ignore[method-assign]

        _drain_probe(conv)
        _drain_probe(conv)  # identity unchanged the second time

        conv._update_observability_metadata.assert_called_once()
        conv.close()
