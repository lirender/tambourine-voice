/**
 * Meeting surface for the menubar dropdown home view.
 *
 * Renders, top to bottom, only what's relevant right now:
 *  - a persistent RECORDING banner while capturing (DESIGN.md: red = recording)
 *  - a "Name speakers" mapping panel when a finished meeting needs it (amber)
 *  - transcribing / GB10-offline rows for in-flight or degraded jobs
 *
 * State lives in meetingStore; this component owns the stop→transcribe→poll
 * orchestration and the elapsed-time ticker.
 */

import { Box, Button, Group, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { useEffect, useState } from "react";
import { tauriAPI } from "../lib/tauri";
import {
	type FinishedMeeting,
	type TranscriptionStatus,
	useMeetingStore,
} from "../stores/meetingStore";
import { SpeakerMapping } from "./SpeakerMapping";

// DESIGN.md semantic tokens.
const RECORDING = "#FA5252";
const ATTENTION = "#E8A33D";
const SURFACE_RAISED = "#1A1A1A";
const BORDER = "#2C2E33";

function fmtElapsed(ms: number): string {
	const total = Math.max(0, Math.floor(ms / 1000));
	const m = Math.floor(total / 60);
	const s = total % 60;
	return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Poll a transcription job to completion and resolve to the final result. */
async function pollJob(
	serverUrl: string,
	jobId: string,
): Promise<{ status: TranscriptionStatus; segments: any[]; speakers: string[] }> {
	for (;;) {
		const res = await fetch(`${serverUrl}/api/meeting/job/${jobId}`);
		const data = await res.json();
		if (data.status === "complete") {
			const r = data.result ?? {};
			const speakers: string[] = r.speakers ?? [];
			// No speakers => GB10 was unreachable and we fell back to local MLX.
			return {
				status: speakers.length > 0 ? "done" : "offline",
				segments: r.segments ?? [],
				speakers,
			};
		}
		if (data.status === "error") {
			return { status: "error", segments: [], speakers: [] };
		}
		await new Promise((r) => setTimeout(r, 3000));
	}
}

function RecordingBanner() {
	const recording = useMeetingStore((s) => s.recording);
	const stopRecording = useMeetingStore((s) => s.stopRecording);
	const addTranscribing = useMeetingStore((s) => s.addTranscribing);
	const completeTranscription = useMeetingStore((s) => s.completeTranscription);
	const [now, setNow] = useState(Date.now());

	useEffect(() => {
		if (!recording) return;
		const id = setInterval(() => setNow(Date.now()), 1000);
		return () => clearInterval(id);
	}, [recording]);

	if (!recording) return null;
	const title = recording.meeting?.title ?? "Meeting";

	const onStop = async () => {
		const { wavPath, meeting } = recording;
		const id = meeting?.id ?? `rec-${recording.startedAt}`;
		stopRecording();
		try {
			const finishedWav = await invoke<string>("stop_meeting_capture");
			addTranscribing({ id, meeting, title, wavPath: finishedWav || wavPath });
			const serverUrl = await tauriAPI.getServerUrl();
			const res = await fetch(`${serverUrl}/api/meeting/transcribe`, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ wav_path: finishedWav || wavPath }),
			});
			const { job_id } = await res.json();
			const result = await pollJob(serverUrl, job_id);
			completeTranscription(id, result);
		} catch (e) {
			completeTranscription(id, { status: "error", segments: [], speakers: [] });
			notifications.show({
				title: "Couldn't finish recording",
				message: String(e),
				color: "red",
			});
		}
	};

	return (
		<Box
			style={{
				background: SURFACE_RAISED,
				border: `1px solid ${RECORDING}55`,
				borderRadius: 8,
				padding: "12px 14px",
			}}
		>
			<Group justify="space-between" wrap="nowrap">
				<Group gap="sm" wrap="nowrap">
					<span className="recording-dot" aria-hidden />
					<div>
						<Text size="sm" fw={600}>
							<Text span c={RECORDING} inherit>
								Recording
							</Text>{" "}
							— {title}
						</Text>
						<Text size="xs" c="dimmed">
							{fmtElapsed(now - recording.startedAt)}
						</Text>
					</div>
				</Group>
				<Button size="xs" variant="outline" color="red" onClick={onStop}>
					Stop
				</Button>
			</Group>
		</Box>
	);
}

function StatusRow({ m }: { m: FinishedMeeting }) {
	const completeTranscription = useMeetingStore((s) => s.completeTranscription);
	const addTranscribing = useMeetingStore((s) => s.addTranscribing);

	if (m.status === "transcribing") {
		return (
			<Text size="sm" c="dimmed">
				Transcribing “{m.title}” on GB10…
			</Text>
		);
	}
	if (m.status === "offline") {
		const onRetry = async () => {
			addTranscribing({ id: m.id, meeting: m.meeting, title: m.title, wavPath: m.wavPath });
			try {
				const serverUrl = await tauriAPI.getServerUrl();
				const res = await fetch(`${serverUrl}/api/meeting/transcribe`, {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({ wav_path: m.wavPath, backend: "parakeet_remote" }),
				});
				const { job_id } = await res.json();
				const result = await pollJob(serverUrl, job_id);
				completeTranscription(m.id, result);
			} catch {
				completeTranscription(m.id, { status: "offline", segments: m.segments, speakers: [] });
			}
		};
		return (
			<Box
				style={{
					background: SURFACE_RAISED,
					border: `1px solid ${ATTENTION}55`,
					borderRadius: 8,
					padding: "10px 12px",
				}}
			>
				<Group justify="space-between" wrap="nowrap">
					<Text size="xs" c="dimmed" style={{ flex: 1 }}>
						<Text span c={ATTENTION} inherit fw={600}>
							GB10 offline
						</Text>{" "}
						— “{m.title}” transcribed locally, no speaker labels.
					</Text>
					<Button size="xs" variant="subtle" color="yellow" onClick={onRetry}>
						Retry
					</Button>
				</Group>
			</Box>
		);
	}
	return null;
}

