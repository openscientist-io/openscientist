"""Page modules for the web application."""

# Import all page functions to register routes
from shandy.webapp_components.pages.admin import admin_page
from shandy.webapp_components.pages.api_keys import api_keys_page
from shandy.webapp_components.pages.billing import billing_page
from shandy.webapp_components.pages.docs import docs_page
from shandy.webapp_components.pages.index import index_page
from shandy.webapp_components.pages.job_detail import job_detail_page
from shandy.webapp_components.pages.jobs_list import jobs_page
from shandy.webapp_components.pages.login import login_page
from shandy.webapp_components.pages.mock_login import mock_login_form
from shandy.webapp_components.pages.new_job import new_job_page
from shandy.webapp_components.pages.skill_detail import skill_detail_page
from shandy.webapp_components.pages.skills_list import skills_page

__all__ = [
    "login_page",
    "mock_login_form",
    "index_page",
    "new_job_page",
    "jobs_page",
    "job_detail_page",
    "billing_page",
    "docs_page",
    "admin_page",
    "api_keys_page",
    "skills_page",
    "skill_detail_page",
]
