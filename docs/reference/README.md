# Public contract snapshot

- File: [tires-openapi-2026-09-27.json](tires-openapi-2026-09-27.json).
- Source: [public Swagger 2.0 schema](https://api.wheel-size.com/v2/tires/swagger/?format=openapi).
- Fetched: `2026-09-27T14:40:21Z`, GET without a key.
- host: `api.wheel-size.com`; basePath: `/v2/tires`.
- SHA-256 of the saved formatted JSON:
  `6d1aef4e1bb09d96bb81ebc55b2543cce534dfc604c02a72dcb56772e3e56e22`.

The schema is kept in full as the primary source: it has 15 paths, but the
first version of the MCP implements only the 12 from the
[inventory](../tools-inventory.md). A path present in the snapshot does not
extend the scope. This is a schema, not a fixture of a live data response.

The snapshot allows studying the interface without network. When updating, save
a new dated file, source, time and hash; record differences in the knowledge
file. Dynamic enums from the snapshot should not be hard-coded into the MCP in
full. Known type-description errors are noted in
[API knowledge](../api-knowledge.md).
