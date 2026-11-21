"""prompt_toolkit Application with a navigation stack and breadcrumb.

Architecture: screens implement the :class:`Screen` protocol. A :class:`NavStack`
holds the active screens; the top screen is always the one rendered. The
``Application`` body reads ``nav.current.render()`` and its key bindings are the
merge of a global ``KeyBindings`` and ``DynamicKeyBindings`` that read
``nav.current.get_key_bindings()`` so the active screen's keys swap in
automatically when the stack changes.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import (
    DynamicKeyBindings,
    KeyBindings,
    merge_key_bindings,
)
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from cursor_chat_recovery import paths


@dataclass
class AppState:
    readonly: bool
    global_db: Path
    workspace_storage: Path
    workspaces_config: Path | None
    sync_load: bool = False


@runtime_checkable
class Screen(Protocol):
    """A single TUI screen pushed onto the navigation stack."""

    def title(self) -> str:
        """Breadcrumb segment for this screen."""
        ...

    def render(self) -> list[tuple[str, str]]:
        """Formatted-text fragments for the body."""
        ...

    def get_key_bindings(self) -> KeyBindings:
        """Screen-specific key bindings."""
        ...

    def footer_hints(self) -> str:
        """Key-hint line for the footer, e.g. '[enter] open  [s] sort'."""
        ...


class NavStack:
    """Holds the stack of screens; the top is the active screen."""

    def __init__(self, root: Screen) -> None:
        self.stack: list[Screen] = [root]

    @property
    def current(self) -> Screen:
        return self.stack[-1]

    def push(self, screen: Screen) -> None:
        self.stack.append(screen)

    def pop(self) -> Screen | None:
        if len(self.stack) > 1:
            return self.stack.pop()
        return None


# Module-level reference to the active nav stack so dialog screens can pop
# themselves without each carrying a back-reference. Set by ``run_tui``.
_active_nav: NavStack | None = None


def get_nav() -> NavStack | None:
    """Return the active navigation stack (or None outside a running app)."""
    return _active_nav


_STYLE = Style.from_dict({
    "breadcrumb": "bg:#222222 #ffffff bold",
    "footer": "bg:#222222 #aaaaaa",
    "header": "bold",
    "row": "",
    "row-selected": "reverse",
    "row-dim": "#666666",
    "role-user": "bold #5fafff",
    "role-assistant": "bold #5fd75f",
    "role-system": "bold #aaaaaa",
    "role-tool": "bold #d7af5f",
    "role-unknown": "bold",
})


def _build_application(
    state: AppState,
    nav: NavStack,
    inp: Any = None,
    output: Any = None,
) -> Application[int]:
    def breadcrumb_text() -> list[tuple[str, str]]:
        ro = " [READ-ONLY]" if state.readonly else ""
        crumb = " › ".join(s.title() for s in nav.stack)
        return [("class:breadcrumb", crumb + ro)]

    breadcrumb_window = Window(
        content=FormattedTextControl(breadcrumb_text), height=1
    )
    from prompt_toolkit.filters import Condition

    body = Window(
        content=FormattedTextControl(lambda: nav.current.render(), focusable=True),
        # Screens opt into soft-wrapping (e.g. chat messages) via a
        # ``wrap_lines = True`` attribute; tabular lists keep truncation.
        wrap_lines=Condition(
            lambda: bool(getattr(nav.current, "wrap_lines", False))
        ),
    )
    footer = Window(
        content=FormattedTextControl(
            lambda: [("class:footer", f" {nav.current.footer_hints()}  [?] help  [q] quit ")]
        ),
        height=1,
    )
    layout = Layout(HSplit([breadcrumb_window, body, footer]))

    def _typing() -> bool:
        return bool(getattr(nav.current, "wants_text_input", lambda: False)())

    not_typing = Condition(lambda: not _typing())

    global_kb = KeyBindings()

    @global_kb.add("q", filter=not_typing)
    def _(event: Any) -> None:
        event.app.exit(result=0)

    @global_kb.add("c-c")
    def _(event: Any) -> None:
        event.app.exit(result=130)

    @global_kb.add("escape", eager=True)
    def _(event: Any) -> None:
        handler = getattr(nav.current, "handle_escape", None)
        if handler is not None and handler():
            return
        nav.pop()

    @global_kb.add("?", filter=not_typing)
    def _(event: Any) -> None:
        from cursor_chat_recovery.tui import dialogs

        if not isinstance(nav.current, dialogs.HelpScreen):
            nav.push(dialogs.HelpScreen(state))

    key_bindings = merge_key_bindings([
        global_kb,
        DynamicKeyBindings(lambda: nav.current.get_key_bindings()),
    ])

    return Application(
        layout=layout,
        key_bindings=key_bindings,
        style=_STYLE,
        full_screen=True,
        input=inp,
        output=output,
        refresh_interval=0.1,
    )


def run_tui(
    readonly: bool = False,
    global_db: Path | None = None,
    workspace_storage: Path | None = None,
    workspaces_config: Path | None = None,
    input: Any = None,  # noqa: A002
    output: Any = None,
    cursor_running_check: Callable[[], bool] | None = None,
    sync_load: bool = False,
) -> int:
    global _active_nav
    if global_db is None or workspace_storage is None:
        loc = paths.locate_cursor_dirs()
        global_db = global_db or loc.global_storage_db
        workspace_storage = workspace_storage or loc.workspace_storage_dir
        workspaces_config = workspaces_config or loc.workspaces_dir

    state = AppState(
        readonly=readonly,
        global_db=Path(global_db),
        workspace_storage=Path(workspace_storage),
        workspaces_config=Path(workspaces_config) if workspaces_config else None,
        sync_load=sync_load,
    )

    # Imported here to avoid a circular import (screen modules import from app).
    from cursor_chat_recovery import schema, storage
    from cursor_chat_recovery.tui import actions, dialogs
    from cursor_chat_recovery.tui.screen_workspaces import WorkspacesScreen

    check = cursor_running_check or storage._default_cursor_running_check

    def on_export(ids: list[str]) -> None:
        chats_depth = len(nav.stack)
        default_dir = str(Path.home() / ".cursor-chat-recovery" / "exports")

        def do_export(dir_str: str) -> None:
            while len(nav.stack) > chats_depth:
                nav.pop()
            paths = actions.export_chats(
                state.global_db, ids, Path(dir_str), fmt="markdown"
            )
            nav.push(
                dialogs.ResultScreen(
                    state,
                    [f"Exported {len(paths)} file(s) to {dir_str}"]
                    + [str(p) for p in paths],
                )
            )

        nav.push(
            dialogs.ExportPathScreen(
                state, default_dir=default_dir, on_submit=do_export
            )
        )

    def open_chat(chat_header: Any) -> None:
        from cursor_chat_recovery.tui.screen_messages import MessagesScreen

        nav.push(
            MessagesScreen(
                state,
                chat_header.composer_id,
                chat_header.name,
                on_export=on_export,
            )
        )

    def on_reassign(ids: list[str], source_ws: Any) -> None:
        if state.readonly:
            nav.push(
                dialogs.ResultScreen(
                    state, ["Read-only mode: mutations disabled."]
                )
            )
            return

        with storage.Storage.open_readonly(state.global_db) as s:
            from cursor_chat_recovery import operations

            workspaces = operations.list_workspaces(
                s, state.workspace_storage, state.workspaces_config
            )

        chats_depth = len(nav.stack)

        def on_pick(target_ws: Any) -> None:
            nav.pop()  # remove the pick screen

            def on_yes() -> None:
                backup_dir = Path.home() / ".cursor-chat-recovery" / "backups"
                # Unwind back to the chats screen before showing the result.
                while len(nav.stack) > chats_depth:
                    nav.pop()
                try:
                    res = actions.perform_reassign(
                        state.global_db,
                        backup_dir,
                        ids,
                        target_ws.identifier.id,
                        cursor_running_check=check,
                    )
                except storage.CursorRunning as e:
                    nav.push(dialogs.ResultScreen(state, [f"Move failed: {e}"]))
                    return
                # Refresh the chats screen so the moved chats disappear from it.
                reload = getattr(nav.current, "reload", None)
                if reload is not None:
                    reload()
                result_lines = [
                    f"Moved {len(res.composer_ids)} chat(s) to {target_ws.display_name}."
                ]
                if res.backup_path:
                    result_lines.append(f"Backup: {res.backup_path}")
                nav.push(dialogs.ResultScreen(state, result_lines))

            target_path = target_ws.identifier.uri or target_ws.identifier.id
            nav.push(
                dialogs.ConfirmScreen(
                    state,
                    f'Move {len(ids)} chat(s) to "{target_ws.display_name}" '
                    f"({target_path})?\nA backup will be written.",
                    on_yes=on_yes,
                )
            )

        nav.push(dialogs.PickTargetScreen(
            state, workspaces, on_pick=on_pick,
            source=source_ws, chat_count=len(ids),
        ))

    def open_workspace(workspace: Any) -> None:
        from cursor_chat_recovery.tui.screen_chats import ChatsScreen

        nav.push(
            ChatsScreen(
                state,
                workspace,
                on_open=open_chat,
                on_reassign=on_reassign,
                on_export=on_export,
            )
        )

    root: Screen
    with storage.Storage.open_readonly(state.global_db) as s:
        report = schema.detect_mismatch(s.connection)
    if not report.ok:
        state.readonly = True
        root = dialogs.SchemaMismatchScreen(state, schema.agent_prompt(report))
    else:
        root = WorkspacesScreen(state, on_open=open_workspace)

    nav = NavStack(root)
    _active_nav = nav
    app = _build_application(state, nav, inp=input, output=output)
    result = app.run()
    return result if isinstance(result, int) else 0
