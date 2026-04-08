def ticket_label(ticket: dict) -> str:
    return f"{ticket['finalclass']}::{ticket['id']}"
