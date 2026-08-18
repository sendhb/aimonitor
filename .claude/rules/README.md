# .claude/rules/ — Path-specific rules for Claude Code

> Directory for conditional rules that activate only for specific file paths or patterns.
> See https://code.claude.com/docs/en/memory#organize-rules-with-clauderules

## Usage

Create `.md` files in this directory with optional YAML frontmatter:

```yaml
---
pathPattern: src/api/**/*.ts
---
# API-specific rules
- All API handlers must validate input with Zod
- Use async error wrapper for all handlers
```

Rules without pathPattern apply to all files in the project.
