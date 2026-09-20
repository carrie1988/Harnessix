import assert from "node:assert/strict"
import { sanitizeTerminalUrl } from "../src/path_utils.mjs"

const value = sanitizeTerminalUrl("wss://alice:secret@example.invalid/terminal?id=1")
assert.equal(value, "wss://example.invalid/terminal?id=1")
assert.equal(value.includes("alice"), false)
assert.equal(value.includes("secret"), false)
