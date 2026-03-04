"""Page modules for the web application."""

# Import all page functions to register routes
from open_scientist.webapp_components.pages.admin import admin_page
from open_scientist.webapp_components.pages.api_keys import api_keys_page
from open_scientist.webapp_components.pages.docs import docs_page
from open_scientist.webapp_components.pages.index import index_page
from open_scientist.webapp_components.pages.job_detail import job_detail_page
from open_scientist.webapp_components.pages.jobs_list import jobs_page
from open_scientist.webapp_components.pages.login import login_page
from open_scientist.webapp_components.pages.mock_login import mock_login_form
from open_scientist.webapp_components.pages.new_job import new_job_page
from open_scientist.webapp_components.pages.skill_detail import skill_detail_page
from open_scientist.webapp_components.pages.skills_list import skills_page

__all__ = [
    "admin_page",
    "api_keys_page",
    "docs_page",
    "index_page",
    "job_detail_page",
    "jobs_page",
    "login_page",
    "mock_login_form",
    "new_job_page",
    "skill_detail_page",
    "skills_page",
]
