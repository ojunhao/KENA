# API examples

All requests are to a local synthetic demo. The public event schema rejects unknown customer fields; arbitrary names, phone numbers and emails are not accepted.

```http
POST /api/events
Content-Type: application/json

{"event_id":"demo-8000-1","amount_cents":800000,"currency":"SGD","new_payee":true,"unusual_device":false,"recent_payee_count":0,"active_call":true,"preferred_channel":"RCS","rcs_available":true}
```

Use the returned `id` in subsequent routes.

`active_call` accepts a strict boolean: true adds 25 risk points and targets the outreach question; false means a simulated inactive call state. Omitted/null is unknown and adds no points. Numbers and string booleans are rejected. Call-state context is explicitly marked simulated. Phone numbers, recordings, call transcripts and history fields are not accepted.

```http
POST /api/customer/<journey-id>/replies
Content-Type: application/json
X-Demo-Customer: demo-customer

{"message_id":"reply-1","text":"I did not make this transfer"}
```

Read the returned `proposal.token`; it is a short-lived demo consent capability, never a token to copy into logs or public operator views.

```http
POST /api/customer/<journey-id>/confirm
Content-Type: application/json
X-Demo-Customer: demo-customer

{"command_id":"consent-1","token":"<current-proposal-token>","approved_actions":["pause_transfer","create_scam_case"]}
```

| Route | Result |
| --- | --- |
| `GET /healthz` | Liveness and synthetic mode |
| `GET /api/dashboard` | Metrics, policy and operator journey views; consent tokens omitted |
| `GET /api/journeys/{id}` | Single operator journey view |
| `DELETE /api/journeys/{id}` | Idempotently remove a synthetic transfer from active views; retain audit and event tombstone |
| `GET /api/customer/{id}` | Matching demo-customer view including proposal |
| `POST /api/events` | Create/replay an event; 201 |
| `POST /api/customer/{id}/replies` | Classify reply, update journey and send simulated response |
| `POST /api/customer/{id}/confirm` | Validate consent and execute atomic mock actions |

Repeating the same event/command ID and identical payload does not create extra records or tools. Changing a payload under a used key returns 409. Deleted event IDs remain blocked with 409; reads/replies/confirmation on a deleted journey return 404. Confirmation without valid current consent returns 409; wrong owner returns 403; invalid schema/unsupported actions returns 422. `X-Demo-Customer` is not real authentication and must be replaced before any external exposure. Deletion is an operator demo action within the same local trust boundary; production authorization is pending.
