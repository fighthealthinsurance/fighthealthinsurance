
# Fight Paperwork — Streaming WebSocket APIs (Caller-Facing Spec)

_Last updated: 2025-09-02 (PT)_

This document specifies the caller-facing behavior, inputs, outputs, and JSON Schemas for our WebSocket-based streaming backends.

## Transport & Conventions

- **Protocol:** WebSocket (text frames).
- **Framing:**
  - Some streams use **newline keep‑alives** (`"\n"` frames). Treat blank/`"\n"` frames as **non-data**.
  - Data frames are **JSON strings**. For `StreamingAppealsBackend` and `StreamingEntityBackend`, the server sometimes sends raw newlines between JSON frames.
- **Encoding:** UTF-8 JSON.
- **Backpressure:** The server yields periodically (`asyncio.sleep(0)`/`sleep(1)`). Clients should **read promptly**.
- **Closure:**
  - `StreamingAppealsBackend`, `StreamingEntityBackend`, `PriorAuthConsumer` close **automatically** when done.
  - `OngoingChatConsumer` remains open for multi-turn use; closes on client disconnect or server error.
- **Errors:** When provided, errors arrive as JSON objects with an `error` field; otherwise the socket may close abruptly (server logs the stack).

---

## 1) StreamingAppealsBackend

**Purpose:** Stream generated appeal records (both previously saved and newly generated) for a given denial.

### Request

**Method:** Send one JSON message after connecting.

#### Request JSON Schema
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "StreamingAppealsBackendRequest",
  "type": "object",
  "additionalProperties": true,
  "required": ["denial_id", "semi_sekret"],
  "properties": {
    "denial_id": { "type": "string", "description": "UUID of the denial" },
    "email": { "type": "string", "format": "email", "description": "Patient email (hashed server-side)" },
    "semi_sekret": { "type": "string", "description": "Shared secret used in lookup -- this comes from the previous rest call creating the denial record." },
    "professional_to_finish": { "type": "boolean", "default": false, "description": "If true, marks the appeal for professional completion" }
  }
}
```

#### Example Request
```json
{
  "denial_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
  "email": "pat@example.org",
  "semi_sekret": "s3mi-s3cret",
  "professional_to_finish": true
}
```

### Streamed Output

- Initial **keep‑alive**: `\n`
- Then **one or more JSON records** (each sent as a text frame). Newlines may be interspersed.

#### Example Appeal Record
```json
"Dear United Healthcare..."
```


### Errors & Closure
- Parameter issues raise and close the socket (errors logged). No guaranteed structured error.
- Closes automatically after streaming all records.

---

## 2) StreamingEntityBackend

**Purpose:** Wait until entity extraction is completed.

### Request

#### Request JSON Schema
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "StreamingEntityBackendRequest",
  "type": "object",
  "required": ["denial_id"],
  "additionalProperties": true,
  "properties": {
    "denial_id": { "type": "string", "description": "UUID of the denial" }
  }
}
```

#### Example Request
```json
{ "denial_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed" }
```

### Streamed Output

- Streams records until all entities are extracted.
- The results are place holders can be ignored.
- This is largely a way to safely wait longer than normal REST timeout for entity extraction.

### Errors & Closure
- On internal errors, the server logs and the socket closes; records sent up to failure may be partial.
- Closes automatically after final `\n`/record burst.

---

## 3) PriorAuthConsumer

**Purpose:** Stream proposed prior authorization objects for an existing `PriorAuthRequest`.

### Request

#### Request JSON Schema
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "PriorAuthRequestStart",
  "type": "object",
  "required": ["token", "id"],
  "properties": {
    "token": { "type": "string", "description": "Access token tied to the prior auth request" },
    "id": { "type": "string", "description": "PriorAuthRequest ID (UUID)" }
  },
  "additionalProperties": true
}
```

#### Example Request
```json
{ "token": "pa_tok_abc123", "id": "2f99a07f-8be6-4d4e-8df6-7f1d3f1b5d2a" }
```

### Streamed Output

1) **Status message** at start:
```json
{ "status": "generating", "message": "Starting to generate prior authorization proposals" }
```

2) Then **proposal objects** (only sent if they include `"proposed_id"`).

#### Stream Message Union Schema
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "oneOf": [
    {
      "title": "StatusMessage",
      "type": "object",
      "required": ["status", "message"],
      "properties": {
        "status": { "type": "string", "const": "generating" },
        "message": { "type": "string" }
      },
      "additionalProperties": true
    },
    {
      "title": "ProposalMessage",
      "type": "object",
      "required": ["proposed_id", "text"],
      "properties": {
        "proposed_id": { "type": "string" },
        "text": { "type": "string", "description": "Proposal prior auth text" }
      },
      "additionalProperties": true
    },
    {
      "title": "ErrorMessage",
      "type": "object",
      "required": ["error"],
      "properties": {
        "error": { "type": "string" }
      },
      "additionalProperties": true
    }
  ]
}
```

