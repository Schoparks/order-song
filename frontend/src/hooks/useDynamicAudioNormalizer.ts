import { useCallback, useRef } from "react";
import { clamp } from "../lib/time";
import { canUseWebAudioForSource } from "./audioSource";

interface AudioGraph {
  ctx: AudioContext;
  source: MediaElementAudioSourceNode;
  analyser: AnalyserNode;
  normalizer: GainNode;
  compressor: DynamicsCompressorNode;
  volume: GainNode;
  samples: Float32Array<ArrayBuffer>;
  rafId: number | null;
  smoothedGain: number;
}

const TARGET_RMS = 0.16;
const SILENCE_RMS = 0.01;
const MIN_NORMALIZER_GAIN = 0.65;
const MAX_NORMALIZER_GAIN = 2.4;
const GAIN_SMOOTHING = 0.08;

type AudioContextWindow = typeof window & { webkitAudioContext?: typeof AudioContext };

function audioContextCtor(): typeof AudioContext | undefined {
  return window.AudioContext || (window as AudioContextWindow).webkitAudioContext;
}

function disconnect(node: AudioNode): void {
  try {
    node.disconnect();
  } catch {
    // Already disconnected.
  }
}

function stopRmsFollower(graph: AudioGraph): void {
  if (graph.rafId != null) {
    window.cancelAnimationFrame(graph.rafId);
    graph.rafId = null;
  }
  graph.smoothedGain = 1;
  graph.normalizer.gain.setTargetAtTime(1, graph.ctx.currentTime, 0.08);
}

function configureCompressor(compressor: DynamicsCompressorNode): void {
  compressor.threshold.value = -30;
  compressor.knee.value = 18;
  compressor.ratio.value = 12;
  compressor.attack.value = 0.004;
  compressor.release.value = 0.22;
}

function startRmsFollower(graph: AudioGraph): void {
  if (graph.rafId != null) return;

  const tick = () => {
    graph.analyser.getFloatTimeDomainData(graph.samples);
    let sumSquares = 0;
    for (const sample of graph.samples) sumSquares += sample * sample;
    const rms = Math.sqrt(sumSquares / graph.samples.length);

    if (Number.isFinite(rms) && rms > SILENCE_RMS) {
      const desiredGain = clamp(TARGET_RMS / rms, MIN_NORMALIZER_GAIN, MAX_NORMALIZER_GAIN);
      graph.smoothedGain += (desiredGain - graph.smoothedGain) * GAIN_SMOOTHING;
      graph.normalizer.gain.setTargetAtTime(graph.smoothedGain, graph.ctx.currentTime, 0.12);
    }

    graph.rafId = window.requestAnimationFrame(tick);
  };

  graph.rafId = window.requestAnimationFrame(tick);
}

function connectGraph(graph: AudioGraph, normalizerEnabled: boolean): void {
  disconnect(graph.source);
  disconnect(graph.analyser);
  disconnect(graph.normalizer);
  disconnect(graph.compressor);
  disconnect(graph.volume);
  if (normalizerEnabled) {
    graph.source.connect(graph.analyser);
    graph.analyser.connect(graph.normalizer);
    graph.normalizer.connect(graph.compressor);
    graph.compressor.connect(graph.volume);
    startRmsFollower(graph);
  } else {
    stopRmsFollower(graph);
    graph.source.connect(graph.volume);
  }
  graph.volume.connect(graph.ctx.destination);
}

export function useDynamicAudioNormalizer(audio: HTMLAudioElement) {
  const graphRef = useRef<AudioGraph | null>(null);

  const ensureGraph = useCallback(() => {
    if (graphRef.current) return graphRef.current;
    const AudioCtx = audioContextCtor();
    if (!AudioCtx) return null;
    const ctx = new AudioCtx();
    const source = ctx.createMediaElementSource(audio);
    const analyser = ctx.createAnalyser();
    const normalizer = ctx.createGain();
    const compressor = ctx.createDynamicsCompressor();
    const volume = ctx.createGain();
    analyser.fftSize = 2048;
    configureCompressor(compressor);
    graphRef.current = {
      ctx,
      source,
      analyser,
      normalizer,
      compressor,
      volume,
      samples: new Float32Array(new ArrayBuffer(analyser.fftSize * Float32Array.BYTES_PER_ELEMENT)),
      rafId: null,
      smoothedGain: 1,
    };
    return graphRef.current;
  }, [audio]);

  const applyOutputVolume = useCallback(
    (normalizerEnabled: boolean, volume: number, sourceUrl: string | null) => {
      const userVolume = clamp(volume, 0, 100) / 100;
      const graphAllowed = canUseWebAudioForSource(sourceUrl);
      const shouldUseGraph = graphAllowed && (normalizerEnabled || graphRef.current);

      if (!shouldUseGraph) {
        audio.volume = userVolume;
        return false;
      }

      const graph = ensureGraph();
      if (!graph) {
        audio.volume = userVolume;
        return false;
      }

      audio.volume = 1;
      connectGraph(graph, normalizerEnabled);
      const now = graph.ctx.currentTime;
      graph.volume.gain.cancelScheduledValues(now);
      graph.volume.gain.setTargetAtTime(userVolume, now, 0.05);
      return true;
    },
    [audio, ensureGraph],
  );

  const unlockAudioGraph = useCallback(async () => {
    const graph = graphRef.current;
    if (graph?.ctx.state === "suspended") {
      await graph.ctx.resume().catch(() => {});
    }
  }, []);

  const cleanupAudioGraph = useCallback(() => {
    const graph = graphRef.current;
    if (!graph) return;
    stopRmsFollower(graph);
    disconnect(graph.source);
    disconnect(graph.analyser);
    disconnect(graph.normalizer);
    disconnect(graph.compressor);
    disconnect(graph.volume);
  }, []);

  return { applyOutputVolume, unlockAudioGraph, cleanupAudioGraph };
}
