# Security & Permissions Gatekeeper
1. All intents must be parsed into a Pydantic `StructuredIntent` (intent, action, parameters, origin).
2. Permission Manager uses a 4-level matrix: ALLOW, CONFIRM, DENY, BLOCK.
3. Taint Analysis: If the `origin` field is 'INDIRECT_WEB_CONTENT', the engine must instantly downgrade any 'ALLOW' action to 'CONFIRM' to prevent indirect prompt injections.
4. Multi-modal confirmation: If state is CONFIRM, trigger a native Windows Toast notification (windows-toasts) with Allow/Deny buttons asynchronously alongside a voice prompt.
