# Content Factory

This fork of OpenMontage runs a multi-channel content factory. Everything in
this file sits **on top of** `AGENT_GUIDE.md` — pipelines, stage directors,
checkpoints and the `projects/` workspace contract all still apply.

## Privacy: channel content never goes in this repo

This fork is **public**. All channel content (briefs, brand, voice, playbooks,
series plans, logs, exports) lives in `channels/`, which is a **separate private
git repo** and is gitignored here, together with `styles/custom/`.

- Commit channel work **inside `channels/`** (`git -C channels commit ...`), never in the fork.
- Never `git add -f` anything under `channels/` or `styles/custom/` in the fork.
- Never put channel names, scripts or brand details in fork files (docs, tests, commit messages).
- Only generic, channel-agnostic code and docs (tools, fixes, this file) belong in the fork.

## Layout

```
channels/                     # PRIVATE repo, gitignored by the fork
  _template/                  # copy to start a new channel
  <channel>/
    CHANNEL.md                # who it's for, tone, topics, default pipelines
    brand/
      BRAND.md                # visual identity, image-generation rules, thumbnail recipe
      playbook.yaml           # the channel's style playbook
      voice/VOICE.md          # pinned narrator voice + pronunciation rules
      logo/  fonts/  references/
    series/                   # one file per season: episode plan, shared elements, shorts plan
    platforms/                # one rules file per platform the channel publishes to
    content-log.md            # everything produced/published, newest first
    published/                # final exports, one folder per platform (ignored by the channels repo too)
styles/custom/<channel>.yaml  # symlink to the channel playbook (scripts/factory_link_playbooks.sh)
projects/<channel>--<slug>/   # the normal OpenMontage workspace for each video
```

After cloning the channels repo on a new machine, run `scripts/factory_link_playbooks.sh`.

## Rules for every production

1. **Identify the channel.** If the user didn't name one, ask. List
   `channels/` (ignore `_template`) to show the options.
2. **Read the channel context before Rule Zero's pipeline selection:**
   `channels/<channel>/CHANNEL.md`, `brand/BRAND.md`, `brand/voice/VOICE.md`,
   the `platforms/<platform>.md` file for every target platform, and the
   `series/` file the video belongs to (ask if unclear). Their constraints feed
   the brief, the proposal and the scene plan. A series episode must honour the
   season's shared elements (opening card, recurring segment, callbacks, end card).
3. **Pick the pipeline** from the channel's "Default pipelines" table unless the
   user asks for something else. Then follow `AGENT_GUIDE.md` as usual.
4. **Name the project `<channel>--<slug>`** and initialize it with the channel playbook:
   `init_project('<channel>--<slug>', title=..., pipeline_type=..., style_playbook='<channel>')`.
5. **Render with the platform's media profile** from `lib/media_profiles.py`
   (each platform file names the profile to use). One idea produced for several
   platforms is **one project with several renders**, not several projects.
6. **Brand consistency (non-negotiable):**
   - Narration uses the voice, model and settings pinned in `VOICE.md`. Never switch voices silently.
   - Every generated image passes the channel's style reference image(s) and contains no text (see `BRAND.md`).
   - Thumbnails = text-free generated background + `thumbnail_compose` for the title, following `BRAND.md`.
7. **Deliver:** copy each final render to
   `channels/<channel>/published/<platform>/<YYYY-MM-DD>--<slug>.mp4`, plus the
   thumbnail and a `<YYYY-MM-DD>--<slug>.md` with title, description, tags and
   hashtags written to that platform's rules.
8. **Log it:** add a line to `channels/<channel>/content-log.md`, update the
   episode's status in its `series/` file, and commit in the channels repo.

## Provider preference (credits are limited)

When the same model or capability is available from a first-party API we hold
a key for, use that API — **not** Higgsfield. Higgsfield credits are reserved for
things only Higgsfield offers.

| Need | Use first | Then | Higgsfield only for |
|---|---|---|---|
| Images | `google_imagen` (`gemini-3-pro-image`, Imagen 4) | `openai_image` (`gpt-image-2`, when `OPENAI_API_KEY` is set) | Soul / Soul ID, Marketing Studio, models with no direct API |
| Video | `veo_video` (Google API) | `sora_video` (when `OPENAI_API_KEY` is set) | Seedance 2.0, Kling and other Higgsfield-only models |
| Narration | `elevenlabs_tts` (direct) | — | — |

Before any Higgsfield call, check the price with `higgsfield generate cost <model> ...`
and include it in the cost estimate shown to the user.

## Adding a channel

1. `cp -r channels/_template channels/<channel>` and fill in every file.
2. Write `channels/<channel>/brand/playbook.yaml` (start from the closest preset in
   `styles/`), run `scripts/factory_link_playbooks.sh`, and check it loads:
   `python -c "from styles.playbook_loader import load_playbook; load_playbook('<channel>')"`.
3. Fill `brand/`: SVG logo masters + PNG exports, fonts (with licenses), at
   least one style reference image, and a pinned voice in `brand/voice/VOICE.md`.
4. Plan the first season in `series/`.
5. Run `pytest tests/contracts`. Upstream contract tests check every playbook,
   including linked custom ones (e.g. caption contrast).
6. Commit in the channels repo.

## Keeping upstream merges painless

This repo pulls updates from `calesthio/OpenMontage` (`git fetch upstream && git merge upstream/main`).
Prefer adding new files (new tools, skills, scripts) over editing upstream
files. When an upstream file must change, keep the edit small.

### Local patches to upstream files
Check these when an upstream merge conflicts. Drop a patch if upstream fixes the same thing.
- `CLAUDE.md`: one line pointing to this file.
- `tools/video/video_compose.py`: the caption bar picks light or dark from the background's luminance (upstream only treated three exact whites as light, which gave warm off-white playbooks an unreadable dark bar).
- `tests/contracts/test_remotion_theme_playbook_keys.py`: `_raw()` also looks in `styles/custom/`.
