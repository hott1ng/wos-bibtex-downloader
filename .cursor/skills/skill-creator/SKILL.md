---
name: skill-creator
description: Create, design, and validate Cursor Agent Skills. Use when the user asks to create a skill, install a project skill, write SKILL.md, define skill metadata, or set up reusable agent workflows.
---

# Skill Creator

## Purpose

Help create Cursor Agent Skills as concise, reusable instructions for specialized workflows.

Skills are directories that contain a required `SKILL.md` file and optional supporting files:

```text
skill-name/
├── SKILL.md
├── reference.md
├── examples.md
└── scripts/
```

## Storage Locations

Use `.cursor/skills/skill-name/` for project skills.
Use `~/.cursor/skills/skill-name/` for personal skills.
Never create skills in `~/.cursor/skills-cursor/`; that directory is managed by Cursor.

## Required SKILL.md Format

Every skill needs YAML frontmatter:

```markdown
---
name: skill-name
description: Specific third-person description of what the skill does and when to use it.
---

# Skill Title

## Instructions

Clear, task-focused guidance for the agent.
```

Skill names must be lowercase and use letters, numbers, and hyphens only.
Keep the main `SKILL.md` concise; prefer supporting files for longer references.

## Creation Workflow

1. Confirm the skill purpose and scope.
2. Decide whether the skill is project-level or personal.
3. Choose a clear name with trigger terms.
4. Write a specific third-person description that includes both what the skill does and when to use it.
5. Add only the instructions the agent needs to reliably perform the workflow.
6. Create supporting files only when they reduce clutter in `SKILL.md`.
7. Verify that file references are direct and one level deep.

## Quality Checklist

- Description is specific and includes trigger terms.
- Description is written in third person.
- The skill has a single clear responsibility.
- Instructions are concise and actionable.
- Examples are concrete when output quality depends on them.
- Scripts, if included, have clear usage instructions.
- No stale, time-sensitive, or environment-specific assumptions are baked in.
