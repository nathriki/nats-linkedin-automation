# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

nats-linkedin-automation — currently a fresh repository (see [README.md](README.md)). Update this file as the project grows with build/test/run commands and architecture notes.

## gstack

This project has the gstack skill suite available. Invoke any of these as a slash command (e.g. `/ship`). Router entry points are `/gstack` and `/_gstack-command`.

### Planning & review
- `/spec` — turn vague intent into a precise, executable spec in five phases.
- `/plan-ceo-review` — CEO/founder-mode plan review.
- `/plan-eng-review` — eng manager-mode plan review.
- `/plan-design-review` — designer's-eye plan review, interactive.
- `/plan-devex-review` — interactive developer experience plan review.
- `/plan-tune` — self-tuning question sensitivity + developer psychographic (observational).
- `/autoplan` — auto-review pipeline running CEO, design, eng, and DX review skills sequentially with auto-decisions.
- `/office-hours` — YC Office Hours style review (two modes).

### Shipping & QA
- `/review` — pre-landing PR review.
- `/ship` — ship workflow: merge base branch, run tests, review diff, bump VERSION, update CHANGELOG, commit, push, create PR.
- `/land-and-deploy` — land and deploy workflow.
- `/landing-report` — read-only queue dashboard for workspace-aware ship.
- `/qa` — systematically QA test a web app and fix bugs found.
- `/qa-only` — report-only QA testing (no fixes).
- `/canary` — post-deploy canary monitoring.
- `/benchmark` — performance regression detection using the browse daemon.
- `/benchmark-models` — cross-model benchmark for gstack skills.
- `/retro` — weekly engineering retrospective.
- `/health` — code quality dashboard.
- `/devex-review` — live developer experience audit.
- `/investigate` — systematic debugging with root cause investigation.
- `/security-review` (cso mode: `/cso`) — chief security officer mode / security review of pending changes.

### Design
- `/design-consultation` — proposes a full design system (aesthetic, typography, color, layout, spacing, motion) with previews.
- `/design-html` — design finalization: production-quality Pretext-native HTML/CSS.
- `/design-review` — designer's-eye QA: visual inconsistency, spacing, hierarchy, AI-slop patterns, slow interactions.
- `/design-shotgun` — generate multiple AI design variants, open a comparison board, collect feedback, iterate.
- `/diagram` — turn an English description or mermaid source into a diagram triplet (source, .excalidraw, SVG/PNG).

### Browser & scraping
- `/browse` — fast headless browser for QA testing and site dogfooding.
- `/connect-chrome`, `/open-gstack-browser` — launch GStack Browser (AI-controlled Chromium with the sidebar extension).
- `/setup-browser-cookies` — import cookies from a real Chromium browser into the headless browse session.
- `/scrape` — pull data from a web page.
- `/skillify` — codify the most recent successful `/scrape` flow into a permanent browser-skill on disk.
- `/pair-agent` — pair a remote AI agent with your browser.

### iOS
- `/ios-qa` — live-device iOS QA for SwiftUI apps.
- `/ios-fix` — autonomous iOS bug fixer.
- `/ios-design-review` — visual design audit for iOS apps on real hardware.
- `/ios-clean` — remove the DebugBridge SPM package and all `#if DEBUG` wiring.
- `/ios-sync` — regenerate the iOS debug bridge against the latest upstream gstack templates.

### Docs
- `/document-generate` — generate missing documentation from scratch for a feature, module, or project.
- `/document-release` — post-ship documentation update.
- `/make-pdf` — turn any markdown file into a publication-quality PDF.

### Safety & session utilities
- `/careful` — safety guardrails for destructive commands.
- `/guard` — full safety mode: destructive command warnings + directory-scoped edits.
- `/freeze` / `/unfreeze` — restrict/clear file-edit scope to a specific directory for the session.
- `/context-save` / `/context-restore` — save and restore working context.
- `/setup-deploy` — configure deployment settings for `/land-and-deploy`.
- `/setup-gbrain` / `/sync-gbrain` — set up and keep gbrain (project memory/search) current.
- `/gstack-upgrade` — upgrade gstack to the latest version.
- `/codex` — OpenAI Codex CLI wrapper (three modes).

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