#### Example Proposal
```json
{
  "proposed_id": "pp_47a0b8",
  "text": "Dear United Healthcare ....",
}
```

### Errors & Closure
- Missing/invalid credentials:
  - `{"error":"Missing token or prior auth ID"}` → close
  - `{"error":"Invalid token or prior auth ID"}` → close
- Server exception: `{"error":"Server error: <msg>"}` → close
- Closes automatically ~1s after the generator finishes.

---

## 4) OngoingChatConsumer

**Purpose:** Multi-turn chat for professional users and patients, with support for replay and iteration flags.

### Request

Two modes:
- **New/continuation message** (`replay: false` or omitted)
- **Replay** (`replay: true` requires `chat_id`)

#### Request JSON Schema (Message Mode)
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "OngoingChatMessage",
  "type": "object",
  "additionalProperties": true,
  "allOf": [
    {
      "oneOf": [
        { "required": ["message"] },
        { "required": ["content"] }
      ]
    },
    {
      "anyOf": [
        { "required": ["session_key"] },
        { "required": ["is_patient", "email"] },
        { "required": [] }
      ]
    }
  ],
  "properties": {
    "message": { "type": "string", "description": "Chat content; alias: content" },
    "content": { "type": "string", "description": "Alias for message" },
    "chat_id": { "type": "string", "description": "Existing chat UUID to continue" },
    "replay": { "type": "boolean", "const": false },
    "iterate_on_appeal": { "type": "object", "description": "Flags/params for appeal iteration" },
    "iterate_on_prior_auth": { "type": "object", "description": "Flags/params for prior auth iteration" },
    "is_patient": { "type": "boolean", "default": false },
    "email": { "type": "string", "format": "email", "description": "Required if is_patient is true (for deletion routing)" },
    "session_key": { "type": "string", "description": "Required for anonymous professional (trial) chats" }
  }
}
```

#### Request JSON Schema (Replay Mode)
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "OngoingChatReplay",
  "type": "object",
  "required": ["replay", "chat_id"],
  "properties": {
    "replay": { "type": "boolean", "const": true },
    "chat_id": { "type": "string" }
  },
  "additionalProperties": true
}
```

#### Example Requests
_New message, anonymous professional (trial):_
```json
{
  "message": "What info do you need to appeal this?",
  "session_key": "sess_abc123"
}
```

_New message, patient:_
```json
{
  "message": "My MRI was denied, what should I do?",
  "is_patient": true,
  "email": "pat@example.org"
}
```

_Replay:_
```json
{ "replay": true, "chat_id": "c3b8a1e2-6e3f-4c7c-90d2-7a2e5a1f1c9a" }
```

### Streamed Output

- Frames are **JSON objects**. Shapes vary by `ChatInterface`. Expect token deltas, tool-call notifications, and final messages.
- Standardized **error** objects when validation fails.

#### Output Union Schema (best-effort)
```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "oneOf": [
    {
      "title": "ChatMessage",
      "type": "object",
      "properties": {
        "role": { "type": "string", "enum": ["system", "user", "assistant", "tool"] },
        "content": { "type": "string" },
        "delta": { "type": "string", "description": "Optional incremental token text" },
        "meta": { "type": "object", "additionalProperties": true }
      },
      "additionalProperties": true
    },
    {
      "title": "ErrorMessage",
      "type": "object",
      "required": ["error"],
      "properties": {
        "error": { "type": "string" }
      },
      "additionalProperties": true
    }
  ]
}
```

#### Example Error Responses
```json
{ "error": "Session key is required for anonymous professional users." }
```
```json
{ "error": "Email is required for patient users." }
```
```json
{ "error": "Invalid email format." }
```

### Lifecycle & Post-Processing

- The socket stays open across turns (client-controlled).
- On **disconnect**, server runs a best-effort analysis of the chat to infer `denied_item`/`denied_reason`. This does **not** send additional frames; it persists results if confidently extracted.

---

## Client Notes (All Streams)

- Treat `\n`-only frames as **keep-alives**.
- Be **schema-tolerant**: new fields may appear; unknown fields should not break clients.
- For logging/observability, capture raw frames before parsing; parse errors should be non-fatal where possible.
