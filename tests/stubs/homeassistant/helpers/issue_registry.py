"""Stub of homeassistant.helpers.issue_registry: open issues land in `issues`."""

from enum import StrEnum

# (domain, issue_id) -> the keyword arguments the issue was created with.
issues: dict = {}


class IssueSeverity(StrEnum):
    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"


def async_create_issue(hass, domain, issue_id, **kwargs):
    issues[(domain, issue_id)] = kwargs


def async_delete_issue(hass, domain, issue_id):
    issues.pop((domain, issue_id), None)