// One shared audio element so only one sample plays at a time.
let samplePlayer: HTMLAudioElement | null = null;
let sampleStopTimer: ReturnType<typeof setTimeout> | null = null;

function playSample(wavPath: string, startMs: number, durMs = 6000) {
	if (sampleStopTimer) clearTimeout(sampleStopTimer);
	if (!samplePlayer) samplePlayer = new Audio();
	samplePlayer.src = convertFileSrc(wavPath);
	samplePlayer.currentTime = startMs / 1000;
	samplePlayer.play().catch(() => {
		notifications.show({
			title: "Couldn't play sample",
			message: "Recording not available for playback.",
			color: "gray",
		});
	});
	sampleStopTimer = setTimeout(() => samplePlayer?.pause(), durMs);
}

function MappingPanel({ m }: { m: FinishedMeeting }) {
	const saveSpeakerNames = useMeetingStore((s) => s.saveSpeakerNames);
	return (
		<Box
			style={{
				background: SURFACE_RAISED,
				border: `1px solid ${BORDER}`,
				borderRadius: 8,
				padding: 14,
			}}
		>
			<Group gap={6} mb="xs">
				<span style={{ width: 6, height: 6, borderRadius: 3, background: ATTENTION }} />
				<Text size="sm" fw={600}>
					{m.title} — who's who?
				</Text>
			</Group>
			<SpeakerMapping
				meeting={m.meeting}
				segments={m.segments}
				onPlaySample={(startMs) => playSample(m.wavPath, startMs)}
				onSave={(names) => {
					saveSpeakerNames(m.id, names);
					notifications.show({
						title: "Names saved",
						message: `${Object.keys(names).length} speakers labeled`,
						color: "gray",
					});
				}}
			/>
		</Box>
	);
}

export function MeetingsSection() {
	const recording = useMeetingStore((s) => s.recording);
	const finished = useMeetingStore((s) => s.finished);
	const selectedMappingId = useMeetingStore((s) => s.selectedMappingId);
	const active = finished.filter((f) => f.status === "transcribing" || f.status === "offline");

	// The meeting whose mapping panel to show: explicit selection, else freshest unmapped.
	const target = selectedMappingId
		? finished.find((f) => f.id === selectedMappingId) ?? null
		: finished.find((f) => f.needsMapping) ?? null;

	// Drive the menubar attention dot (amber): on when any meeting needs names.
	const needAttention = finished.some((f) => f.needsMapping);
	useEffect(() => {
		invoke("set_menubar_attention", { on: needAttention }).catch(() => {});
	}, [needAttention]);

	if (!recording && !target && active.length === 0) return null;

	return (
		<Stack gap="sm" mb="md">
			<RecordingBanner />
			{target && <MappingPanel m={target} />}
			{active.map((m) => (
				<StatusRow key={m.id} m={m} />
			))}
		</Stack>
	);
}

/** Durable list of finished meetings this session, with re-entry to mapping. */
export function TranscriptsList() {
	const finished = useMeetingStore((s) => s.finished);
	const selectForMapping = useMeetingStore((s) => s.selectForMapping);
	const done = finished.filter((f) => f.status === "done" || f.status === "offline");

	if (done.length === 0) return null;

	return (
		<div className="dash-card">
			<p className="dash-card-title">Transcripts</p>
			<Stack gap={0}>
				{done.map((m) => {
					const named = Object.keys(m.speakerNames).length;
					return (
						<Group
							key={m.id}
							justify="space-between"
							wrap="nowrap"
							py={6}
							style={{ borderBottom: `1px solid ${BORDER}` }}
						>
							<Box style={{ minWidth: 0 }}>
								<Text size="sm" fw={600} lineClamp={1}>
									{m.title}
								</Text>
								<Text size="xs" c="dimmed">
									{m.status === "offline"
										? "Local transcript · no speaker labels"
										: m.needsMapping
											? `${m.speakers.length} speakers · unnamed`
											: `${named} speakers named`}
								</Text>
							</Box>
							{m.status === "done" && (
								<Button
									size="xs"
									variant="subtle"
									color={m.needsMapping ? "yellow" : "gray"}
									onClick={() => selectForMapping(m.id)}
								>
									{m.needsMapping ? "Name speakers" : "Edit names"}
								</Button>
							)}
						</Group>
					);
				})}
			</Stack>
		</div>
	);
}
