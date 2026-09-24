# -*- coding: utf-8 -*-
"""Refresh workspace skills before each agent build.

``PRE_AGENT_BUILD`` runs before ``AgentBuilder.build()``. When a skill's
frontmatter version changes, this hook drops the parsed-skill caches so
the build that follows reads the new ``SKILL.md``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import frontmatter

from qwenpaw.agents.skill_system.runtime_cache import _RUNTIME_SKILL_CACHE
from qwenpaw.agents.skill_system.store import (
    extract_version,
    get_workspace_skills_dir,
)
from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.plugins.api import PluginApi
from qwenpaw.runtime.hooks import HookResult
from qwenpaw.runtime.phases import Phase
from qwenpaw.utils.file_snapshot_cache import get_file_snapshot_cache

logger = logging.getLogger("qwenpaw.plugins.qwenpaw_skill_runtime")


class SkillVersionResolver:
    """Remember skill versions and invalidate caches when they change."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def resolve(self, workspace_dir: Path | str | None) -> list[str]:
        """Return skill names whose version changed since the last call."""
        if not workspace_dir:
            return []
        root = Path(workspace_dir).expanduser()
        skills_dir = get_workspace_skills_dir(root)
        current = _read_skill_versions(skills_dir)
        workspace_key = str(root)
        changed: list[tuple[str, str, str]] = []
        with self._lock:
            seen = {
                name for ws, name in self._versions if ws == workspace_key
            }
            for name, version in current.items():
                key = (workspace_key, name)
                previous = self._versions.get(key)
                if previous is not None and previous != version:
                    changed.append((name, previous, version))
                self._versions[key] = version
            for name in seen - set(current):
                previous = self._versions.pop((workspace_key, name), "")
                changed.append((name, previous, ""))

        if not changed:
            return []

        cache = get_file_snapshot_cache()
        for name, previous, version in changed:
            skill_md = skills_dir / name / "SKILL.md"
            if skill_md.is_file():
                cache.invalidate(skill_md)
            logger.info(
                "[skill-runtime] %s %s -> %s",
                name,
                previous or "(none)",
                version or "(removed)",
            )
        _RUNTIME_SKILL_CACHE.clear()
        return [name for name, _, _ in changed]


def _read_skill_versions(skills_dir: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    if not skills_dir.is_dir():
        return versions
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.is_file():
            continue
        try:
            post = frontmatter.load(skill_md)
            versions[child.name] = extract_version(post)
        except OSError as exc:
            logger.warning(
                "[skill-runtime] skip %s: %s",
                child.name,
                exc,
            )
    return versions


def _skill_bodies(skills_dir: Path) -> dict[str, str]:
    bodies: dict[str, str] = {}
    if not skills_dir.is_dir():
        return bodies
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.is_file():
            continue
        try:
            post = frontmatter.load(skill_md)
        except OSError:
            continue
        bodies[child.name] = str(post.content or "").strip()
    return bodies


def _iter_context_messages(session_state: Any) -> list[dict[str, Any]]:
    if not isinstance(session_state, dict):
        return []
    state = session_state.get("state")
    if isinstance(state, dict) and isinstance(state.get("context"), list):
        return [msg for msg in state["context"] if isinstance(msg, dict)]
    context = session_state.get("context")
    if isinstance(context, list):
        return [msg for msg in context if isinstance(msg, dict)]
    return []


def _skill_name_from_call(block: dict[str, Any]) -> str:
    raw = block.get("input")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    if isinstance(raw, dict):
        name = raw.get("skill") or raw.get("name") or ""
        return str(name)
    return ""


def _replace_tool_output(block: dict[str, Any], body: str) -> bool:
    output = block.get("output")
    if not isinstance(output, list):
        return False
    changed = False
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        if str(item.get("text") or "").strip() == body:
            continue
        item["text"] = body
        changed = True
    return changed


def rewrite_stale_skill_results(
    session_state: Any,
    workspace_dir: Path | str | None,
) -> list[str]:
    """Replace Skill tool results that still hold an older skill body."""
    if not workspace_dir:
        return []
    skills_dir = get_workspace_skills_dir(Path(workspace_dir))
    bodies = _skill_bodies(skills_dir)
    if not bodies:
        return []
    updated: list[str] = []
    for message in _iter_context_messages(session_state):
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        calls = {
            str(block.get("id")): _skill_name_from_call(block)
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_call"
        }
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if block.get("name") != "Skill":
                continue
            skill_name = calls.get(str(block.get("id")), "")
            if not skill_name:
                skill_name = next(
                    (
                        name
                        for name in calls.values()
                        if name in bodies
                    ),
                    "",
                )
            body = bodies.get(skill_name)
            if not body or not _replace_tool_output(block, body):
                continue
            updated.append(skill_name)
            skill_md = skills_dir / skill_name / "SKILL.md"
            if skill_md.is_file():
                get_file_snapshot_cache().invalidate(skill_md)
    if updated:
        _RUNTIME_SKILL_CACHE.clear()
    return list(dict.fromkeys(updated))


def _stale_skill_notice(workspace_dir: Path | str, names: list[str]) -> str:
    skills_dir = get_workspace_skills_dir(Path(workspace_dir))
    bodies = _skill_bodies(skills_dir)
    parts = [
        "以下技能刚刚更新。不要沿用本会话里更早的 Skill 工具结果或据此作出的回复。",
        "请重新调用 Skill 工具，并只按新正文回答。",
    ]
    for name in names:
        body = bodies.get(name, "")
        parts.append(f"技能 {name} 的当前正文：\n{body}")
    return "\n\n".join(parts)


_RESOLVER = SkillVersionResolver()


class SkillRuntimePreBuildHook(LifecycleHook):
    """Refresh skills after session state is loaded and before agent build."""

    phase = Phase.PRE_AGENT_BUILD
    name = "qwenpaw_skill_runtime_pre_build"
    # SessionLoadHook uses priority 10. Lower runs first, so this runs after
    # the old session has been loaded and can replace stale skill text.
    priority = 80

    async def run(self, ctx: Any) -> HookResult:
        workspace_dir = getattr(ctx, "workspace_dir", None)
        refreshed = _RESOLVER.resolve(workspace_dir)
        rewritten = rewrite_stale_skill_results(
            getattr(ctx, "session_state", None),
            workspace_dir,
        )
        names = list(dict.fromkeys([*refreshed, *rewritten]))
        if names:
            logger.info(
                "[skill-runtime] pre_agent_build session_id=%s refreshed=%s",
                getattr(ctx, "session_id", "") or "",
                ",".join(names),
            )
            inject = getattr(ctx, "inject_context", None)
            if callable(inject) and workspace_dir:
                inject(
                    _stale_skill_notice(workspace_dir, names),
                    source="qwenpaw-skill-runtime",
                    priority=1,
                )
        return HookResult()


class SkillRuntimePlugin:
    """Plugin entry point."""

    def register(self, api: PluginApi) -> None:
        api.register_runtime_hook(SkillRuntimePreBuildHook())


plugin = SkillRuntimePlugin()
