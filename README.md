# skills

A collection of agent skills, installable with the [`skills`](https://github.com/vercel-labs/skills) CLI.

## Install

Use the [`skills`](https://github.com/vercel-labs/skills) CLI (the subcommand is `add`, not `install`):

```bash
npx skills add greatgitsby/skills                 # all skills in this repo
npx skills add greatgitsby/skills -s mici         # one skill
npx skills add greatgitsby/skills --list          # list without installing
npx skills add greatgitsby/skills -s mici -g      # install globally (all projects)
npx skills add greatgitsby/skills -s mici -a '*'  # install to all detected agents
npx skills add greatgitsby/skills --all           # all skills, all agents, no prompts
```

## Update

```bash
npx skills update            # update all installed skills
npx skills update mici       # update one skill
npx skills update -g         # global skills only (-p for project only)
```

### Claude Code plugin marketplace

```
/plugin marketplace add greatgitsby/skills
/plugin install greatgitsby-skills@greatgitsby-skills
```

## Add a skill

```bash
cd skills
npx skills init my-new-skill
```

Then edit `skills/my-new-skill/SKILL.md`. Commit and push, and it becomes installable from this repo.

## License

[MIT](./LICENSE)
