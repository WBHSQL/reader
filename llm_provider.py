"""LLM provider adapters for the personal agent MVP."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import json
import tempfile
import threading


class LlmProviderError(RuntimeError):
    pass


def _creationflags() -> int:
    """Keep background CLI providers from opening console windows on Windows."""
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


class ClaudeCliProvider:
    """Use the user's existing Claude Code provider configuration."""

    def __init__(self, command: str | Path | None = None, *, timeout_seconds: int = 180) -> None:
        self.command = str(command or self._discover())
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _discover() -> Path:
        found = shutil.which("claude")
        if found:
            return Path(found)
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidate = Path(appdata) / "npm" / "claude.cmd"
            if candidate.exists():
                return candidate
        raise LlmProviderError("Claude Code CLI not found")

    def answer(self, prompt: str) -> str:
        if not prompt.strip():
            raise LlmProviderError("prompt is empty")
        try:
            completed = subprocess.run(
                [
                    self.command,
                    "--bare",
                    "--no-session-persistence",
                    "--tools",
                    "",
                    "-p",
                ],
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
                creationflags=_creationflags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LlmProviderError("Claude CLI invocation failed") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-500:]
            raise LlmProviderError(f"Claude CLI failed: {detail}")
        answer = (completed.stdout or "").strip()
        if not answer:
            raise LlmProviderError("Claude CLI returned an empty answer")
        return answer


class HermesCliProvider:
    """Use Hermes Agent with the user's ChatGPT/Codex subscription runtime."""

    def __init__(self, command: str | Path | None = None, *, model: str = "gpt-5.6-luna", effort: str = "low", timeout_seconds: int = 180) -> None:
        self.command = str(command or self._discover())
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _discover() -> Path:
        found = shutil.which("hermes")
        if found:
            return Path(found)
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "bin" / "hermes.exe"
        if local.exists():
            return local
        raise LlmProviderError("Hermes CLI not found")

    def answer(self, prompt: str) -> str:
        if not prompt.strip():
            raise LlmProviderError("prompt is empty")
        args = [self.command, "chat", "--query-file", "-", "--oneshot", "-Q",
                "-m", self.model, "--provider", "openai-codex",
                "--reasoning", self.effort, "--source", "tool", "--ignore-rules"]
        env = os.environ.copy()
        extras = []
        if env.get("LOCALAPPDATA"):
            extras.append(str(Path(env["LOCALAPPDATA"]) / "hermes" / "bin"))
        if env.get("APPDATA"):
            extras.append(str(Path(env["APPDATA"]) / "npm"))
        env["PATH"] = os.pathsep.join(extras + [env.get("PATH", "")])
        try:
            completed = subprocess.run(args, input=prompt, text=True, encoding="utf-8", errors="replace",
                                       capture_output=True, timeout=self.timeout_seconds, check=False, env=env,
                                       creationflags=_creationflags())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LlmProviderError("Hermes CLI invocation failed") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-800:]
            raise LlmProviderError(f"Hermes CLI failed: {detail}")
        lines = [line for line in (completed.stdout or "").splitlines()
                 if not line.strip().lower().startswith("session_id:")]
        answer = "\n".join(lines).strip()
        if not answer:
            raise LlmProviderError("Hermes CLI returned an empty answer")
        return answer


_CODEX_SESSION_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_CODEX_SESSION_LOCKS_GUARD = threading.Lock()


def _codex_session_lock(path: Path, key: str) -> threading.RLock:
    ident = (str(path.resolve()), key)
    with _CODEX_SESSION_LOCKS_GUARD:
        return _CODEX_SESSION_LOCKS.setdefault(ident, threading.RLock())


