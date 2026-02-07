"""Login page."""

from nicegui import app, ui

from shandy.webapp_components.utils.auth import check_password


@ui.page("/login")
def login_page():
    """Login page"""

    def try_login():
        if check_password(password_input.value):
            app.storage.user["authenticated"] = True
            ui.navigate.to("/")
        else:
            ui.notify("Invalid password", color="negative")
            password_input.value = ""

    with ui.column().classes("absolute-center items-center"):
        ui.markdown("# SHANDY")
        ui.markdown("_Scientific Hypothesis Agent for Novel Discovery_")
        password_input = (
            ui.input("Password", password=True, password_toggle_button=True)
            .classes("w-64")
            .on("keydown.enter", try_login)
        )
        ui.button("Login", on_click=try_login).classes("w-64")
