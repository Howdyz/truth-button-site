// Runs the CPU-heavy lamejs MP3 encoding off the main thread. This used to run
// as one uninterrupted loop directly on the page's main thread, which froze
// the entire page (no clicks, no scrolling, no repaint) for the full length
// of the encode — worse the longer the recording. Moving it into a worker
// keeps the page responsive; progress messages let the UI show real movement
// instead of a static "Encoding…" label that looks stuck.
importScripts('lame.min.js');

self.onmessage = function (e) {
  const { left, right, sampleRate, channels } = e.data;
  try {
    const encoder = new lamejs.Mp3Encoder(channels, sampleRate, 320);
    const blockSize = 1152;
    const mp3Chunks = [];
    const total = left.length;
    const progressEvery = blockSize * 200; // ~a couple dozen updates per encode, not one per block

    for (let i = 0; i < total; i += blockSize) {
      const leftChunk = left.subarray(i, i + blockSize);
      const buf = right
        ? encoder.encodeBuffer(leftChunk, right.subarray(i, i + blockSize))
        : encoder.encodeBuffer(leftChunk);
      if (buf.length > 0) mp3Chunks.push(buf);

      if (i % progressEvery === 0) {
        self.postMessage({ type: 'progress', percent: Math.min(99, Math.round((i / total) * 100)) });
      }
    }
    const endBuf = encoder.flush();
    if (endBuf.length > 0) mp3Chunks.push(endBuf);

    // Transfer each chunk's underlying buffer back rather than copying —
    // avoids duplicating what can be several MB of encoded audio.
    self.postMessage({ type: 'done', chunks: mp3Chunks }, mp3Chunks.map((c) => c.buffer));
  } catch (err) {
    self.postMessage({ type: 'error', message: (err && err.message) ? err.message : String(err) });
  }
};
