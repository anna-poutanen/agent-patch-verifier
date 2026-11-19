def resolve(value=None):
    """Return five only when the caller omits a value."""
    return 5 if value is None else value
