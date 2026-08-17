def calendar_entry_sort_key(entry):
    """Sort scheduled entries first, then keep ties deterministic by type."""
    scheduled_at = entry["scheduled_at"]
    return scheduled_at is None, scheduled_at, entry["event_type"]


def sort_calendar_entries(entries):
    return sorted(entries, key=calendar_entry_sort_key)
