"""Chat tab rendering and controller logic for the job detail page.

Renders the "Chat" tab: the research-assistant conversation UI (styles,
scroll observer, message bubbles, typing indicator, sound effects) and the
`_ChatTabController` that loads chat history, submits new messages, and keeps
the transcript in sync. Consumed by `_render_job_tabs` in `job_detail.py`,
which remains the page-orchestration seam between timeline, report, and chat
tabs.
"""

import logging
from typing import Any
from uuid import UUID

from nicegui import ui

from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session_ctx
from openscientist.job.types import JobStatus
from openscientist.job_chat import get_chat_history, send_chat_message
from openscientist.webapp_components.pages.job_detail_context import _JobDetailContext
from openscientist.webapp_components.ui_components import render_thinking_status
from openscientist.webapp_components.utils import ClientGuard, safe_run_javascript

logger = logging.getLogger(__name__)

_CHAT_STYLES = """
<style>
    .chat-bubble-user {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 18px 18px 4px 18px;
        padding: 12px 16px;
        color: white;
        max-width: 85%;
        margin-left: auto;
        word-wrap: break-word;
        box-shadow: 0 2px 8px rgba(102, 126, 234, 0.3);
    }
    .chat-bubble-assistant {
        background: linear-gradient(135deg, #e0f2fe 0%, #cffafe 100%);
        border-radius: 18px 18px 18px 4px;
        padding: 12px 16px;
        color: #0c4a6e;
        max-width: 85%;
        word-wrap: break-word;
        box-shadow: 0 2px 8px rgba(14, 116, 144, 0.15);
        border: 1px solid #a5f3fc;
    }
    .chat-bubble-assistant .markdown-body {
        background: transparent !important;
    }
    .chat-container {
        background: linear-gradient(180deg, #fafbfc 0%, #f0f2f5 100%);
        border-radius: 12px;
        border: 1px solid #e1e4e8;
    }
    .chat-input-container {
        background: white;
        border-radius: 24px;
        border: 2px solid #e1e4e8;
        transition: border-color 0.2s, box-shadow 0.2s;
        min-height: 48px;
    }
    .chat-input-container:focus-within {
        border-color: #667eea;
        box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
    }
    .chat-input-row {
        align-items: center !important;
    }
    .chat-send-btn {
        width: 48px !important;
        height: 48px !important;
        min-width: 48px !important;
        min-height: 48px !important;
    }
</style>
"""


_CHAT_SCROLL_OBSERVER_SCRIPT = """
if (!window._chatScrollObserver) {
    window._chatScrollObserver = new MutationObserver(() => {
        const el = document.querySelector('.chat-messages-scroll');
        if (el && el.getBoundingClientRect().width > 0) {
            window._chatScrollObserver.disconnect();
            window._chatScrollObserver = null;
            const scroll = () => {
                const c = el.querySelector('.q-scrollarea__container');
                if (c) c.scrollTop = c.scrollHeight;
            };
            [50, 150, 300].forEach(ms => setTimeout(scroll, ms));
        }
    });
    window._chatScrollObserver.observe(document.body, {
        childList: true, subtree: true, attributes: true
    });
}
"""


_CHAT_HEADER_SVG = """
<svg viewBox="0 0 100 100" width="28" height="28" xmlns="http://www.w3.org/2000/svg">
    <path d="M22 18 Q50 18 50 40 Q50 60 78 60 Q78 82 50 82 Q22 82 22 60"
          fill="none" stroke="#0891b2" stroke-width="10" stroke-linecap="round"/>
    <circle cx="22" cy="18" r="10" fill="#06b6d4"/>
    <circle cx="78" cy="60" r="10" fill="#06b6d4"/>
    <circle cx="22" cy="60" r="10" fill="#0e7490"/>
</svg>
"""


_CHAT_AVATAR_HTML = """
<div style="width: 32px; height: 32px; background: #e0f2fe; border-radius: 50%; padding: 4px; flex-shrink: 0;">
    <svg viewBox="0 0 100 100" width="24" height="24" xmlns="http://www.w3.org/2000/svg">
        <path d="M22 18 Q50 18 50 40 Q50 60 78 60 Q78 82 50 82 Q22 82 22 60"
              fill="none" stroke="#0891b2" stroke-width="12" stroke-linecap="round"/>
        <circle cx="22" cy="18" r="10" fill="#06b6d4"/>
        <circle cx="78" cy="60" r="10" fill="#06b6d4"/>
        <circle cx="22" cy="60" r="10" fill="#0e7490"/>
    </svg>
</div>
"""


