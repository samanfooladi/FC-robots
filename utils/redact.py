"""
Redaction helpers — sensitive account fields (email / password / backup code)
must never reach stdout or log files in clear text. Only the SQLite database
may store them.
"""


def redact_email(email: str | None) -> str:
    """'someone@mail.com' → 's***@***' — safe to log."""
    if not email or "@" not in email:
        return "***"
    local = email.split("@", 1)[0]
    return f"{local[:1] or '*'}***@***"
