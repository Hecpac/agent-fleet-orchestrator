---
name: deep-research
description: Research a topic thoroughly by exploring the codebase, reading docs, and summarizing findings. Use for understanding how something works, investigating bugs, or analyzing architecture.
context: fork
agent: Explore
---

# Deep Research

Research `$ARGUMENTS` thoroughly.

## Methodology

1. **Search broadly first**
   - Use Glob to find relevant files by name patterns
   - Use Grep to find references, imports, and usage patterns
   - Cast a wide net before narrowing down

2. **Read and analyze**
   - Read key files completely (don't skim)
   - Trace the flow from entry points to implementation
   - Note patterns, dependencies, and architectural decisions

3. **Map relationships**
   - How do the pieces connect?
   - What depends on what?
   - Where are the boundaries?

4. **Summarize findings**
   - Start with a one-paragraph overview
   - List key files with their roles
   - Describe the architecture/flow
   - Highlight gotchas, tech debt, or non-obvious behavior
   - Include specific file:line references
