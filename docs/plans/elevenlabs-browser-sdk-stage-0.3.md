# Stage 0.3 — ElevenLabs browser SDK verification

Verified against the current ElevenLabs documentation and the official
`elevenlabs/packages` source on 2026-09-01.

## Result

**PASS.** The remote WebRTC design can keep the ElevenLabs API key on the
computer. The browser can start an authenticated session with a short-lived
conversation token while still supplying Omarvis's page-side client tool and
dynamic variables. Stages 3–4 are not blocked by the plan's hard-stop
condition.

## Pinned API shapes

### Token mint

The computer mints a WebRTC token with the existing Python SDK:

```python
response = client.conversational_ai.conversations.get_webrtc_token(
    agent_id=str(config["agent_id"]),
)
token = response.token
conversation_id = response.conversation_id
```

This is `GET /v1/convai/conversation/token?agent_id=...` with the API key in
the `xi-api-key` header. Its response contains both `token` and
`conversation_id`. The API key is never returned to the page.

The browser starts the private WebRTC conversation with this shape:

```javascript
const conversation = await Conversation.startSession({
  conversationToken: token,
  connectionType: "webrtc",
  dynamicVariables: dynamicVariables,
  clientTools: {
    run: relayToApi,
  },
  // callbacks omitted
});
```

`Conversation.startSession` accepts `conversationToken`, `dynamicVariables`,
and `clientTools` together. Client-tool handlers may be async and their return
value is passed back to the agent. The configured ElevenLabs `run` client tool
must expect a response so the conversation blocks for the relayed result.

### Conversation ID consequence

The token response already determines the conversation ID. Apply the branch
specified in plan rev 6: `/api/token` creates the per-session handler and
binds it to `response.conversation_id`; `POST /api/session` no longer has a
start/registration action. The ID returned by `Conversation.startSession`
can be checked against the minted ID as a fail-closed consistency assertion.
An end action may remain as the page's courtesy shutdown signal; the SSE/ping
lifeline and server cap remain authoritative.

### Client-tool signature and correlation

The public JavaScript client-tool handler receives only the configured tool
parameters. The SDK retains `tool_call_id` internally and uses it when it
sends the handler's result, but does not pass it into the handler.

Therefore use plan 3.9's declared fallback for remote `omarvis see`: one fixed
pending-screenshot key and an enforced invariant that at most one screenshot
may be pending at a time. Do not assume Python-SDK correlation-ID parity.

### Dynamic variables

Pass the result of `catalog_variables(config=config)` from `/api/token` to the
page, then pass it as the camel-case `dynamicVariables` option to
`Conversation.startSession`. The conversation token does not replace or
prevent page-side initiation data.

### Contextual updates

Supported directly:

```javascript
conversation.sendContextualUpdate(text);
```

Stage 3.8 can forward desktop, browser-tab, and Herdr context events over SSE;
the documented degraded-context fallback is not needed.

### Multimodal/image injection

Supported directly in current `@elevenlabs/client`:

```javascript
conversation.sendMultimodalMessage({
  text: screenshotFollowup,
  fileId: fileId,
});
```

`sendMultimodalMessage` was added to the public client API in
`@elevenlabs/client` 1.1.1 and remains present in the current official source.
Stage 3.9 can preserve tool-result-first ordering and then inject the uploaded
file turn; the spoken-refusal fallback is not needed.

### Transcript events

The current client maps the server's final `user_transcript` event to
`onMessage({ role: "user", message, event_id })`. Use only those user-role
messages for `POST /api/transcript`; do not derive confirmation turns from
debug/tentative events.

## Sources

- ElevenLabs API reference: <https://elevenlabs.io/docs/api-reference/conversations/get-webrtc-token>
- ElevenLabs JavaScript SDK docs: <https://elevenlabs.io/docs/eleven-agents/libraries/java-script>
- ElevenLabs React SDK docs (complete `startSession` option contract): <https://elevenlabs.io/docs/eleven-agents/libraries/react>
- ElevenLabs dynamic variables: <https://elevenlabs.io/docs/eleven-agents/customization/personalization/dynamic-variables>
- ElevenLabs client-to-server events: <https://elevenlabs.io/docs/eleven-agents/customization/events/client-to-server-events>
- Official browser SDK source: <https://github.com/elevenlabs/packages/tree/main/packages/client/src>
- Multimodal API release note: <https://elevenlabs.io/docs/changelog/2026/4/7>
