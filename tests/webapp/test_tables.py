"""Tests for table UI components module."""

from openscientist.webapp_components.components.tables import (
    make_action_button_slot,
    render_actions_slot_with_delete,
    render_skill_name_slot,
)


class TestRenderSkillNameSlot:
    """Tests for render_skill_name_slot function."""

    def test_returns_string(self):
        """Test that function returns a string template."""
        template = render_skill_name_slot()
        assert isinstance(template, str)

    def test_contains_quasar_cell(self):
        """Test that template contains the Quasar cell wrapper."""
        template = render_skill_name_slot()
        assert "<q-td" in template
        assert "props.row.name" in template

    def test_emits_view_skill_with_category_and_slug(self):
        """Test that clicking the name emits view-skill with category/slug payload."""
        template = render_skill_name_slot()
        assert "$parent.$emit('view-skill'" in template
        assert "category: props.row.category" in template
        assert "slug: props.row.slug" in template

    def test_description_is_conditionally_rendered(self):
        """Test that the description line is guarded by a v-if and interpolated."""
        template = render_skill_name_slot()
        assert 'v-if="props.row.description"' in template
        assert "{{ props.row.description }}" in template


class TestRenderActionsSlotWithDelete:
    """Tests for render_actions_slot_with_delete function."""

    def test_returns_string(self):
        """Test that function returns a string template."""
        template = render_actions_slot_with_delete()
        assert isinstance(template, str)

    def test_contains_quasar_cell(self):
        """Test that template contains the Quasar cell wrapper."""
        template = render_actions_slot_with_delete()
        assert "<q-td" in template

    def test_share_button_conditionally_rendered(self):
        """Test that the share button is guarded by can_share and emits share-job."""
        template = render_actions_slot_with_delete()
        assert 'v-if="props.row.can_share"' in template
        assert "$parent.$emit('share-job', props.row.job_id)" in template
        assert "Share job" in template

    def test_delete_button_conditionally_rendered(self):
        """Test that the delete button is guarded by can_delete and emits delete-job."""
        template = render_actions_slot_with_delete()
        assert 'v-if="props.row.can_delete"' in template
        assert "$parent.$emit('delete-job', props.row.job_id)" in template
        assert "Delete job" in template


class TestMakeActionButtonSlot:
    """Tests for make_action_button_slot function."""

    def test_returns_string(self):
        """Test that function returns a string template."""
        template = make_action_button_slot(label="Assign", event_name="assign")
        assert isinstance(template, str)

    def test_contains_quasar_cell(self):
        """Test that template contains the Quasar cell wrapper."""
        template = make_action_button_slot(label="Assign", event_name="assign")
        assert "<q-td" in template

    def test_label_and_color_are_interpolated(self):
        """Test that the label and color are correctly substituted."""
        template = make_action_button_slot(label="Assign", event_name="assign", color="negative")
        assert 'label="Assign"' in template
        assert 'color="negative"' in template

    def test_default_color_is_primary(self):
        """Test that color defaults to primary when not specified."""
        template = make_action_button_slot(label="Assign", event_name="assign")
        assert 'color="primary"' in template

    def test_icon_omitted_when_not_provided(self):
        """Test that no icon attribute is rendered when icon is None."""
        template = make_action_button_slot(label="Assign", event_name="assign")
        assert "icon=" not in template

    def test_icon_included_when_provided(self):
        """Test that the icon attribute is rendered when icon is provided."""
        template = make_action_button_slot(label="Assign", event_name="assign", icon="person_add")
        assert 'icon="person_add"' in template

    def test_event_name_used_in_emit(self):
        """Test that the event_name is used in the emitted click event."""
        template = make_action_button_slot(label="Assign", event_name="assign")
        assert "$parent.$emit('assign', props.row)" in template
