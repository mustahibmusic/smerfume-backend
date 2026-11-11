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


@register.simple_tag
def page_links(page_obj):
    """Page numbers for the pagination bar: the first and last pages, two
    either side of the current one, and ``None`` for each gap."""
    if page_obj is None:
        return []
    return list(page_obj.paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1))