class CodexCliProvider:
    """Use Codex CLI as a text reasoning provider, optionally on one persistent thread."""

    def __init__(
        self, command: str | Path | None = None, *, model: str = "gpt-5.6-luna",
        effort: str = "low", timeout_seconds: int = 120, web_search: bool = False,
        session_key: str | None = None, session_store: str | Path | None = None,
        session_bootstrap: str = "",
    ) -> None:
        self.command = str(command or self._discover())
        self.model = model
        self.effort = effort
        self.timeout_seconds = timeout_seconds
        self.web_search = bool(web_search)
        self.workdir = Path(__file__).resolve().parents[1]
        self.session_key = str(session_key or "").strip() or None
        self.session_store = Path(session_store) if session_store else None
        self.session_bootstrap = session_bootstrap.strip()
        if self.session_key and self.session_store is None:
            raise ValueError("session_store is required when session_key is set")

    @staticmethod
    def _discover() -> Path:
        found = shutil.which("codex")
        if found:
            return Path(found)
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        matches = sorted(local.glob("*/codex.exe"), key=lambda value: value.stat().st_mtime, reverse=True) if local.exists() else []
        if matches:
            return matches[0]
        raise LlmProviderError("Codex CLI not found")

    def _load_session_id(self) -> str:
        if not self.session_key or not self.session_store or not self.session_store.exists():
            return ""
        try:
            data = json.loads(self.session_store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get(self.session_key, "")).strip()

    def _save_session_id(self, thread_id: str) -> None:
        assert self.session_key and self.session_store
        self.session_store.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, str] = {}
        if self.session_store.exists():
            try:
                loaded = json.loads(self.session_store.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = {str(k): str(v) for k, v in loaded.items()}
            except (OSError, json.JSONDecodeError):
                pass
        data[self.session_key] = thread_id
        fd, name = tempfile.mkstemp(prefix=f".{self.session_store.name}.", suffix=".tmp", dir=self.session_store.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, self.session_store)
        finally:
            try: tmp.unlink(missing_ok=True)
            except OSError: pass

    @staticmethod
    def _thread_id_from_jsonl(text: str) -> str:
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started":
                return str(event.get("thread_id", "")).strip()
        return ""

    def _run_persistent(self, prompt: str) -> str:
        assert self.session_key and self.session_store
        lock = _codex_session_lock(self.session_store, self.session_key)
        with lock:
            session_id = self._load_session_id()
            fd, output_name = tempfile.mkstemp(prefix="reader-codex-answer-", suffix=".txt")
            os.close(fd)
            output_path = Path(output_name)
            try:
                if session_id:
                    args = [self.command]
                    if self.web_search:
                        args.append("--search")
                    args += [
                        "exec", "resume", session_id, "-m", self.model,
                        "-c", f'model_reasoning_effort="{self.effort}"',
                        "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                        "--json", "-o", str(output_path), "-",
                    ]
                    message = "新任务。沿用 Reader 的稳定工作规则，但只用本条消息中的任务材料回答；不要把上一篇文章的事实带进来。\n\n" + prompt
                else:
                    args = [self.command]
                    if self.web_search:
                        args.append("--search")
                    args += [
                        "exec", "-m", self.model,
                        "-c", f'model_reasoning_effort="{self.effort}"',
                        "-s", "read-only", "--skip-git-repo-check",
                        "--ignore-user-config", "--ignore-rules", "--json",
                        "-o", str(output_path), "-",
                    ]
                    message = ((self.session_bootstrap + "\n\n") if self.session_bootstrap else "") + prompt
                completed = subprocess.run(
                    args, input=message, text=True, encoding="utf-8", errors="replace",
                    capture_output=True, timeout=self.timeout_seconds, check=False,
                    cwd=str(self.workdir), creationflags=_creationflags(),
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "").strip()[-500:]
                    raise LlmProviderError(f"Codex CLI failed: {detail}")
                returned_id = self._thread_id_from_jsonl(completed.stdout or "")
                if not returned_id:
                    raise LlmProviderError("Codex CLI did not return a thread id")
                if session_id and returned_id != session_id:
                    raise LlmProviderError("Codex resume returned a different thread; refusing to create hidden chat spam")
                if not session_id:
                    self._save_session_id(returned_id)
                answer = output_path.read_text(encoding="utf-8", errors="replace").strip() if output_path.exists() else ""
                if not answer:
                    raise LlmProviderError("Codex CLI returned an empty answer")
                return answer
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LlmProviderError("Codex CLI invocation failed") from exc
            finally:
                try: output_path.unlink(missing_ok=True)
                except OSError: pass

    def answer(self, prompt: str) -> str:
        if not prompt.strip():
            raise LlmProviderError("prompt is empty")
        if self.session_key:
            return self._run_persistent(prompt)
        args = [self.command]
        if self.web_search:
            args.append("--search")
        args += [
            "exec", "-m", self.model,
            "-c", f'model_reasoning_effort="{self.effort}"',
            "-s", "read-only",
            "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules", "-",
        ]
        try:
            completed = subprocess.run(args, input=prompt, text=True, encoding="utf-8", errors="replace",
                                       capture_output=True, timeout=self.timeout_seconds, check=False,
                                       cwd=str(self.workdir), creationflags=_creationflags())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LlmProviderError("Codex CLI invocation failed") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-500:]
            raise LlmProviderError(f"Codex CLI failed: {detail}")
        answer = (completed.stdout or "").strip()
        if not answer:
            raise LlmProviderError("Codex CLI returned an empty answer")
        return answer

