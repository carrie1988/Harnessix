#!/bin/sh
set -eu

while IFS= read -r request; do
    request_id=$(printf '%s\n' "$request" | sed -n 's/.*"id":\([^,}]*\).*/\1/p')
    case "$request" in
        *'"method":"server/discover"'*)
            printf '{"jsonrpc":"2.0","id":%s,"error":{"code":-32601,"message":"legacy server"}}\n' "$request_id"
            ;;
        *'"method":"initialize"'*)
            printf '{"jsonrpc":"2.0","id":%s,"result":{"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"harnessix-container-fixture","version":"1"}}}\n' "$request_id"
            ;;
        *'"method":"notifications/initialized"'*|*'"method":"notifications/cancelled"'*)
            ;;
        *'"method":"tools/list"'*)
            printf '{"jsonrpc":"2.0","id":%s,"result":{"tools":[{"name":"echo","description":"container echo","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false}}]}}\n' "$request_id"
            ;;
        *'"method":"tools/call"'*)
            printf '{"jsonrpc":"2.0","id":%s,"result":{"content":[{"type":"text","text":"container-ok"}],"structuredContent":{"ok":true},"isError":false}}\n' "$request_id"
            ;;
        *)
            printf '{"jsonrpc":"2.0","id":%s,"error":{"code":-32601,"message":"method not found"}}\n' "$request_id"
            ;;
    esac
done
