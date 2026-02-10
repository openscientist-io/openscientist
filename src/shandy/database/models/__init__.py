"""
Database models for SHANDY.

This package contains SQLAlchemy ORM models for all database tables.
Models are organized by domain:
- User authentication (users, oauth_accounts, sessions, api_keys)
- Jobs (jobs, job_shares, job_data_files, job_chat_messages)
- Knowledge state (hypotheses, findings, literature, plots, etc.)
- Cost tracking (cost_records)
"""

# Core authentication models
from .analysis_log import AnalysisLog
from .api_key import APIKey

# Cost tracking
from .cost_record import CostRecord
from .feedback_history import FeedbackHistory
from .finding import Finding

# Relationship models
from .finding_hypothesis import finding_hypotheses
from .finding_literature import finding_literature

# Knowledge state models
from .hypothesis import Hypothesis
from .hypothesis_spawn import HypothesisSpawn
from .iteration_summary import IterationSummary

# Job models
from .job import Job
from .job_chat_message import JobChatMessage
from .job_data_file import JobDataFile
from .job_share import JobShare
from .literature import Literature
from .oauth_account import OAuthAccount
from .plot import Plot
from .session import Session
from .user import User

__all__ = [
    # Core authentication
    "User",
    "OAuthAccount",
    "Session",
    "APIKey",
    # Jobs
    "Job",
    "JobShare",
    "JobDataFile",
    "JobChatMessage",
    # Knowledge state
    "Hypothesis",
    "Finding",
    "Literature",
    "AnalysisLog",
    "IterationSummary",
    "FeedbackHistory",
    "Plot",
    # Relationships
    "finding_hypotheses",
    "finding_literature",
    "HypothesisSpawn",
    # Cost tracking
    "CostRecord",
]
