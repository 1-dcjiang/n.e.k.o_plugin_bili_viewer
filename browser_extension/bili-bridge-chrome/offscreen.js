// B站接入桥 (Chrome) —— offscreen 声音采集（v0.7.11）
// 接收 background 转发的 tabCapture streamId，用 AudioContext 采集标签页音频，
// 每段约 8 秒组装成 16kHz/16bit/单声道 wav（base64）推送到本机插件
// http://127.0.0.1:8765/api/extension-audio；数据只走本机回环，不上外网。
// 一次性授权（扩展弹层开启声音）后持续静默运行，不抢界面、不播报。

const PANEL_URL = "http://127.0.0.1:8765";
const SEGMENT_SECONDS = 8;      // 每段时长（秒）
const SAMPLE_RATE = 16000;      // 统一重采样到 16k，wav 与本地 ASR 直接兼容
const MIN_BYTES = 1600;         // 小于该字节数视为无声段，跳过推送

let audioContext = null;
let sourceNode = null;
let processorNode = null;
let stream = null;
let pcmBuffer = [];             // 累积的 Float32 采样
let segmentTimer = null;
let collecting = false;
let seq = 0;

function buildWavBase64(samples) {
  // Float32 → Int16 PCM wav（44 字节头 + 数据），返回 base64 字符串
  const n = samples.length;
  const dataLen = n * 2;
  const buf = new ArrayBuffer(44 + dataLen);
  const view = new DataView(buf);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataLen, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);          // PCM
  view.setUint16(22, 1, true);          // mono
  view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, "data");
  view.setUint32(40, dataLen, true);
  let off = 44;
  for (let i = 0; i < n; i++) {
    let v = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(off, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    off += 2;
  }
  let bin = "";
  const bytes = new Uint8Array(buf);
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

async function pushSegment(samples) {
  if (!samples || samples.length < 1) return;
  const bytes = samples.length * 2;
  if (bytes < MIN_BYTES) return;                       // 无声段：跳过，节省面板负载
  seq += 1;
  const b64 = buildWavBase64(samples);
  try {
    await fetch(`${PANEL_URL}/api/extension-audio`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        audio: `data:audio/wav;base64,${b64}`,
        sample_rate: SAMPLE_RATE,
        seq: seq,
        at: Date.now(),
      }),
    });
  } catch (e) { /* 插件未运行：静默，下段继续尝试 */ }
}

function flushSegment() {
  if (!collecting) return;
  const samples = pcmBuffer;
  pcmBuffer = [];
  pushSegment(samples);
}

function onAudioProcess(e) {
  const input = e.inputBuffer.getChannelData(0);
  if (!collecting) return;
  // 拷贝当前帧（getChannelData 返回可被复用的缓冲，不能持有引用）
  for (let i = 0; i < input.length; i++) pcmBuffer.push(input[i]);
}

async function startCapture(streamId) {
  stopCapture();
  collecting = true;
  stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId,
      },
    },
  });
  audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
  sourceNode = audioContext.createMediaStreamSource(stream);
  processorNode = audioContext.createScriptProcessor(4096, 1, 1);
  sourceNode.connect(processorNode);
  processorNode.connect(audioContext.destination);
  processorNode.onaudioprocess = onAudioProcess;
  segmentTimer = setInterval(flushSegment, SEGMENT_SECONDS * 1000);
  // 通知 background 已就绪（供状态回显）
  chrome.runtime.sendMessage({ type: "audio-state", running: true }).catch(() => {});
}

function stopCapture() {
  collecting = false;
  if (segmentTimer) { clearInterval(segmentTimer); segmentTimer = null; }
  pcmBuffer = [];
  if (processorNode) { try { processorNode.disconnect(); } catch (e) {} processorNode = null; }
  if (sourceNode) { try { sourceNode.disconnect(); } catch (e) {} sourceNode = null; }
  if (audioContext) { try { audioContext.close(); } catch (e) {} audioContext = null; }
  if (stream) { try { stream.getTracks().forEach((t) => t.stop()); } catch (e) {} stream = null; }
  chrome.runtime.sendMessage({ type: "audio-state", running: false }).catch(() => {});
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "start-audio" && msg.streamId) {
    startCapture(msg.streamId).catch(() => stopCapture());
    return true;
  }
  if (msg && msg.type === "stop-audio") {
    stopCapture();
    return true;
  }
});
