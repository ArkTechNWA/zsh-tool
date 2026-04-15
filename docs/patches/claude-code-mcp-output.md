# Claude Code Patch: MCP Tool Output Visibility

**Target:** `@anthropic-ai/claude-code` cli.js
**Version patched:** 2.1.107
**Date:** 2026-04-15

## One-Line Fix

```bash
CLAUDE="$(dirname $(which claude))/../lib/node_modules/@anthropic-ai/claude-code/cli.js"
cp "$CLAUDE" "${CLAUDE}.bak"
sed -i 's/Pwz=m6(()=>y.string().describe("MCP tool execution result"))/Pwz=m6(()=>y.union([y.string(),y.array(y.any())]).describe("MCP tool execution result"))/' "$CLAUDE"
```

## Root Cause

MCPTool's `outputSchema` is `z.string()`. The MCP client returns tool results as content arrays `[{type:"text", text:"..."}]`. The renderer validates data against outputSchema via `safeParse()` — array fails string validation, renderer returns null, user sees nothing.

Built-in tools (Bash, Edit, Read, Write) have NO `outputSchema`. The validation check is skipped entirely for them. They render fine.

## The Code Path

```
MCP server returns: {content: [{type: "text", text: "hello"}]}
MCP client processes: data = [{type:"text", text:"hello"}]  (array)
Renderer: outputSchema.safeParse(data) → z.string() vs array → {success: false}
Renderer: if (W && !W.success) return null  ← NOTHING RENDERS
```

## The Fix

Widen outputSchema from `z.string()` to `z.union([z.string(), z.array(z.any())])`. Array data passes validation. `renderToolResultMessage` is called. Output renders in the `⎿` block.

## Revert

```bash
CLAUDE="$(dirname $(which claude))/../lib/node_modules/@anthropic-ai/claude-code/cli.js"
cp "${CLAUDE}.bak" "$CLAUDE"
```

## Notes

- Re-apply after `npm update -g @anthropic-ai/claude-code`
- Variable names are version-specific (minified). Verify `Pwz` pattern exists before applying on different versions.
- Confirmed working on v2.1.107. The `Pwz` schema was likely introduced recently — earlier versions may not have this issue.
