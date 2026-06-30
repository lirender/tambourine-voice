/**
 * Post-meeting UX: map diarized speaker labels (speaker_0, speaker_1, …) to the
 * real attendees from the calendar event.
 *
 * Each detected speaker shows a representative snippet (and a play button to
 * hear a sample from the recording), plus a dropdown of the meeting's attendees.
 * Saving applies the chosen names to the transcript.
 */

import { Button, Group, Select, Stack, Text } from "@mantine/core";
import { Play } from "lucide-react";
import { useMemo, useState } from "react";
import type { Meeting } from "../lib/events";

export interface DiarizedSegment {
	start_ms: number;
	end_ms: number;
	text: string;
	speaker: string | null;
}

interface SpeakerInfo {
	label: string;
	sample: string;
	firstStartMs: number;
	totalMs: number;
}

function summarizeSpeakers(segments: DiarizedSegment[]): SpeakerInfo[] {
	const byLabel = new Map<string, SpeakerInfo>();
	for (const seg of segments) {
		const label = seg.speaker ?? "unknown";
		const existing = byLabel.get(label);
		const dur = seg.end_ms - seg.start_ms;
		if (!existing) {
			byLabel.set(label, {
				label,
				sample: seg.text,
				firstStartMs: seg.start_ms,
				totalMs: dur,
			});
		} else {
			existing.totalMs += dur;
			// Prefer a longer, more representative sample.
			if (seg.text.length > existing.sample.length) existing.sample = seg.text;
		}
	}
	// Most-talkative first.
	return [...byLabel.values()].sort((a, b) => b.totalMs - a.totalMs);
}

function defaultLabel(label: string): string {
	return label.replace("speaker_", "Speaker ");
}

interface SpeakerMappingProps {
	meeting: Meeting | null;
	segments: DiarizedSegment[];
	/** Play a sample of the recording starting at the given offset (ms). */
	onPlaySample?: (startMs: number) => void;
	/** Persist the chosen label→name mapping. */
	onSave: (mapping: Record<string, string>) => void;
}

export function SpeakerMapping({
	meeting,
	segments,
	onPlaySample,
	onSave,
}: SpeakerMappingProps) {
	const speakers = useMemo(() => summarizeSpeakers(segments), [segments]);
	const [mapping, setMapping] = useState<Record<string, string>>({});

	const attendeeOptions = useMemo(() => {
		const opts =
			meeting?.attendees.map((a) => ({ value: a.name, label: a.name })) ?? [];
		return opts;
	}, [meeting]);

	if (speakers.length === 0) {
		return <Text size="sm" c="dimmed">No speakers detected in this recording.</Text>;
	}

	return (
		<Stack gap="md">
			<Text fw={600} size="sm">
				Who's who? Map each voice to an attendee.
			</Text>
			{speakers.map((sp) => (
				<Group key={sp.label} align="flex-start" wrap="nowrap" gap="sm">
					<Button
						variant="light"
						size="xs"
						px={8}
						onClick={() => onPlaySample?.(sp.firstStartMs)}
						aria-label={`Play sample for ${sp.label}`}
					>
						<Play size={14} />
					</Button>
					<Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
						<Text size="xs" c="dimmed" lineClamp={2}>
							“{sp.sample}”
						</Text>
						<Select
							size="xs"
							placeholder={defaultLabel(sp.label)}
							data={attendeeOptions}
							searchable
							clearable
							value={mapping[sp.label] ?? null}
							onChange={(value) =>
								setMapping((prev) => {
									const next = { ...prev };
									if (value) next[sp.label] = value;
									else delete next[sp.label];
									return next;
								})
							}
							nothingFoundMessage="No matching attendee"
						/>
					</Stack>
				</Group>
			))}
			<Group justify="flex-end">
				<Button size="xs" onClick={() => onSave(mapping)}>
					Save names
				</Button>
			</Group>
		</Stack>
	);
}