def _chat_sound_script(sound_type: str) -> str:
    return f"""
    (function() {{
        try {{
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const type = '{sound_type}';

            if (type === 'sound-send') {{
                const osc1 = ctx.createOscillator();
                const gain1 = ctx.createGain();
                osc1.connect(gain1);
                gain1.connect(ctx.destination);
                osc1.frequency.setValueAtTime(600, ctx.currentTime);
                osc1.frequency.exponentialRampToValueAtTime(900, ctx.currentTime + 0.08);
                osc1.type = 'sine';
                gain1.gain.setValueAtTime(0, ctx.currentTime);
                gain1.gain.linearRampToValueAtTime(0.15, ctx.currentTime + 0.02);
                gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.12);
                osc1.start(ctx.currentTime);
                osc1.stop(ctx.currentTime + 0.15);
            }} else if (type === 'sound-receive') {{
                const osc1 = ctx.createOscillator();
                const osc2 = ctx.createOscillator();
                const gain1 = ctx.createGain();
                const gain2 = ctx.createGain();
                osc1.connect(gain1);
                osc2.connect(gain2);
                gain1.connect(ctx.destination);
                gain2.connect(ctx.destination);
                osc1.type = 'sine';
                osc2.type = 'sine';
                osc1.frequency.value = 880;
                osc2.frequency.value = 1100;
                gain1.gain.setValueAtTime(0, ctx.currentTime);
                gain1.gain.linearRampToValueAtTime(0.12, ctx.currentTime + 0.02);
                gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
                gain2.gain.setValueAtTime(0, ctx.currentTime + 0.08);
                gain2.gain.linearRampToValueAtTime(0.1, ctx.currentTime + 0.1);
                gain2.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
                osc1.start(ctx.currentTime);
                osc2.start(ctx.currentTime + 0.08);
                osc1.stop(ctx.currentTime + 0.3);
                osc2.stop(ctx.currentTime + 0.35);
            }} else if (type === 'sound-error') {{
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.type = 'sine';
                osc.frequency.setValueAtTime(440, ctx.currentTime);
                osc.frequency.exponentialRampToValueAtTime(280, ctx.currentTime + 0.2);
                gain.gain.setValueAtTime(0, ctx.currentTime);
                gain.gain.linearRampToValueAtTime(0.12, ctx.currentTime + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
                osc.start(ctx.currentTime);
                osc.stop(ctx.currentTime + 0.3);
            }}
        }} catch(e) {{}}
    }})();
    """


