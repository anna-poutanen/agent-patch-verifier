def normalize(value):
    return "missing" if value is None else str(value).strip().lower()
