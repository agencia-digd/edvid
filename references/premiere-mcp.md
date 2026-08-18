# PREMIERE (MCP) — editing inside Adobe Premiere Pro instead of the ffmpeg pipeline

Read this file when the user wants the edit to happen **inside Premiere via the
`premiere-pro` MCP** (e.g. "edit the sequence X in Premiere", "corte no Premiere
via MCP") rather than through `render.py`/`cut.mp4`.

## Golden rule — the medium changes, the METHOD does not

Everything in SKILL.md still governs: audio-primary, cut on silence, best takes,
word-boundary edges from `speech_regions.py`, the **phase gate**, grade with
taste, verify before showing. The Premiere MCP is only a different set of hands.
So the analysis half (transcribe → `takes_packed.md` → `edl.json`) is IDENTICAL
and CACHED — **reuse an existing approved `edl.json`** if `<videos_dir>/edit/`
already has one; the timeline just replays those ranges.

Do NOT build `cut.mp4` / clips_graded / the preview server for a Premiere job —
the Premiere sequence itself is the deliverable and the user watches in the
Program Monitor. `verify_cut.py` is replaced by `get_full_sequence_info` +
`export_frame` (see Verify below).

## Before touching the timeline

1. **Read the server's own operating rules first:** MCP resource
   `premiere://config/get_instructions`. Key ones: inspect before editing;
   prefer non-destructive first (offer `duplicate_sequence` as a backup before
   razoring); cut many layers with `razor_timeline_at_time` (not clip-by-clip);
   verify the active sequence; **if a tool fails, report the real limitation,
   never fake success** (scripting coverage is incomplete).
2. Discover: `ping` → `get_project_info` → `list_sequences` →
   `get_full_sequence_info` (clip layout) → `find_project_item_by_name` to get
   the source `mediaPath` for transcription. Confirm the target sequence is active.
3. Map: when a clip sits at timeline 0 with source in-point 0, **timeline time ==
   source time**, so `edl.json` ranges map straight onto the timeline. Otherwise
   offset by the clip's start/inPoint.

## Sequence setup — short-form default (when creating a new sequence)

If the user asks to START a short-form edit in Premiere (no sequence yet), create
the sequence with these defaults:

- **Resolution 1080×1920** (vertical 9:16).
- **Frame rate 30 fps** when the source is ≥30 fps (else 24) — mirrors the Edvid
  short-form fps rule.
- **Maximum Bit Depth: ON** and **Maximum Render Quality: ON** — always check both
  in the sequence settings (`set_sequence_settings` / at creation).
- **Scale for the source→sequence ratio:** a **4K source in the 1080 sequence →
  Motion Scale 50** (the 4K frame is ~2× the sequence's linear size), set per clip
  via `set_clip_scale {clipId, scale:50}`. General rule: `scale% ≈ sequence_width /
  source_display_width × 100` (mind rotated phone footage — the display width is
  the rotated one).

## THE cut recipe — clean silences / assemble keeps (battle-tested)

Given N keep-ranges (from `edl.json`), the timeline currently holds the full
take. Convert keeps → gaps and ripple them out:

1. `set_all_tracks_targeted` → `true`.
2. **Razor every boundary** with `razor_timeline_at_time {time}` (cuts ALL
   video+audio tracks at once). Batch all boundaries — they're commutative.
   Razor snaps to the nearest frame (e.g. 5.20 → `00:00:05:06`); that's fine.
3. `get_full_sequence_info` → read the resulting segments. Each segment now has
   a stable `nodeId` on BOTH the video track and the audio track. Label each
   segment KEEP or GAP by its start/end vs the `edl.json` ranges.
4. **Ripple-delete each GAP with `remove_from_timeline {clipId, deleteMode:
   "ripple"}`.** ⚠️ **The V/A link does NOT propagate the ripple** — removing
   the video nodeId leaves the linked audio behind. You MUST remove BOTH the
   video gap nodeId AND the audio gap nodeId for every gap. Work last-gap →
   first (or just by nodeId; they're stable). After removing all video gaps then
   all audio gaps, the 6 keeps concatenate and V/A realign automatically.
5. `get_full_sequence_info` to confirm: keep count matches, `durationSeconds` ≈
   `edl.total_duration_s`, every clip.start == previous clip.end (no gaps), and
   video/audio starts line up.

### What does NOT work in this CEP context (don't waste calls)

- `extract_selection` → no-op ("Premiere menu command APIs are unavailable").
- `ripple_delete` (the in/out-range expanded op) → accepts but no-ops.
- Sequence in/out + Extract as a shortcut → unavailable.
  → Always use `razor_timeline_at_time` + `remove_from_timeline(ripple)`.

## Grade — ALWAYS ask the color profile first

On a Premiere Phase-1 job, ask which profile the footage was shot in — the answer
picks the treatment:

> "Em qual perfil de cor foi gravado: **Rec-709** (padrão), **Sony S-LOG2**, ou **Apple LOG**?"

- **Rec-709 / standard** → light corrective or none (footage is already display-ready).
- **Sony S-LOG2** → apply THE STANDARD below.
- **Apple LOG** → treatment **TBD** (not yet defined — ask the user for their grade,
  then capture it here the same way S-LOG2 was captured).

### Sony S-LOG2 — THE STANDARD (user-defined, captured 2026-07-06)

Per keep-clip, three parts:

1. **Motion → Scale** set for the source→sequence ratio (see Sequence setup): a 4K
   source in a 1080 sequence → **Scale 50** (`set_clip_scale {clipId, scale:50}`).
2. **Sharpen** effect → **Sharpen Amount 20**.
3. **Lumetri Color → Basic Correction** (all other Lumetri sections left at default):

   | Control | Value |
   |---|---|
   | Exposure | **+0.45** |
   | Contrast | **+64.5** |
   | Highlights | **+24** |
   | Shadows | **−47.7** |
   | Whites / Blacks | 0 / 0 |
   | Saturation | **118.5** (100 = neutral) |
   | White Balance (Temp/Tint) | 0 / 0 (neutral) |

   Shape: lift exposure, strong contrast, recover highlights, deep shadow crush,
   modest saturation bump — expands flat S-LOG2 into a punchy, contrasty look.

### Applying the grade — via ExtendScript (validated 2026-07-06)

Lumetri IS fully scriptable — but ONLY through `execute_extendscript`, not the
wrapper tools:
- `set_effect_property` on a Lumetri param → **no-op**. `apply_effect "Lumetri
  Color"` on a clip that already has one → **spawns empty duplicate Lumetris**
  (junk; remove is no-op). `color_correct` writes only Lumetri's legacy −100..100
  "Correction" slots — can't reach Exposure or the exact Basic-Correction values.
- **`execute_extendscript` works.** Set the Lumetri component's properties
  directly by displayName. Validated: all 5 Basic-Correction params set and read
  back exactly. This is THE way to apply the S-LOG2 grade to every clip.

Recipe — loops the sequence, sets Basic Correction on each clip's FIRST Lumetri
(the FIRST property occurrence of each name IS the Basic Correction control;
"Saturation"/"Contrast" also appear later for Creative/Correction — skip those):

```js
(function(){
  var seq = app.project.activeSequence;
  var t = {"Exposure":0.44789791107178,"Contrast":64.533821105957,
           "Highlights":23.9488124847412,"Shadows":-47.7148056030273,
           "Saturation":118.464347839355};
  var track = seq.videoTracks[0], n=0;
  for (var i=0;i<track.clips.numItems;i++){
    var clip=track.clips[i], lum=null;
    for (var c=0;c<clip.components.numItems;c++)
      if(String(clip.components[c].displayName).indexOf("Lumetri")>=0){lum=clip.components[c];break;}
    if(!lum) continue;
    var done={};
    for (var p=0;p<lum.properties.numItems;p++){
      var pr=lum.properties[p], dn=String(pr.displayName);
      if(t.hasOwnProperty(dn) && !(dn in done)){ pr.setValue(t[dn],true); done[dn]=1; }
    }
    n++;
  }
  return "graded "+n+" clips";
})();
```

A clip with NO Lumetri yet: add one first (`color_correct {clipId}` with any
value creates the component), then run the script to overwrite Basic Correction
with the exact numbers. `Scale` (`set_clip_scale`) and the `Sharpen` effect
(amount 20) are set separately (both scriptable). Real `.cube` LUT: `apply_lut`.
The same ExtendScript pattern reproduces the Apple LOG grade once it's defined.

## Voice master — THE DEFAULT (user-specified, validated 2026-07-06)

`apply_audio_effect_to_all_clips {sequenceId, effectName, parameters}` applies one
effect to every audio clip in one call, and **honors exact parameter values**
(validated: every `valueRequested == valueAfter`). Re-applying a same-named
effect UPDATES its params instead of stacking. Parameter values are Premiere's
internal **normalized 0..1** floats — pass exactly what `list_clip_effects`
returns. `sequenceId` must be the active sequence (`set_active_sequence` first).

**This is the standard voice treatment for Premiere edits** (replaces the old
Vocal Enhancer default). Two effects, applied in this order:

**1. Graphic Equalizer (10 Bands)** — a gentle "smile" curve for a clean, warm,
present voice: lows lifted (warmth), low-mids/mids scooped (kill mud/box),
highs lifted (presence + air). Band 0.5 = 0 dB; bands are 31/63/125/250/500/1k/
2k/4k/8k/16k Hz.

```json
{ "effectName": "Graphic Equalizer (10 Bands)", "parameters": {
  "Accuracy": 0.12099699676037, "Gain": 0.5, "Range": 0.39240506291389,
  "EQ Band 1": 0.57301169633865, "EQ Band 2": 0.54417580366135,
  "EQ Band 3": 0.53625345230103, "EQ Band 4": 0.48103365302086,
  "EQ Band 5": 0.47641825675964, "EQ Band 6": 0.47273010015488,
  "EQ Band 7": 0.47507897019386, "EQ Band 8": 0.5,
  "EQ Band 9": 0.527836561203,  "EQ Band 10": 0.57620537281036 } }
```

**2. Hard Limiter (preset "Medium")** — anti-clip ceiling (~−0.18 dB), input
driven up for loudness, true-peak limiting OFF.

```json
{ "effectName": "Hard Limiter", "parameters": {
  "Maximum Amplitude": 0.98000001907349, "Input Boost": 0.70666700601578,
  "Look-Ahead Time": 0.13333298265934, "Release Time": 0.375,
  "Link Channels": 1, "Decay to Ceiling": 1, "Limit True Peak": 0 } }
```

Apply EQ first, then the limiter. Verify with `list_clip_effects` on one audio
clip (component list should read Volume → Channel Volume → Graphic Equalizer →
Hard Limiter with the values above). Loudness normalization to −14 LUFS is NOT on
the timeline — it's an **export** setting. Let the user hear it in Premiere.

**Caveat:** `remove_effect_by_name` / `remove_effect` are no-ops in this CEP
(like extract_selection). You cannot script-remove a stray effect — apply onto a
clean clip, or have the user delete it in the UI. `Vocal Enhancer` is no longer
part of the default.

## Dynamic zoom — static cut rhythm + animated push-ins (Motion Scale keyframes)

Zoom gives a talking-head edit life. Two layers that COEXIST (user-approved on the
VSL, 2026-07-07):

1. **Static alternating framing on every cut** (the "cut rhythm", NO keyframes):
   consecutive cuts alternate a *static* Scale — **tight ↔ wide** (e.g. **56 ↔ 50**
   over a base-50 fit). Just `setValue` per clip, alternating. Gives an edited
   punch with no motion.
2. **Animated zoom-in ONLY on important/impactful lines** (layered on top): a
   push-in that **arrives and holds** — 3 keys **50 → 56 → 61** (decelerating) over
   ~1.5 s from the clip's IN, then held at the peak to the clip end. ~1 line per
   ~25 s (product reveal, price anchors, guarantee, CTA, kicker). These cuts are
   animated *instead of* static and must NOT disturb the others' IN/OUT alternation
   (don't toggle the alternation counter on them).

Center zoom (no Position keyframes); a ~15% push (50→61 on a 4K-fit-50 clip) stays
face-safe (verified headroom). Captions/overlays live on a separate track and are
unaffected by V1 zoom. The user rejected both "animated in/out on EVERY cut" and
"only the important zoom-ins, everything else flat" — it must be **static
alternation on all + animated push-in on the important few**.

### ⚠️ Keyframe time is SOURCE time, not sequence time (the #1 trap)

`add_keyframe {clipId,"Motion","Scale",time,value}` and ExtendScript `addKey(t)`
interpret `time` in the clip's **source/media** timeline, NOT the sequence. Every
cut is a trim of one long recording, so a keyframe at *sequence* time (e.g.
`clip.start`=0) lands at the **start of the raw uncut source** — before every cut —
and the animation is invisible in the edit (the value just holds inside the cut).
**Place keyframes at `clip.inPoint.seconds → clip.outPoint.seconds`** (source
in/out). Sanity-check against a hand-made keyframe: a clip at sequence 0 with
source in-point 37.83 must key at 37.83, not 0.

### Method notes

- For 90+ clips do it in ONE `execute_extendscript` batch (find the "Motion"
  component → "Scale" property per clip). `add_keyframe`/`get_keyframes` round-trip
  cleanly (times reported in source seconds) for spot checks.
- Clear a clip's keyframes: `scale.setTimeVarying(false); scale.setTimeVarying(true)`.
  Static value: `setTimeVarying(false); setValue(v,1)`. **`setValue` THROWS on a
  time-varying property** — for keyframes use `addKey(t)` + `setValueAtKey(t,v,1)`,
  never `setValue`.
- Base Scale is the source→sequence fit (4K→1080 = **50**), not 100. Oscillate/push
  around it; never below the fit or you get black borders. `clip.nodeId` == the MCP
  clipId.

### UI-refresh gotcha

Script-added keyframes are real — they **render** and persist on save — but
Premiere's **Effect Controls does not redraw** the diamonds live, and the Program
Monitor shows a stale cache when scrubbing. The user reads this as "animation on,
but no keyframe points / nothing happens." **Fix: close & reopen the project (or
File > Revert)** so the saved keyframes load into the UI.

### Verifying zoom renders (isolate scale from the subject moving)

A talking head moves, so two frames at different times differ regardless of scale —
don't trust that as proof. To PROVE a scale/keyframe change renders, compare the
**same timecode** twice: `export_frame` at t with static Scale 50, then set static
68, `export_frame` at the same t — same pose, only scale differs. For keyframes,
compare a keyframed frame at t against a static-50 frame at the same t.

## Inserts — image / animation overlays (Remotion → Premiere)

The user drops inserts onto the cut one at a time (exact content + timecode +
"tela cheia" or "card"). Build each in the Remotion track (`references/shortform.md`
insert style — one Composition per insert, `durationInFrames` = the exact frame
count of the timecode range), preview in **Remotion Studio** for approval, then
render + place.

**Render as ProRes `4444` — NOT `hq`/422.** `--codec=prores --prores-profile=4444`
(comes out `yuv422p12le`, opaque; use it even for opaque full-screen inserts).
Learned the hard way (2026-07-08): one insert rendered `--prores-profile=hq`
(ProRes 422 HQ, `yuv422p10le`) **hung Premiere** — the conform locked the whole
MCP bridge for 30–45s and the clip wouldn't play on the timeline. Re-rendering the
SAME comp as `4444` fixed it. Match the profile of the inserts that already play.
For a true alpha card, use `4444` with a `yuva` pixel format (alpha preserved).

**Organize in a bin named `Inserções`.** All rendered inserts live in one project
bin. `import_media {binName:"Inserções"}` can silently drop the item at the project
root instead (accented bin name not matched), so after importing, verify + relocate
via ExtendScript: find-or-create the bin with `app.project.rootItem.createBin(name)`
and move stray items with `projectItem.moveBin(bin)` (delete a footage item by
moving it into a temp bin and calling `tempBin.deleteBin()` — classic DOM has no
direct item delete). This bin is the standard home for every insert.

**Place on a dedicated overlay track above the captions** (here `Animações` = V4,
one above the caption track) so full-screen inserts cover the captions.
`add_to_timeline {trackIndex, time:<timecode in seconds>, insertMode:"overwrite",
linkAudio:false}` — `linkAudio:false` strips the silent PCM so the narration
underneath survives (result reports `unlinkedAudioRemoved`). Timecode→seconds at
30 fps: `HH*3600 + MM*60 + SS + FF/30`.

**Verify placement with a LIGHT ExtendScript query, not `export_frame`.** Right
after placing a fresh heavy ProRes, `export_frame` races the conform (renders the
layers BELOW → looks empty) and can time out AND freeze the bridge. Instead read
`app.project.activeSequence.videoTracks[n].clips` (name/start/end) to confirm the
clip landed. The bridge can also drop between sessions (reopen panel → Start Bridge)
and the user hand-adds their own B-roll inserts to this track — don't assume it
only holds your rendered inserts.

## Verify (numeric-first, same spirit as verify_cut.py)

- `get_full_sequence_info`: keep count, total duration vs EDL, zero gaps, V/A
  alignment. This is the numeric self-eval.
- `export_frame {sequenceId, time, outputPath, format}`: eyeball the grade on 2+
  clips. ⚠️ **`export_frame` appends `.png` to `outputPath`** — pass the path
  WITHOUT the extension (else you get `name.png.png`). Write frames to the
  scratchpad, then Read them.

## Finish

- `save_project` when the cut+grade+voice are in and verified.
- Then the **phase gate**: show the Program Monitor result, wait for approval
  before any Phase-2 work.

## Phase 2 in Premiere — decide the medium

After approval, ask whether Phase-2 visuals (captions, hook, inserts, graphics)
should be (a) built inside Premiere (real MOGRTs via `import_mogrt`, essential
graphics, `add_text_overlay`) — the server does NOT invent final-quality design
assets, so bring real MOGRTs/LUTs/audio — or (b) done in the Remotion track
(`references/shortform.md` / `longform.md`) and delivered as `final.mp4`. For
branded/ad assemblies the server prefers `assemble_product_spot` /
`build_brand_spot_from_mogrt_and_assets` with a clipPlan.

## Tool cheat-sheet

| Need | Tool |
|---|---|
| Server rules | resource `premiere://config/get_instructions` |
| Health / active seq | `ping`, `get_project_info`, `set_active_sequence` |
| Inspect timeline | `get_full_sequence_info`, `get_timeline_summary`, `list_clip_effects` |
| Source media path | `find_project_item_by_name` |
| Target all tracks | `set_all_tracks_targeted` |
| Cut across layers | `razor_timeline_at_time {time}` |
| Ripple-remove a clip | `remove_from_timeline {clipId, deleteMode:"ripple"}` (video AND audio) |
| Grade | `color_correct {clipId,…}` / `apply_lut` |
| Insert (overlay) | Remotion → ProRes **`4444`** (never `hq`/422) → bin `Inserções` → `add_to_timeline {trackIndex, time, insertMode:"overwrite", linkAudio:false}` |
| Static clip scale | `set_clip_scale` / ExtendScript `setValue` (base fit 4K→1080 = 50) |
| Zoom keyframes | `add_keyframe`/`get_keyframes` (Motion→Scale, **SOURCE time = inPoint→outPoint**) or ExtendScript batch |
| Voice / audio FX | `apply_audio_effect_to_all_clips`, `apply_audio_effect` |
| Frame preview | `export_frame` (path WITHOUT `.png`) |
| Backup / save | `duplicate_sequence`, `save_project` |
| Export | `export_sequence`, `add_to_render_queue` |
