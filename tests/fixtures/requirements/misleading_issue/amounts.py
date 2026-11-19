def parse_amount(text):
    if text == "":
        raise ValueError("empty amount")
    return int(text)