class _ChatTabController:
    def __init__(self, context: _JobDetailContext) -> None:
        self.context = context
        self.job_uuid = UUID(context.job_id)
        self.chat_scroll: Any = None
        self.status_container: Any = None
        self.chat_input: Any = None
        self.send_btn: Any = None

    def render(self) -> None:
        if self.context.job_info.status != JobStatus.COMPLETED:
            ui.label("Chat will be available when the job completes.").classes(
                "text-gray-500 italic"
            )
            return

        ui.add_head_html(_CHAT_STYLES)
        self._render_shell()
        self.context.active_timers.append(ui.timer(0.1, self._render_messages, once=True))

        if self.context.can_edit:
            self._render_input_area()
            return
        ui.label("You have view-only access to this job.").classes(
            "text-sm text-gray-500 italic mt-4 text-center"
        )

    def _render_shell(self) -> None:
        with (
            ui.column()
            .classes("w-full max-w-4xl mx-auto chat-container p-4 flex flex-col flex-nowrap")
            .style("height: calc(100vh - 280px); min-height: 500px;")
        ):
            with ui.row().classes("w-full items-center gap-3 mb-4 pb-2 border-b"):
                ui.html(_CHAT_HEADER_SVG)
                ui.label("Research Assistant").classes("font-semibold text-gray-700")
                ui.label("Discuss your findings").classes("text-sm text-gray-500 ml-auto")

            self.chat_scroll = (
                ui.scroll_area()
                .classes("w-full flex-grow px-2 chat-messages-scroll")
                .style("min-height: 400px; max-height: calc(100vh - 350px);")
            )
            ui.run_javascript(_CHAT_SCROLL_OBSERVER_SCRIPT)

            self.status_container = ui.element("div").classes("hidden")
            with self.status_container:
                render_thinking_status("Analyzing your message...")

    def _play_sound(self, sound_type: str) -> None:
        safe_run_javascript(_chat_sound_script(sound_type))

    def _scroll_to_bottom(self) -> None:
        safe_run_javascript(
            """
            setTimeout(() => {
                const el = document.querySelector('.chat-messages-scroll .q-scrollarea__container');
                if (el) el.scrollTop = el.scrollHeight;
            }, 100);
            """
        )

    def _render_message_bubble(self, role: str, content: str) -> None:
        if role == "user":
            with (
                ui.row().classes("w-full justify-end mb-3"),
                ui.element("div").classes("chat-bubble-user"),
            ):
                ui.label(content).classes("text-sm")
            return

        with ui.row().classes("items-start gap-2 mb-3"):
            ui.html(_CHAT_AVATAR_HTML)
            with ui.element("div").classes("chat-bubble-assistant"):
                ui.markdown(content).classes("text-sm")

    def _render_empty_state(self) -> None:
        with ui.column().classes("w-full items-center py-8"):
            ui.icon("chat_bubble_outline", size="xl").classes("text-gray-300 mb-4")
            if self.context.can_edit:
                ui.label("Start a conversation").classes("text-lg font-medium text-gray-600")
                ui.label("Ask questions about your research findings").classes(
                    "text-sm text-gray-400 mb-4"
                )
                with ui.column().classes("gap-2"):
                    for suggestion in [
                        "What are the main findings?",
                        "How strong is the evidence?",
                        "What should I investigate next?",
                    ]:
                        ui.button(
                            suggestion,
                            on_click=lambda s=suggestion: self._quick_send(s),
                        ).props("flat dense").classes("text-indigo-600 normal-case")
                return
            ui.label("No messages yet").classes("text-lg font-medium text-gray-600")
            ui.label("You have view-only access to this job.").classes("text-sm text-gray-400")

    async def _load_chat_messages(self) -> list[Any]:
        async with get_session_ctx() as session:
            await set_current_user(session, UUID(self.context.user_id))
            return await get_chat_history(session, self.job_uuid)

    async def _render_messages(self) -> None:
        guard = ClientGuard()
        if not guard.is_connected or self.chat_scroll is None:
            return

        try:
            messages = await self._load_chat_messages()
            if not guard.is_connected:
                return

            self.chat_scroll.clear()
            with self.chat_scroll:
                if not messages:
                    self._render_empty_state()
                else:
                    for message in messages:
                        self._render_message_bubble(message.role, message.content)
            self._scroll_to_bottom()
        except Exception as exc:
            logger.error("Failed to load chat history: %s", exc)

    def _toggle_typing_indicator(self, visible: bool) -> None:
        if self.status_container is None:
            return
        if visible:
            self.status_container.classes(remove="hidden")
            return
        self.status_container.classes(add="hidden")

    def _read_input_message(self) -> str | None:
        if self.chat_input is None:
            return None
        message = (self.chat_input.value or "").strip()
        return message or None

    def _clear_input(self, guard: ClientGuard) -> None:
        if self.chat_input is None or self.send_btn is None:
            return
        self.chat_input.value = ""
        guard.run_javascript(
            "document.querySelector('textarea[placeholder=\"Ask about your research...\"]').value = ''"
        )
        self.send_btn.disable()

    async def _send_message_to_backend(self, message: str) -> None:
        async with get_session_ctx() as session:
            await set_current_user(session, UUID(self.context.user_id))
            await send_chat_message(session, self.job_uuid, message, self.context.job_dir)

    def _restore_input(self, guard: ClientGuard) -> None:
        if not guard.is_connected or self.send_btn is None or self.chat_input is None:
            return
        self.send_btn.enable()
        self.chat_input.run_method("focus")

    async def _send_message(self) -> None:
        guard = ClientGuard()
        if not guard.is_connected or self.chat_scroll is None:
            return

        message = self._read_input_message()
        if not message:
            return

        self._play_sound("sound-send")
        self._clear_input(guard)
        with self.chat_scroll:
            self._render_message_bubble("user", message)
        self._toggle_typing_indicator(True)
        self._scroll_to_bottom()

        try:
            await self._send_message_to_backend(message)
            if not guard.is_connected:
                return
            self._toggle_typing_indicator(False)
            self._play_sound("sound-receive")
            await self._render_messages()
        except Exception as exc:
            logger.error("Chat error: %s", exc, exc_info=True)
            if guard.is_connected:
                self._toggle_typing_indicator(False)
                self._play_sound("sound-error")
                ui.notify("An error occurred. Please try again.", type="negative")
        finally:
            self._restore_input(guard)

    async def _quick_send(self, message: str) -> None:
        if self.chat_input is None:
            return
        self.chat_input.value = message
        await self._send_message()

    def _render_input_area(self) -> None:
        with ui.row().classes("w-full max-w-3xl mx-auto gap-3 mt-4 chat-input-row"):
            with ui.element("div").classes("flex-grow chat-input-container flex items-center px-4"):
                self.chat_input = (
                    ui.textarea(placeholder="Ask about your research...")
                    .classes("flex-grow")
                    .props("borderless dense rows=1 autogrow input-class='text-sm py-3'")
                )

            self.send_btn = (
                ui.button(icon="send")
                .props("round color=indigo size=md")
                .classes("shadow-lg chat-send-btn")
            )

        self.send_btn.on_click(self._send_message)
        self.chat_input.on(
            "keydown.enter",
            lambda e: self._send_message() if not e.args.get("shiftKey") else None,
        )


def _render_chat_tab(context: _JobDetailContext) -> None:
    _ChatTabController(context).render()
