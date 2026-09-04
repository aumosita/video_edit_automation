<script>
  import { onMount } from "svelte";
  import { submitJob, listModels, ApiError } from "./api.js";

  let { onSubmitted = () => {} } = $props();

  // Persisted in localStorage so the user's last choice is remembered.
  // Use a single object so any future field is automatically loaded.
  const LS_KEY = "veauto.options.v1";
  function loadOptions() {
    if (typeof localStorage === "undefined") return {};
    try {
      const raw = localStorage.getItem(LS_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (_e) { return {}; }
  }
  function saveOptions(opts) {
    if (typeof localStorage === "undefined") return;
    try { localStorage.setItem(LS_KEY, JSON.stringify(opts)); }
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

  let file = $state(null);
  let filename = $state("");
  let model = $state("tiny");
  let language = $state("ko");
  let device = $state("auto");
  let computeType = $state("auto");
  let noiseDb = $state(-30.0);
  // Relative ±dB adjustment applied on top of the auto-derived
  // threshold (only meaningful in "auto" mode).
  let noiseDbOffset = $state(0.0);
  // "auto" derives the silence threshold from the file's own loudness
  // profile; "fixed" uses the numeric Noise (dB) input as-is.
  let thresholdMode = $state("fixed");
  const autoNoiseDb = $derived(thresholdMode === "auto");
  let minSilence = $state(1.5);
  let margin = $state(0.3);
  let minKeepSeconds = $state(0.15);
  let noSilence = $state(false);
  // Subtitle target covers all four outcomes (both/srt/fcpxml/none);
  // the legacy no_subtitles checkbox was removed from the UI.
  let subtitleTarget = $state("both");
  let stylePosition = $state("bottom");
  let styleFont = $state("Apple SD Gothic Neo");
  let styleFontSize = $state(56);
  let styleBold = $state(true);
  let styleColor = $state("#FFFFFF");
  let styleOffsetY = $state(0);
  let styleTemplate = $state("text");
  let subtitleOffset = $state(0.0);
  let projectName = $state("Auto Edit");
  let eventName = $state("veauto");
  let submitting = $state(false);
  let error = $state("");

  let models = $state(["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]);

  onMount(() => {
    const saved = loadOptions();
    if (typeof saved.subtitleTarget === "string"
        && SUBTITLE_TARGETS.some(t => t.v === saved.subtitleTarget)) {
      subtitleTarget = saved.subtitleTarget;
    }
    if (typeof saved.model === "string") model = saved.model;
    if (typeof saved.language === "string") language = saved.language;
    if (typeof saved.device === "string") device = saved.device;
    if (typeof saved.computeType === "string") computeType = saved.computeType;
    if (typeof saved.noSilence === "boolean") noSilence = saved.noSilence;
    if (typeof saved.thresholdMode === "string"
        && (saved.thresholdMode === "auto" || saved.thresholdMode === "fixed")) {
      thresholdMode = saved.thresholdMode;
    }
    if (Number.isFinite(Number(saved.noiseDbOffset))) {
      noiseDbOffset = Number(saved.noiseDbOffset);
    }
  });

  // Restore every option to its built-in default and clear the
  // persisted localStorage snapshot.
  function resetToDefaults() {
    model = "tiny";
    language = "ko";
    device = "auto";
    computeType = "auto";
    noiseDb = -30.0;
    noiseDbOffset = 0.0;
    thresholdMode = "fixed";
    minSilence = 1.5;
    margin = 0.3;
    minKeepSeconds = 0.15;
    noSilence = false;
    subtitleTarget = "both";
    stylePosition = "bottom";
    styleFont = "Apple SD Gothic Neo";
    styleFontSize = 56;
    styleBold = true;
    styleColor = "#FFFFFF";
    styleOffsetY = 0;
    styleTemplate = "text";
    subtitleOffset = 0.0;
    projectName = "Auto Edit";
    eventName = "veauto";
    saveOptions({
      subtitleTarget, model, language, device, computeType, noSilence,
      thresholdMode, noiseDbOffset,
    });
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
        model, language, device, compute_type: computeType,
        noise_db: noiseDb, auto_noise_db: autoNoiseDb,
        noise_db_offset: noiseDbOffset,
        min_silence: minSilence, margin,
        min_keep_seconds: minKeepSeconds,
        no_silence: noSilence,
        subtitle_target: subtitleTarget,
        style_position: stylePosition, style_font: styleFont,
        style_font_size: styleFontSize, style_bold: styleBold,
        style_color: styleColor, style_offset_y: styleOffsetY,
        style_template: styleTemplate, subtitle_offset: subtitleOffset,
        project_name: projectName, event_name: eventName,
      });
      saveOptions({
        subtitleTarget, model, language, device, computeType, noSilence,
        thresholdMode, noiseDbOffset,
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
          <select bind:value={stylePosition}>
            <option value="bottom">bottom</option>
            <option value="center">center</option>
            <option value="top">top</option>
          </select>
        </label>
        <label>Font
          <input type="text" bind:value={styleFont} />
        </label>
        <label>Font size
          <input type="number" min="8" max="400" bind:value={styleFontSize} />
        </label>
        <label>Text color
          <input type="color" bind:value={styleColor} />
        </label>
        <label>Title template
          <select bind:value={styleTemplate}>
            <option value="text">Static (no fade)</option>
            <option value="lower_third">Lower Third (faded)</option>
          </select>
        </label>
        <label>Y offset (px)
          <input type="number" step="10" min="-540" max="540" bind:value={styleOffsetY} />
        </label>
        <label class="checkbox">
          <input type="checkbox" bind:checked={styleBold} /> Bold
        </label>
        <label>Timing offset (s)
          <input type="number" step="0.1" min="-2" max="2" bind:value={subtitleOffset} />
        </label>
      </div>
    </details>

    <details open>
      <summary>Options</summary>
      <div class="grid">
        <label>Model
          <select bind:value={model}>
            {#each models as m}<option value={m}>{m}</option>{/each}
          </select>
        </label>
        <label>Language
          <input type="text" bind:value={language} placeholder="ko / en / auto" />
        </label>
        <label>Device
          <select bind:value={device}>
            <option value="auto">auto</option>
            <option value="cpu">cpu</option>
            <option value="cuda">cuda</option>
            <option value="mps">mps</option>
          </select>
        </label>
        <label>Compute type
          <select bind:value={computeType}>
            <option value="auto">auto</option>
            <option value="int8">int8</option>
            <option value="int8_float16">int8_float16</option>
            <option value="float16">float16</option>
            <option value="float32">float32</option>
          </select>
        </label>
        <label>Threshold mode
          <select bind:value={thresholdMode}>
            <option value="auto">Auto (recommended for quiet recordings)</option>
            <option value="fixed">Fixed (use Noise dB below)</option>
          </select>
        </label>
        {#if autoNoiseDb}
          <label>Threshold adjustment (±dB)
            <input
              type="number" step="1" min="-30" max="30"
              bind:value={noiseDbOffset}
              title="Relative adjustment on the auto-derived threshold. Negative = keep more audio, positive = cut more."
            />
          </label>
        {:else}
          <label>Noise (dB)
            <input type="number" step="1" bind:value={noiseDb} />
          </label>
        {/if}
        <label>Min silence (s)
          <input type="number" step="0.1" min="0.1" bind:value={minSilence} />
        </label>
        <label>Margin (s)
          <input type="number" step="0.05" min="0" bind:value={margin} />
        </label>
        <label>Min keep (s)
          <input type="number" step="0.05" min="0" bind:value={minKeepSeconds} />
        </label>
        <label>Project name
          <input type="text" bind:value={projectName} />
        </label>
        <label>Event name
          <input type="text" bind:value={eventName} />
        </label>
        <label class="checkbox">
          <input type="checkbox" bind:checked={noSilence} /> Skip silence removal
        </label>
        <label>Subtitle target
          <select bind:value={subtitleTarget}>
            {#each SUBTITLE_TARGETS as t}<option value={t.v}>{t.label}</option>{/each}
          </select>
        </label>
      </div>
    </details>

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

