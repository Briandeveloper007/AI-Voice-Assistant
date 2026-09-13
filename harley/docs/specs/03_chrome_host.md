# Chrome Native Messaging Extension
1. Harley controls Chrome via a Manifest V3 extension communicating via a 32-bit length-prefixed `stdio` bridge.
2. Do NOT use Puppeteer or CDP.
3. DOM Sanitization: The extension content script must actively strip zero-opacity nodes, `<script>`, and hidden elements before sending text to the Orchestrator to neutralize prompt injections.
