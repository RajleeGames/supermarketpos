def fmt(value):
    try:
        return f"TZS {value:,.2f}"
    except Exception:
        return "TZS 0.00"
