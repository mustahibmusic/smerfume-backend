"""Display helpers for Smerfume Panel templates."""

from django import template

register = template.Library()


@register.filter
def person(user):
    """A staff or customer name for display: full name, else email, else
    mobile number; an em dash when there is no user (e.g. SET_NULL)."""
    if user is None:
        return "—"
    return (
        getattr(user, "full_name", "") or getattr(user, "email", "") or getattr(user, "mobile_number", "") or "—"
    )
