/**
 * Meeting feature state for the menubar dropdown.
 *
 * Holds the live recording session, in-flight transcription jobs, and finished
 * meetings awaiting speaker mapping. Drives the persistent recording banner and
 * the interaction states defined in DESIGN.md (recording / transcribing / done /
 * GB10-offline / partial).
 */

import { create } from "zustand";
import type { Meeting } from "../lib/events";
import type { DiarizedSegment } from "../components/SpeakerMapping";

export type TranscriptionStatus =
	| "transcribing" // running on GB10
	| "done" // completed with speaker labels
	| "offline" // GB10 unreachable; transcribed locally without diarization
	| "error";

export interface RecordingSession {
	meeting: Meeting | null;
	/** epoch ms when capture started, for the elapsed timer */
	startedAt: number;
	/** path to the WAV being written */
	wavPath: string;
}

export interface FinishedMeeting {
	id: string;
	meeting: Meeting | null;
	title: string;
	wavPath: string;
	status: TranscriptionStatus;
	segments: DiarizedSegment[];
	speakers: string[];
	/** label -> attendee name, once the user maps them */
	speakerNames: Record<string, string>;
	/** true until the user has mapped the detected speakers */
	needsMapping: boolean;
	finishedAt: number;
}

interface MeetingState {
	recording: RecordingSession | null;
	finished: FinishedMeeting[];
	/** Meeting the user explicitly chose to (re)label, overriding the auto-pick. */
	selectedMappingId: string | null;

	startRecording: (meeting: Meeting | null, wavPath: string, startedAt: number) => void;
	stopRecording: () => void;

	/** A meeting finished and is now transcribing. */
	addTranscribing: (m: { id: string; meeting: Meeting | null; title: string; wavPath: string }) => void;
	/** Transcription completed (with or without diarization). */
	completeTranscription: (
		id: string,
		result: { status: TranscriptionStatus; segments: DiarizedSegment[]; speakers: string[] },
	) => void;
	saveSpeakerNames: (id: string, names: Record<string, string>) => void;

	/** Choose a meeting to (re)label; null reverts to the auto-pick. */
	selectForMapping: (id: string | null) => void;
	/** The meeting whose mapping panel should show: explicit selection, else the
	 * most-recent meeting still needing names. */
	mappingTarget: () => FinishedMeeting | null;
	/** Count of finished meetings still awaiting speaker names. */
	attentionCount: () => number;
}

export const useMeetingStore = create<MeetingState>((set, get) => ({
	recording: null,
	finished: [],
	selectedMappingId: null,

	startRecording: (meeting, wavPath, startedAt) =>
		set({ recording: { meeting, wavPath, startedAt } }),

	stopRecording: () => set({ recording: null }),

	addTranscribing: ({ id, meeting, title, wavPath }) =>
		set((s) => ({
			finished: [
				{
					id,
					meeting,
					title,
					wavPath,
					status: "transcribing",
					segments: [],
					speakers: [],
					speakerNames: {},
					needsMapping: false,
					finishedAt: 0,
				},
				...s.finished.filter((f) => f.id !== id),
			],
		})),

	completeTranscription: (id, result) =>
		set((s) => ({
			finished: s.finished.map((f) =>
				f.id === id
					? {
							...f,
							status: result.status,
							segments: result.segments,
							speakers: result.speakers,
							// Only ask for names when diarization actually produced speakers.
							needsMapping: result.status === "done" && result.speakers.length > 0,
							finishedAt: f.finishedAt || 0,
						}
					: f,
			),
		})),

	saveSpeakerNames: (id, names) =>
		set((s) => ({
			finished: s.finished.map((f) =>
				f.id === id ? { ...f, speakerNames: names, needsMapping: false } : f,
			),
			selectedMappingId: s.selectedMappingId === id ? null : s.selectedMappingId,
		})),

	selectForMapping: (id) => set({ selectedMappingId: id }),

	mappingTarget: () => {
		const { finished, selectedMappingId } = get();
		if (selectedMappingId) {
			return finished.find((f) => f.id === selectedMappingId) ?? null;
		}
		return finished.find((f) => f.needsMapping) ?? null;
	},

	attentionCount: () => get().finished.filter((f) => f.needsMapping).length,
}));
