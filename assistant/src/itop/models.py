from enum import StrEnum


class TicketStatus(StrEnum):
    NEW = "new"
    # ASSIGNED = "assigned"
    # PENDING  = "pending"
    # RESOLVED = "resolved"
    # CLOSED   = "closed"


# class Ticket(BaseModel):
#     id:                  str
#     title:               str
#     description:         str
#     status:              TicketStatus
#     service:             str = Field(alias="service_name")
#     service_subcategory: str = Field(alias="servicesubcategory_name")
#     caller_id:           str
#     public_log:          list[PublicLogEntry] = []
#
#     model_config = {"populate_by_name": True}
#
# class Ticket(BaseModel):
#     class_name: str
#     id: str
#     ref: str
#     title: str
#     description: str
#     service_id: str
#     servicesubcategory_id: str
#     caller_id: str
#     status: TicketStatus
#
# class Service(BaseModel):
#     id: str
#     name: str
#     description: Optional[str]
#
# class ServiceSubcategory(BaseModel):
#     id: str
#     name: str
#     description: Optional[str]
