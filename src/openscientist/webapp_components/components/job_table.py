"""Job table slot templates and action buttons for the web app."""

from collections.abc import Callable

from nicegui import ui


def render_job_id_slot(field_name: str = "job_id") -> str:
    """Quasar slot template for a job ID column with clickable badges."""
    return f"""
        <q-td :props="props">
            <span
                class="job-id-badge"
                :data-job-id="props.row.{field_name}"
                :title="'View job ' + props.row.{field_name}"
                style="display:inline-flex;align-items:center;padding:2px 8px;background-color:#f3f4f6;border:1px solid #9ca3af;border-radius:4px;text-decoration:none;color:#374151;font-size:0.8em;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-weight:500;cursor:pointer;white-space:nowrap;"
                @click="$parent.$emit('view-job', props.row.{field_name})"
            >
                <svg width="14" height="14" style="display:inline !important;width:14px;height:14px;min-width:14px;min-height:14px;margin-right:4px;fill:currentColor;flex-shrink:0;" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
                    <path d="M20 6h-4V4c0-1.1-.9-2-2-2h-4c-1.1 0-2 .9-2 2v2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-6 0h-4V4h4v2z"/>
                </svg>
                {{{{ props.row.{field_name}.slice(-8) }}}}
            </span>
        </q-td>
    """


def render_status_cell_slot() -> str:
    """Quasar slot template for a status column with failed-job error tooltip."""
    return r"""
        <q-td :props="props">
            <div class="row items-center justify-center gap-1 flex-wrap">
                <!-- Failed status: Red badge with white text and error tooltip -->
                <q-badge
                    v-if="props.row.status === 'failed'"
                    color="red"
                    text-color="white"
                    class="px-1.5 py-0.5 font-bold cursor-pointer"
                >
                    <span class="row items-center" style="white-space:nowrap;">✗&nbsp;{{ props.row.status }}</span>
                    <q-tooltip
                        v-if="props.row.error"
                        class="bg-red-800 text-white text-sm"
                        max-width="400px"
                        anchor="top middle"
                        self="bottom middle"
                    >
                        <div class="font-bold mb-1">Error:</div>
                        <div>{{ props.row.error.substring(0, 150) }}{{ props.row.error.length > 150 ? '...' : '' }}</div>
                        <div class="text-xs mt-2 italic">Click "View" for details</div>
                    </q-tooltip>
                </q-badge>

                <!-- Completed status: Green badge -->
                <q-badge
                    v-else-if="props.row.status === 'completed'"
                    color="green"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">✓&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Running status: Teal badge -->
                <q-badge
                    v-else-if="props.row.status === 'running'"
                    color="teal"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">▶&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Queued status: Blue badge -->
                <q-badge
                    v-else-if="props.row.status === 'queued'"
                    color="blue"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">⟳&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Awaiting feedback: Orange badge -->
                <q-badge
                    v-else-if="props.row.status === 'awaiting_feedback'"
                    color="orange"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">⏸&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Generating report status: Teal badge -->
                <q-badge
                    v-else-if="props.row.status === 'generating_report'"
                    color="teal"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">⟳&nbsp;generating report</span>
                </q-badge>

                <!-- Cancelled status: Gray badge -->
                <q-badge
                    v-else-if="props.row.status === 'cancelled'"
                    color="grey"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">⊗&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Default: Gray badge -->
                <q-badge
                    v-else
                    color="grey"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">○&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Public: teal badge when the owner has opened the job up -->
                <q-badge
                    v-if="props.row.is_public"
                    color="teal"
                    class="px-1.5 py-0.5"
                >
                    <span class="row items-center" style="white-space:nowrap;">◎&nbsp;public</span>
                </q-badge>
            </div>
        </q-td>
    """


def render_cost_slot() -> str:
    """Quasar slot template for the Cost column, one badge per row."""
    return r"""
        <q-td :props="props">
            <q-badge color="green" text-color="white" class="px-1.5 py-0.5">
                {{ props.row.cost }}
            </q-badge>
        </q-td>
    """


def render_actions_slot_with_delete() -> str:
    """Quasar slot template for the actions column with share and delete buttons.

    View is handled by the job ID badge and notifications on the job detail page.
    """
    return r"""
        <q-td :props="props">
            <div class="row items-center gap-1 justify-center flex-wrap">
                <!-- Share button - conditionally shown based on can_share (owners only) -->
                <q-btn
                    v-if="props.row.can_share"
                    round
                    flat
                    dense
                    size="sm"
                    color="primary"
                    icon="share"
                    @click="$parent.$emit('share-job', props.row.job_id)"
                >
                    <q-tooltip>Share job</q-tooltip>
                </q-btn>

                <!-- Delete button - conditionally shown based on can_delete -->
                <q-btn
                    v-if="props.row.can_delete"
                    round
                    flat
                    dense
                    size="sm"
                    color="negative"
                    icon="delete"
                    @click="$parent.$emit('delete-job', props.row.job_id)"
                >
                    <q-tooltip>Delete job</q-tooltip>
                </q-btn>
            </div>
        </q-td>
    """


def render_job_action_buttons(
    on_share: Callable[[], None] | None = None,
    on_delete: Callable[[], None] | None = None,
    on_notifications: Callable[[], None] | None = None,
) -> None:
    """Render job action buttons (share, delete, notifications) in table-action style.

    Each button is hidden when its callback is None.
    """
    with ui.row().classes("gap-1 items-center"):
        if on_notifications:
            with ui.button(icon="notifications", on_click=on_notifications).props(
                "round flat dense size=sm color=primary"
            ):
                ui.tooltip("Configure push notifications")

        if on_share:
            with ui.button(icon="share", on_click=on_share).props(
                "round flat dense size=sm color=primary"
            ):
                ui.tooltip("Share job")

        if on_delete:
            with ui.button(icon="delete", on_click=on_delete).props(
                "round flat dense size=sm color=negative"
            ):
                ui.tooltip("Delete job")
