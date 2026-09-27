---
name: deep-research
description: Investigate a codebase, trace behavior and dependencies, and summarize architecture or bug findings with file references. Use for repository research; general web or literature research belongs to other workflows.
context: fork
agent: Explore
---

# Deep Research

Research `$ARGUMENTS` thoroughly.

## Methodology

1. **Start from the question and known references**
   - Find the relevant entry points, files and usage patterns
   - Expand the search when evidence is missing or reveals another relevant boundary

2. **Read and analyze**
   - Read the complete contracts or code blocks needed to evaluate the conclusion; read entire files when their surrounding context matters
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
