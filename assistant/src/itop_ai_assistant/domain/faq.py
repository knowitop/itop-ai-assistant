"""Semantic FAQ model, decoupled from iTop attribute names.

Raw iTop attributes are translated into this model by `FaqRepository`
according to the `faq_mapping` config section — see `domain/ticket.py` for
the same pattern applied to tickets. Not a subclass of `Ticket`: FAQ has no
caller, no log, no request type — modeling it as a stripped-down ticket would
carry fields that mean nothing for this class.
"""

from datetime import datetime

from pydantic import BaseModel


class FaqArticle(BaseModel):
    obj_class: str = "FAQ"
    id: str
    title: str = ""
    summary: str = ""
    category_name: str = ""
    error_code: str = ""
    key_words: str = ""
    description: str = ""  # raw HTML as stored in iTop
    status: str = ""  # empty unless faq_mapping.fields.status is set — no such attribute in stock iTop
    org_id: str | None = None
    # iTop timestamps carry a nominal UTC tzinfo — see ticket_repository._parse_dt
    last_update: datetime | None = None
    start_date: datetime | None = None
