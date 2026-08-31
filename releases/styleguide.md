<!-- This file has been created with the assistance of an AI tool. -->

# Release notes style guide

This guide defines the house style for files in `releases/`. [`0.5.0.md`](0.5.0.md)
is the canonical reference; when in doubt, match it.

## Structure

- Open the file **directly** with `## What's Changed`. No top-level `#` title,
  no version heading, no preamble.
- Follow with one `### <Category>: <short description>` heading per change,
  each with a prose body.
- End the file with a single `**Full Changelog**` line and nothing after it:

  ```markdown
  **Full Changelog**: https://github.com/mayty/claude-agent-wrap/compare/<prev>...<this>
  ```

- The sole exception is the initial release, which has nothing to enumerate and
  may be a one-line note (see [`0.1.0.md`](0.1.0.md)).

A complete skeleton looks like this:

```markdown
## What's Changed
### Added: <short description>

<one tight prose paragraph describing the change>

### Changed: <short description>

<one tight prose paragraph describing the change>

**Full Changelog**: https://github.com/mayty/claude-agent-wrap/compare/<prev>...<this>
```

## Section headings

- Form: `### <Category>: <short description>`.
- The description is lowercase (except code spans, identifiers, and proper
  nouns) and has no trailing punctuation.
- The heading names **every** change in its section. A merged section whose heading
  cannot name all of them is either split, or renamed on the pattern
  `<Category>: <change one>, and <change two>`. Name the change, not the topic:
  "the wrapper creates the missing host paths that a mount needs", not "host paths
  for mounts".
- Sections appear in the file in **exactly** the order the categories are listed
  below — `Breaking`, then `Added`, then `Changed`, then `Improved`, then
  `Fixed`, then `Other`. Group every section of a given category together; never
  interleave categories. Omit any category that has no entries.

  | Category   | Use for |
  |------------|---------|
  | `Breaking` | Changes that require user action to keep an existing workflow working, or that remove existing functionality |
  | `Added`    | New commands, providers, flags, or capabilities |
  | `Changed`  | Behavior changes to existing functionality |
  | `Improved` | Hardening or quality improvements, no behavior contract change |
  | `Fixed`    | Bug fixes |
  | `Other`    | Internal refactors, docs, and housekeeping worth noting |

## Body

- Write **prose paragraphs**, not bullet lists. A single change gets one tight
  paragraph; fold supporting detail into sentences rather than bullets. Split the
  paragraph when it reaches the six-sentence limit below, rather than letting it run.
- Keep fenced code blocks for commands the reader runs (e.g. a migration
  `rm -rf`) — prose alone would lose precision there.
- A Markdown table is allowed for a dense mapping where prose would be harder to
  scan (e.g. the old → new rename map in [`0.4.0.md`](0.4.0.md)). Reach for it
  only when the content is genuinely tabular — default to prose otherwise.
- **No commit hashes and no PR numbers** anywhere — not in headings, not inline.
  The trailing `**Full Changelog**` compare link is the traceability anchor.
- Use code spans for env vars (`AGENT_PROVIDER`), commands (`agent run`),
  function and container/image names, image tags, and paths that are **not**
  files in this repo — e.g. the runtime secrets path `~/claude_keys.json`,
  in-container mount paths, consumer-project files like `Dockerfile.agent`, and
  placeholder paths containing `<...>`.
- Reference any file or directory that exists **in this repo at the release's
  tag** with a markdown link on its first mention in the file — never a bare
  code span. (Later repeats of the same path may stay code spans to avoid a sea
  of links.) Use an absolute GitHub URL pinned to that tag: `/blob/<tag>/...` for
  a file, `/tree/<tag>/...` for a directory, e.g.
  `[README.md](https://github.com/mayty/claude-agent-wrap/blob/<tag>/agent_wrap/providers/litellm_bedrock/README.md)`.
  This applies to every repo file reference (READMEs, docs, source), not just
  READMEs. Pin to the file's location **as of that release** — paths that moved
  later (e.g. the `ops/` reorg in 0.4.0) resolve differently per tag.
- **Release notes describe user-visible behavior, not internals.** State what
  broke and what changed from the user's perspective — never describe how a fix
  works. No data structures, no algorithms, no function signatures, no parameter
  names. If a change cannot be described without internals, it does not belong
  in the release notes.
- **Internal housekeeping is not noteworthy.** Lint fixes, import reordering,
  test additions, refactors, and similar changes that have no user-visible
  effect are omitted from release notes entirely. The `**Full Changelog**`
  compare link is the audit trail for those who need it.

## Language

Release notes are written in **ASD-STE100 Simplified Technical English**. The rules
that matter most here:

- One topic per sentence, and **20 words maximum** per sentence.
- **Six sentences maximum** per paragraph. A section that needs more gets a second
  paragraph.
- Active voice, simple present tense. Name the actor: "`agent update` refuses to
  start", not "the update is refused".
- No `-ing` clauses, and no noun cluster longer than three words.
- No idiom, metaphor or understatement.
- Give the behavior, not the argument for it: no rationale, no self-justification,
  and no "deliberately" or "on purpose".
- Keep the article ("the", "a") in front of each noun. Do not write a telegraphic
  style.

[`0.10.0.md`](0.10.0.md) is the reference for this section; [`0.5.0.md`](0.5.0.md)
stays the reference for structure. Older files keep the prose they shipped with.

## Length

A reader does not read a wall of text, so each release has a budget:

- **200 words maximum per change.** A section that merges related changes gets that
  budget for each change it covers.
- **1500 words maximum per file.** Merge related sections before you cut facts — one
  section for all the changes to one command, for example.
- **80 words maximum** for the release's summary paragraph in
  [CHANGELOG.md](../CHANGELOG.md), which links here for the full notes.

Cut in this order to reach the budget: `--json` field inventories, edge cases and
failure modes, internals, and each "See `<doc>`" line after the first one in a
section.
