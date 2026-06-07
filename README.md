# skills

A collection of agent skills, installable with the [`skills`](https://github.com/vercel-labs/skills) CLI.

## Install

Install everything in this repo:

```bash
npx skills install greatgitsby/skills
```

Install a single skill:

```bash
npx skills install greatgitsby/skills/example-skill
```

### Claude Code

This repo doubles as a Claude Code plugin marketplace. Add it and install:

```
/plugin marketplace add greatgitsby/skills
/plugin install greatgitsby-skills@greatgitsby-skills
```

## Layout

This repo uses the flat layout. Each skill is a directory under `skills/` containing a `SKILL.md`:

```
skills/
└── example-skill/
    └── SKILL.md
```

## Add a skill

```bash
cd skills
npx skills init my-new-skill
```

Then edit `skills/my-new-skill/SKILL.md`. Commit and push, and it becomes installable from this repo.

## License

[MIT](./LICENSE)
