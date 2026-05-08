/**
 * Device Fingerprint SDK — Client-side collection of browser/device signals.
 *
 * Collects: Canvas hash, WebGL renderer, screen dims, hardware specs,
 * typing cadence, mouse entropy, and environment flags.
 *
 * Usage:
 *   import { collectFingerprint, sendFingerprint } from "@/lib/fingerprint";
 *   const fp = await collectFingerprint();
 *   await sendFingerprint(userId, sessionId, fp);
 */

export interface RawFingerprint {
  user_agent: string;
  platform: string;
  language: string;
  languages: string[];
  timezone_offset: number;
  timezone_name: string;
  screen_width: number;
  screen_height: number;
  color_depth: number;
  device_memory: number | null;
  hardware_concurrency: number | null;
  touch_support: boolean;
  max_touch_points: number;
  canvas_hash: string;
  webgl_hash: string;
  webgl_vendor: string;
  webgl_renderer: string;
  audio_hash: string;
  connection_type: string;
  cookies_enabled: boolean;
  local_storage: boolean;
  session_storage: boolean;
  indexed_db: boolean;
  do_not_track: boolean;
  ad_blocker: boolean;
  webdriver: boolean;
  plugins_count: number;
  pdf_viewer: boolean;
}

async function hashString(str: string): Promise<string> {
  const buf = new TextEncoder().encode(str);
  const hash = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(hash))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
}

function getCanvasHash(): Promise<string> {
  return new Promise((resolve) => {
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 200;
      canvas.height = 50;
      const ctx = canvas.getContext("2d");
      if (!ctx) return resolve("");
      ctx.textBaseline = "top";
      ctx.font = "14px Arial";
      ctx.fillStyle = "#f60";
      ctx.fillRect(125, 1, 62, 20);
      ctx.fillStyle = "#069";
      ctx.fillText("AFDS fp 🔒", 2, 15);
      ctx.fillStyle = "rgba(102,204,0,0.7)";
      ctx.fillText("AFDS fp 🔒", 4, 17);
      hashString(canvas.toDataURL()).then(resolve);
    } catch {
      resolve("");
    }
  });
}

function getWebGLInfo(): { hash: string; vendor: string; renderer: string } {
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    if (!gl || !(gl instanceof WebGLRenderingContext)) return { hash: "", vendor: "", renderer: "" };
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    const vendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : "";
    const renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : "";
    return { hash: "", vendor, renderer }; // hash computed after
  } catch {
    return { hash: "", vendor: "", renderer: "" };
  }
}

function getAudioHash(): Promise<string> {
  return new Promise((resolve) => {
    try {
      const ctx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
      const oscillator = ctx.createOscillator();
      const analyser = ctx.createAnalyser();
      const gain = ctx.createGain();
      const proc = ctx.createScriptProcessor(4096, 1, 1);

      gain.gain.value = 0;
      oscillator.type = "triangle";
      oscillator.frequency.value = 10000;
      oscillator.connect(analyser);
      analyser.connect(proc);
      proc.connect(gain);
      gain.connect(ctx.destination);
      oscillator.start(0);

      const data = new Float32Array(analyser.frequencyBinCount);
      analyser.getFloatFrequencyData(data);
      const sum = data.reduce((a, b) => a + Math.abs(b), 0);
      oscillator.stop();
      ctx.close();
      hashString(String(sum)).then(resolve);
    } catch {
      resolve("");
    }
  });
}

function detectAdBlocker(): boolean {
  try {
    const el = document.createElement("div");
    el.className = "adsbox ad-banner";
    el.style.position = "absolute";
    el.style.left = "-9999px";
    document.body.appendChild(el);
    const blocked = el.offsetHeight === 0;
    document.body.removeChild(el);
    return blocked;
  } catch {
    return false;
  }
}

export async function collectFingerprint(): Promise<RawFingerprint> {
  const [canvasHash, audioHash] = await Promise.all([getCanvasHash(), getAudioHash()]);
  const webgl = getWebGLInfo();
  const webglHash = await hashString(`${webgl.vendor}|${webgl.renderer}`);

  const nav = navigator as Navigator & {
    deviceMemory?: number;
    connection?: { effectiveType?: string };
    pdfViewerEnabled?: boolean;
  };

  return {
    user_agent: navigator.userAgent,
    platform: navigator.platform,
    language: navigator.language,
    languages: [...navigator.languages],
    timezone_offset: new Date().getTimezoneOffset(),
    timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen_width: screen.width,
    screen_height: screen.height,
    color_depth: screen.colorDepth,
    device_memory: nav.deviceMemory ?? null,
    hardware_concurrency: navigator.hardwareConcurrency ?? null,
    touch_support: "ontouchstart" in window,
    max_touch_points: navigator.maxTouchPoints || 0,
    canvas_hash: canvasHash,
    webgl_hash: webglHash,
    webgl_vendor: webgl.vendor,
    webgl_renderer: webgl.renderer,
    audio_hash: audioHash,
    connection_type: nav.connection?.effectiveType || "",
    cookies_enabled: navigator.cookieEnabled,
    local_storage: (() => { try { return !!localStorage; } catch { return false; } })(),
    session_storage: (() => { try { return !!sessionStorage; } catch { return false; } })(),
    indexed_db: !!window.indexedDB,
    do_not_track: navigator.doNotTrack === "1",
    ad_blocker: detectAdBlocker(),
    webdriver: !!(navigator as Navigator & { webdriver?: boolean }).webdriver,
    plugins_count: navigator.plugins?.length || 0,
    pdf_viewer: nav.pdfViewerEnabled ?? false,
  };
}

/**
 * Send a collected fingerprint to the AFDS backend.
 */
export async function sendFingerprint(
  userId: string,
  sessionId: string,
  fp: RawFingerprint,
  extra?: { ip_address?: string; typing_cadence_ms?: number[]; mouse_movements?: number; mouse_entropy?: number }
): Promise<unknown> {
  const body = {
    user_id: userId,
    session_id: sessionId,
    ...fp,
    ip_address: extra?.ip_address || "",
    typing_cadence_ms: extra?.typing_cadence_ms || [],
    mouse_movements: extra?.mouse_movements || 0,
    mouse_entropy: extra?.mouse_entropy || 0,
    scroll_behavior: "",
  };

  const res = await fetch("/api/v1/device/collect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}
