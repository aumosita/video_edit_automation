<script>
  import { onMount } from "svelte";
  import { submitJob, listModels, ApiError } from "./api.js";

  let { onSubmitted = () => {} } = $props();

  // All form fields live in one reactive object so they can be saved to
  // localStorage automatically on every change. The user's last settings
  // survive page reloads and server restarts; only "Reset all settings"
  // returns them to the built-in defaults.
  const LS_KEY = "veauto.options.v1";
  function loadOptions() {
    if (typeof localStorage === "undefined") return {};
    try {
      const raw = localStorage.getItem(LS_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (_e) { return {}; }
  }
  function saveOptions(data) {
    if (typeof localStorage === "undefined") return;
    try { localStorage.setItem(LS_KEY, JSON.stringify(data)); }
    catch (_e) { /* quota or privacy mode - ignore */ }
  }
  // Allowed values for the subtitle target select. Must match the
  // backend Literal in SubtitleConfig / JobOptions.
  const SUBTITLE_TARGETS = [
    { v: "both",   label: "SRT + FCPXML (default)" },
    { v: "srt",    label: "SRT only (no captions in FCPXML)" },
    { v: "fcpxml", label: "FCPXML only (no STT — reserved)" },
    { v: "none",   label: "None (skip subtitles entirely)" },
  ];

  // Built-in defaults; resetToDefaults() restores these exactly.
  const DEFAULTS = {
    model: "tiny",
    language: "ko",
    device: "auto",
    computeType: "auto",
    noiseDb: -30.0,
    // Relative ±dB adjustment applied on top of the auto-derived
    // threshold (only meaningful in "auto" mode).
    noiseDbOffset: 0.0,
    // "auto" derives the silence threshold from the file's own loudness
    // profile; "fixed" uses the numeric Noise (dB) input as-is.
    thresholdMode: "fixed",
    minSilence: 1.5,
    margin: 0.3,
    minKeepSeconds: 0.15,
    noSilence: false,
    // Subtitle target covers all four outcomes (both/srt/fcpxml/none);
    // the legacy no_subtitles checkbox was removed from the UI.
    subtitleTarget: "both",
    stylePosition: "bottom",
    styleFont: "Apple SD Gothic Neo",
    styleFontSize: 56,
    styleBold: true,
    styleColor: "#FFFFFF",
    styleOffsetY: 0,
    styleTemplate: "text",
    subtitleOffset: 0.0,
    projectName: "Auto Edit",
    eventName: "veauto",
    // Line breaking: max chars per display line, max display lines per
    // cue, and whether a sentence boundary starts a new subtitle.
    maxCharsPerLine: 42,
    maxLines: 2,
    splitSentence: true,
  };

  let opts = $state({ ...DEFAULTS });
  const autoNoiseDb = $derived(opts.thresholdMode === "auto");

  let file = $state(null);
  let filename = $state("");
  let submitting = $state(false);
  let error = $state("");

  let models = $state(["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]);

  // Whether the saved snapshot has been loaded; guards the auto-save
  // effect so defaults are never written over stored values before
  // they are restored.
  let loaded = $state(false);

  onMount(() => {
    const saved = loadOptions();
    for (const [key, def] of Object.entries(DEFAULTS)) {
      const v = saved[key];
      if (v === undefined || v === null) continue;
      if (typeof def === "boolean") {
        if (typeof v === "boolean") opts[key] = v;
      } else if (typeof def === "number") {
        const n = Number(v);
        if (Number.isFinite(n)) opts[key] = n;
      } else if (typeof v === "string" && v !== "") {
        opts[key] = v;
      }
    }
    // Validate enum-ish fields against their allowed sets.
    if (!SUBTITLE_TARGETS.some((t) => t.v === opts.subtitleTarget)) {
      opts.subtitleTarget = DEFAULTS.subtitleTarget;
    }
    if (opts.thresholdMode !== "auto" && opts.thresholdMode !== "fixed") {
      opts.thresholdMode = DEFAULTS.thresholdMode;
    }
    if (!["bottom", "center", "top"].includes(opts.stylePosition)) {
      opts.stylePosition = DEFAULTS.stylePosition;
    }
    if (!["text", "lower_third"].includes(opts.styleTemplate)) {
      opts.styleTemplate = DEFAULTS.styleTemplate;
    }
    loaded = true;
  });

  // Auto-save on every change so nothing is lost on refresh/reconnect.
  // $state.snapshot reads all properties, making the effect track them.
  $effect(() => {
    const snapshot = $state.snapshot(opts);
    if (loaded) saveOptions(snapshot);
  });

  // Restore every option to its built-in default and overwrite the
  // persisted localStorage snapshot (the auto-save effect picks this
  // up once `loaded` flips inside the reset call chain).
  function resetToDefaults() {
    opts = { ...DEFAULTS };
    saveOptions({ ...DEFAULTS });
    loadedFrom = "";
  }

  // Re-populate the form from a previous job's options (JobOptions JSON
  // as returned by the API). Any field the record doesn't carry falls
  // back to the built-in default. The usual validation on load applies,
  // and saving happens through the existing auto-save effect, so the
  // restored settings survive a reload like any other edit.
  let loadedFrom = $state("");

  export function applySettings(jobOptions = {}, sourceName = "") {
    const next = { ...DEFAULTS };
    const map = {
      model: "model",
      language: "language",
      device: "device",
      compute_type: "computeType",
      noise_db: "noiseDb",
      noise_db_offset: "noiseDbOffset",
      min_silence: "minSilence",
      margin: "margin",
      min_keep_seconds: "minKeepSeconds",
      no_silence: "noSilence",
      subtitle_target: "subtitleTarget",
      style_position: "stylePosition",
      style_font: "styleFont",
      style_font_size: "styleFontSize",
      style_bold: "styleBold",
      style_color: "styleColor",
      style_offset_y: "styleOffsetY",
      style_template: "styleTemplate",
      subtitle_offset: "subtitleOffset",
      style_max_chars: "maxCharsPerLine",
      style_max_lines: "maxLines",
      style_split_sentence: "splitSentence",
      project_name: "projectName",
      event_name: "eventName",
    };
    for (const [srcKey, dstKey] of Object.entries(map)) {
      const v = jobOptions[srcKey];
      if (v === undefined || v === null) continue;
      const def = DEFAULTS[dstKey];
      if (typeof def === "boolean") {
        if (typeof v === "boolean") next[dstKey] = v;
      } else if (typeof def === "number") {
        const n = Number(v);
        if (Number.isFinite(n)) next[dstKey] = n;
      } else if (typeof v === "string" && v !== "") {
        next[dstKey] = v;
      }
    }
    // auto_noise_db selects the threshold mode.
    if (typeof jobOptions.auto_noise_db === "boolean") {
      next.thresholdMode = jobOptions.auto_noise_db ? "auto" : "fixed";
    }
    // Legacy records used the no_subtitles boolean instead of a target.
    if (
      jobOptions.subtitle_target == null
      && typeof jobOptions.no_subtitles === "boolean"
      && jobOptions.no_subtitles
    ) {
      next.subtitleTarget = "none";
    }
    // Enum fields must still be valid after the round-trip.
    if (!SUBTITLE_TARGETS.some((t) => t.v === next.subtitleTarget)) {
      next.subtitleTarget = DEFAULTS.subtitleTarget;
    }
    if (next.thresholdMode !== "auto" && next.thresholdMode !== "fixed") {
      next.thresholdMode = DEFAULTS.thresholdMode;
    }
    if (!["bottom", "center", "top"].includes(next.stylePosition)) {
      next.stylePosition = DEFAULTS.stylePosition;
    }
    if (!["text", "lower_third"].includes(next.styleTemplate)) {
      next.styleTemplate = DEFAULTS.styleTemplate;
    }
    opts = next;
    loadedFrom = sourceName ? `Loaded settings from ${sourceName}` : "Loaded settings";
    setTimeout(() => { loadedFrom = ""; }, 4000);
  }

  async function refreshModels() {
    try {
      const remote = await listModels();
      if (Array.isArray(remote) && remote.length) {
        models = remote;
      }
    } catch (_e) { /* keep static list */ }
  }
  refreshModels();

  function onFile(e) {
    const t = e.target;
    if (t.files && t.files.length) {
      file = t.files[0];
      filename = file.name;
    }
  }

  function onDrop(e) {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      file = e.dataTransfer.files[0];
      filename = file.name;
    }
  }
  function onDragOver(e) { e.preventDefault(); }

  async function onSubmit(e) {
    e.preventDefault();
    if (!file || submitting) return;
    error = "";
    submitting = true;
    try {
      const job = await submitJob(file, {
        model: opts.model, language: opts.language, device: opts.device,
        compute_type: opts.computeType,
        noise_db: opts.noiseDb, auto_noise_db: autoNoiseDb,
        noise_db_offset: opts.noiseDbOffset,
        min_silence: opts.minSilence, margin: opts.margin,
        min_keep_seconds: opts.minKeepSeconds,
        no_silence: opts.noSilence,
        subtitle_target: opts.subtitleTarget,
        style_position: opts.stylePosition, style_font: opts.styleFont,
        style_font_size: opts.styleFontSize, style_bold: opts.styleBold,
        style_color: opts.styleColor, style_offset_y: opts.styleOffsetY,
        style_template: opts.styleTemplate,
        subtitle_offset: opts.subtitleOffset,
        style_max_chars: opts.maxCharsPerLine,
        style_max_lines: opts.maxLines,
        style_split_sentence: opts.splitSentence,
        project_name: opts.projectName, event_name: opts.eventName,
      });
      onSubmitted(job);
      // Reset file input but keep options
      file = null;
      filename = "";
      const input = document.getElementById("file-input");
      if (input) input.value = "";
    } catch (e) {
      error = e instanceof ApiError
        ? `API error: ${e.status} ${e.body || e.statusText}`
        : `Submit failed: ${e?.message || e}`;
    } finally {
      submitting = false;
    }
  }
</script>

<section class="card">
  <h2>New job</h2>
  <form onsubmit={onSubmit}>
    <div
      class="dropzone"
      class:has-file={!!filename}
      ondrop={onDrop}
      ondragover={onDragOver}
      role="button"
      tabindex="0"
    >
      <input id="file-input" type="file" accept="video/*,audio/*" onchange={onFile} />
      <p class="dz-text">
        {#if filename}
          <strong>{filename}</strong>
        {:else}
          Drag &amp; drop a video, or
          <label for="file-input" class="link">browse</label>
        {/if}
      </p>
    </div>

    <details open>
      <summary>Subtitle style</summary>
      <div class="grid">
        <label>Position
          <select bind:value={opts.stylePosition}>
            <option value="bottom">bottom</option>
            <option value="center">center</option>
            <option value="top">top</option>
          </select>
        </label>
        <label>Font
          <input type="text" bind:value={opts.styleFont} />
        </label>
        <label>Font size
          <input type="number" min="8" max="400" bind:value={opts.styleFontSize} />
        </label>
        <label>Text color
          <input type="color" bind:value={opts.styleColor} />
        </label>
        <label>Title template
          <select bind:value={opts.styleTemplate}>
            <option value="text">Static (no fade)</option>
            <option value="lower_third">Lower Third (faded)</option>
          </select>
        </label>
        <label>Y offset (px)
          <input type="number" step="10" min="-540" max="540" bind:value={opts.styleOffsetY} />
        </label>
        <label class="checkbox">
          <input type="checkbox" bind:checked={opts.styleBold} /> Bold
        </label>
        <label>Timing offset (s)
          <input type="number" step="0.1" min="-2" max="2" bind:value={opts.subtitleOffset} />
        </label>
        <label>Max chars / line
          <input type="number" min="5" max="200" bind:value={opts.maxCharsPerLine}
                 title="Longer text wraps onto the next display line (up to Max lines)." />
        </label>
        <label>Max lines
          <input type="number" min="1" max="4" bind:value={opts.maxLines}
                 title="Display lines per subtitle cue." />
        </label>
        <label class="checkbox">
          <input type="checkbox" bind:checked={opts.splitSentence} /> Break subtitles at sentence boundaries
        </label>
      </div>
    </details>

    <details open>
      <summary>Options</summary>
      <div class="grid">
        <label>Model
          <select bind:value={opts.model}>
            {#each models as m}<option value={m}>{m}</option>{/each}
          </select>
        </label>
        <label>Language
          <input type="text" bind:value={opts.language} placeholder="ko / en / auto" />
        </label>
        <label>Device
          <select bind:value={opts.device}>
            <option value="auto">auto</option>
            <option value="cpu">cpu</option>
            <option value="cuda">cuda</option>
            <option value="metal">metal (Apple GPU)</option>
            <option value="mps">mps → metal</option>
          </select>
        </label>
        <label>Compute type
          <select bind:value={opts.computeType}>
            <option value="auto">auto</option>
            <option value="int8">int8</option>
            <option value="int8_float16">int8_float16</option>
            <option value="float16">float16</option>
            <option value="float32">float32</option>
          </select>
        </label>
        <label>Threshold mode
          <select bind:value={opts.thresholdMode}>
            <option value="auto">Auto (recommended for quiet recordings)</option>
            <option value="fixed">Fixed (use Noise dB below)</option>
          </select>
        </label>
        {#if autoNoiseDb}
          <label>Threshold adjustment (±dB)
            <input
              type="number" step="1" min="-30" max="30"
              bind:value={opts.noiseDbOffset}
              title="Relative adjustment on the auto-derived threshold. Negative = keep more audio, positive = cut more."
            />
          </label>
        {:else}
          <label>Noise (dB)
            <input type="number" step="1" bind:value={opts.noiseDb} />
          </label>
        {/if}
        <label>Min silence (s)
          <input type="number" step="0.1" min="0.1" bind:value={opts.minSilence} />
        </label>
        <label>Margin (s)
          <input type="number" step="0.05" min="0" bind:value={opts.margin} />
        </label>
        <label>Min keep (s)
          <input type="number" step="0.05" min="0" bind:value={opts.minKeepSeconds} />
        </label>
        <label>Project name
          <input type="text" bind:value={opts.projectName} />
        </label>
        <label>Event name
          <input type="text" bind:value={opts.eventName} />
        </label>
        <label class="checkbox">
          <input type="checkbox" bind:checked={opts.noSilence} /> Skip silence removal
        </label>
        <label>Subtitle target
          <select bind:value={opts.subtitleTarget}>
            {#each SUBTITLE_TARGETS as t}<option value={t.v}>{t.label}</option>{/each}
          </select>
        </label>
      </div>
    </details>

    {#if loadedFrom}
      <p class="notice">{loadedFrom}</p>
    {/if}

    {#if error}
      <p class="error">{error}</p>
    {/if}

    <div class="actions">
      <button class="primary" type="submit" disabled={!file || submitting}>
        {submitting ? "Submitting…" : "Submit job"}
      </button>
      <button class="secondary" type="button" onclick={resetToDefaults}>
        Reset all settings
      </button>
    </div>
  </form>
</section>

<style>
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.2rem;
    margin-bottom: 1.5rem;
  }
  h2 { margin: 0 0 0.8rem; font-size: 1.1rem; }
  .dropzone {
    border: 2px dashed var(--border);
    border-radius: 6px;
    padding: 1.5rem;
    text-align: center;
    transition: border-color 0.2s;
    background: var(--bg);
  }
  .dropzone.has-file { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 5%, var(--bg)); }
  .dropzone input[type="file"] { display: none; }
  .dz-text { margin: 0; color: var(--muted); }
  .has-file .dz-text { color: var(--fg); }
  .link { color: var(--accent); cursor: pointer; }
  details { margin: 1rem 0; }
  summary { cursor: pointer; color: var(--muted); }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 0.6rem 1rem;
    margin-top: 0.8rem;
  }
  label { display: flex; flex-direction: column; font-size: 0.85rem; color: var(--muted); gap: 0.25rem; }
  label.checkbox { flex-direction: row; align-items: center; gap: 0.5rem; }
  input, select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.4rem 0.5rem;
    color: var(--fg);
    font: inherit;
  }
  input:focus, select:focus {
    outline: 2px solid var(--accent);
    outline-offset: 0;
  }
  button.primary {
    background: var(--accent);
    color: white;
    border: 0;
    padding: 0.6rem 1.2rem;
    border-radius: 6px;
    font-weight: 600;
    cursor: pointer;
  }
  button.primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .error {
    color: var(--danger);
    background: color-mix(in srgb, var(--danger) 10%, var(--bg));
    padding: 0.5rem 0.8rem;
    border-radius: 4px;
    margin: 0.6rem 0;
  }
  .actions {
    display: flex;
    gap: 0.6rem;
    align-items: center;
  }
  button.secondary {
    background: var(--bg);
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.5rem 0.9rem;
    cursor: pointer;
  }
  button.secondary:hover:not(:disabled) {
    color: var(--text);
    border-color: var(--accent);
  }
</style>

