# iTop Domain Knowledge

Context for working with the Combodo iTop ITSM platform.

## What is iTop

iTop is an open-source ITSM/ServiceDesk/CMDB platform built on PHP. It exposes
a REST/JSON API for all operations. This service integrates with iTop exclusively
via that API — no direct database access, no PHP code.

## REST API

**Base URL pattern:** `{ITOP_URL}/webservices/rest.php?version=1.3`

All requests are POST with `multipart/form-data`:
- `auth_user` / `auth_pwd` — credentials (basic auth)
- `auth_token` — application or personal token (alternative to user/pwd)
- `json_data` — JSON string with the operation

**Access control:** the iTop user must have the `REST Services User` profile.
Administrators do NOT get REST access by default — it must be explicitly granted.
- `core/get` and `core/get_related` require **bulk read** privilege
- Modifying operations require **write access** AND **bulk write** privilege

**Core operations:**
```json
// Get object by ID
{
  "operation": "core/get",
  "class": "UserRequest",
  "key": "SELECT UserRequest WHERE id = 123",
  "output_fields": "title,description,status,agent_id,caller_id"
}

// Update object (append to public log)
{
  "operation": "core/update",
  "class": "UserRequest",
  "key": 123,
  "fields": {
    "public_log": {
      "add_item": {
        "message": "Text of the comment",
        "user_login": "ai-assistant"
      }
    }
  }
}
```

**Response structure:**
```json
{
  "code": 0,
  "message": "Found: 1",
  "objects": {
    "UserRequest::123": {
      "key": "123",
      "fields": { ... }
    }
  }
}
```
`code: 0` means success. Any other code is an error.
When no objects match, `objects` is `null` (not `{}`).

**Error codes:**

| Code | Meaning |
|------|---------|
| 0 | OK |
| 1 | UNAUTHORIZED — insufficient credentials or rights |
| 100 | INTERNAL_ERROR — server-side failure |

## Key Specification Formats

Objects can be identified in three ways:
```json
// 1. Numeric ID
"key": 123

// 2. OQL query string
"key": "SELECT UserRequest WHERE status = 'new'"

// 3. Criteria object (AND of all fields)
"key": {"name": "Smith", "first_name": "John"}
```

## output_fields

- `"field1,field2"` — specific fields
- `"*"` — all attributes of the class
- `"*+"` — all attributes including subclass-specific ones

Avoid returning `*_list` fields (link sets) unless needed — can cause memory
exhaustion on large datasets.

## Case Log Formats (public_log / private_log)

Three formats for writing to case logs:

```json
// Shorthand (message only, posted as current auth user)
{"public_log": "Comment text"}

// Rich (explicit user and optional date)
{
  "public_log": {
    "add_item": {
      "message": "Text of the comment",
      "user_login": "ai-assistant",
      "date": "2024-01-15 10:30:00"
    }
  }
}

// Full (replace entire log — use with extreme caution)
{
  "public_log": {
    "items": [
      {"date": "...", "user_login": "...", "message": "..."}
    ]
  }
}
```

## Key Object Classes

| Class | Description |
|-------|-------------|
| `UserRequest` | Service request from a user |
| `Incident` | Incident ticket |
| `Change` | Change request (RFC) |
| `Problem` | Problem record |
| `Service` | IT service |
| `ServiceSubcategory` | Sub-category of a service |
| `Person` | User/contact record |
| `Team` | Support team |
| `FunctionalCI` | Configuration item (base class) |
| `Server`, `PC`, `Software` | Specific CI types |

## Ticket Lifecycle (UserRequest / Incident)

Standard statuses in order:
```
new → assigned → in_progress → resolved → closed
                     ↓
               waiting_for_user  (on hold, waiting user response)
               waiting_for_3rd_party
```

**For this service: only act when `status == "new"`.**
If status has moved beyond "new", an engineer has taken the ticket — stop
processing and do nothing.

## Webhook Payload

iTop sends a POST with JSON body:
```json
{
  "id": 123,
  "class": "UserRequest",
  "async": false
}
```

- `id` — ticket ID, use it to fetch full object via API
- `class` — object class, determines which fields to fetch and how to process
- `async` — if true, return 202 immediately and process in background

The webhook payload intentionally contains minimal data. Always fetch the full
object from the API — never rely solely on webhook data.

## Public Log vs Private Log

iTop tickets have two logs:
- `public_log` — visible to the end user (caller) in the self-service portal
- `private_log` — visible to IT staff only

**AI posts clarifying questions to `public_log`** so the user sees them.
**AI posts internal notes to `private_log`** (e.g. "ticket enriched, category
set to X").

## AI Service Account

The AI operates under a dedicated iTop account (configured via `ITOP_AI_USER`
env var). This is critical for:
- Distinguishing AI comments from engineer/user comments when reading log history
- Auditing — all AI actions are traceable to one account
- Avoiding loops — if the last public log entry was posted by `ITOP_AI_USER`,
  do not post another question until the user responds

## Fetching Related Objects

When processing `UserRequest` or `Incident`, always fetch related objects for
context:
```json
// Get service details
{
  "operation": "core/get",
  "class": "Service",
  "key": "SELECT Service WHERE id = {service_id}",
  "output_fields": "name,description"
}
```

Related object IDs come as `{"id": 5, "name": "..."}` in the parent object's
fields.

## Pagination

By default iTop returns all matching objects. For large result sets use `limit`
and `page` (page number starting from **1**, not 0):
```json
{
  "operation": "core/get",
  "class": "UserRequest",
  "key": "SELECT UserRequest WHERE status = 'new'",
  "output_fields": "id,title,status",
  "limit": 50,
  "page": 1
}
```

Fetch next page with `"page": 2`, etc. Stop when the returned batch is smaller
than `limit`.

## Common Pitfalls

- Always check `code == 0` in the response before accessing `objects`
- `objects` is `null` (not `{}`) when no records match — check for null/None
- Field values for linked objects come as dicts: `{"id": 5, "name": "Foo"}`,
  not plain strings
- Public log entries are appended, never replaced — never try to overwrite log
- Ticket `id` in webhook is an integer; iTop API accepts both int and string
- `core/update` does NOT support bulk updates — one object per call
- Admins need `REST Services User` profile explicitly — it's not granted by default
