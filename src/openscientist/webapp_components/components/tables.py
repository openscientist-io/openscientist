"""
Table UI components for OpenScientist web interface.

Provides Quasar table slot-template generators used with NiceGUI's
table.add_slot() for rendering custom cell content (links, action buttons).
"""


def render_skill_name_slot() -> str:
    """
    Generate Quasar table slot template for skill name column with clickable link.

    Returns slot template string that renders skill names as clickable links
    navigating to the skill detail page.

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <span
                class="skill-name-link"
                style="color:#0891b2;cursor:pointer;font-weight:500;"
                @click="$parent.$emit('view-skill', {category: props.row.category, slug: props.row.slug})"
            >
                {{ props.row.name }}
            </span>
            <div v-if="props.row.description" class="text-caption text-grey-7" style="max-width:400px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                {{ props.row.description }}
            </div>
        </q-td>
    """


def render_actions_slot_with_delete() -> str:
    """
    Generate Quasar table slot template for actions column with share and delete.

    Returns slot template string with:
    - Share icon button (conditionally shown via v-if="props.row.can_share") - uses share icon
    - Delete icon button (conditionally shown via v-if="props.row.can_delete") - uses delete icon
    - All buttons use round style for a compact, badge-like appearance
    - Tooltips for clarity

    Note: View functionality is handled by clicking the job ID badge.
    Note: Notifications are configured on the job detail page.

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <div class="row items-center gap-1 justify-center">
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


def make_action_button_slot(
    label: str,
    event_name: str,
    icon: str | None = None,
    color: str = "primary",
) -> str:
    """
    Generate a Quasar table slot template for an action button.

    Creates an HTML template string for use with NiceGUI's table.add_slot().
    The button emits an event with the row data when clicked.

    Args:
        label: Button label text
        event_name: Event name emitted when button is clicked
        icon: Optional Material icon name (e.g., "person_add")
        color: Quasar color for the button

    Returns:
        Quasar slot template string

    Example:
        table.add_slot("body-cell-actions", make_action_button_slot(
            label="Assign",
            event_name="assign",
            icon="person_add",
        ))
        table.on("assign", handle_assign)
    """
    icon_attr = f'icon="{icon}"' if icon else ""
    return f"""
<q-td :props="props">
    <q-btn
        size="sm"
        color="{color}"
        {icon_attr}
        label="{label}"
        @click="$parent.$emit('{event_name}', props.row)"
    />
</q-td>
"""
