# Active-call risk context

## Implemented MVP

`TransferEvent.active_call` is a strict boolean with an optional unknown (`null` or omitted) value. The transfer form explicitly supplies true/false from a simulation toggle. `CallStateProvider` separates observation from scoring, and `SimulatedCallStateProvider` is the only provider used by the HTTP application. Each journey records minimal call context (active/inactive/unknown, simulated source and observation timestamp). The classifier receives that context; the dashboard displays it.

No call audio, phone numbers, caller identity, history or transcripts are read. The event schema rejects those additional fields. Call state is a contextual risk signal, not evidence that a call is fraudulent or authority to invoke banking tools. Existing explicit consent and tool guardrails remain in force.

Policy `KENA-DEMO-2` weights:

| Signal | Points |
| --- | --- |
| Amount at least S$5,000 | 35 |
| New payee | 30 |
| Unusual device | 10 |
| At least three recent new payees | 5 |
| Active call | 25 |

High-risk threshold: 65. Display score is capped at 100; raw score is retained. Device/recent-payee weights are reduced from v1 to leave meaningful headroom for call context. Risk weights are demo assumptions, not an empirically calibrated model.

S$8,000 + new payee: 65 without call context, 90 with it. S$4,000 + new payee + unusual device + recent payees: 45 without a call, 70 with one, triggering outreach. Call context alone is 25 and does not trigger intervention. Unknown is not proof of inactivity and does not reduce other risk signals.

When flagged with `active_call=true`, KENA asks whether someone on the call is instructing the transfer. Messaging still passes through the End User Messaging transport contract, RCS/SMS simulation by default. No bank action occurs until a disputed transfer receives valid explicit confirmation.

## Future legitimate Android integration

Implement an Android banking-app adapter for the `CallStateProvider` observation contract. On Android API 31+, `TelephonyCallback.CallStateListener` reports telephony call-state changes and requires `READ_PHONE_STATE`. Register through the relevant `TelephonyManager`, request the runtime permission transparently and unregister when the intended observation lifecycle ends. The callback applies to its registered subscription; multi-SIM handling must be deliberate. It reports mobile telephony calls, not universal coverage of third-party VoIP calls.

Use the tested `normalize_telephony_state` boundary:

| Observed condition | Normalized value |
| --- | --- |
| `CALL_STATE_IDLE` with permission | false |
| `CALL_STATE_OFFHOOK` with permission | true |
| `CALL_STATE_RINGING` | unknown |
| Permission denied, unsupported, unavailable | unknown |
| Observation older than 30 seconds or future-dated | unknown |

`OFFHOOK` includes dialing, active or held calls; it is not a guarantee of an active conversation. The `active_call` name represents an in-progress telephony risk context. Do not identify a caller or infer conversation content. Do not request recording or call-log permissions for this feature.

At transaction initiation, capture a fresh minimal observation in the legitimate app, bind it to the authenticated customer/device and transfer event, and submit through an authenticated ingestion path. Treat it as untrusted contextual telemetry even with device integrity measures; it cannot bypass consent. KENA currently accepts only simulation input and does not authenticate native telemetry. Android implementation, device testing, permission UX, subscription aggregation and the trusted ingestion path remain future work.

## Official references

- [TelephonyCallback.CallStateListener](https://developer.android.com/reference/android/telephony/TelephonyCallback.CallStateListener)
- [TelephonyManager call-state constants](https://developer.android.com/reference/android/telephony/TelephonyManager#CALL_STATE_OFFHOOK)

Reviewed 13 October 2026. Browser code cannot access these native telephony APIs.
