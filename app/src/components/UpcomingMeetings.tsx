/**
 * "Up next" — the dashboard's calendar surface.
 *
 * Polls the EventKit helper (via the get_upcoming_meetings command) and renders
 * compact meeting rows: time-until, title, ALL attendee names (DESIGN.md: no
 * "+N others" truncation), and a Record affordance that starts system-audio
 * capture for that meeting.
 */

import { Box, Group, Stack, Text, UnstyledButton } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { invoke } from "@tauri-apps/api/core";
import { Calendar, CalendarOff, Circle } from "lucide-react";
import { useEffect, useState } from "react";
import type { Meeting } from "../lib/events";
import {
	getUpcomingMeetings,
	type UpcomingMeetings as Upcoming,
} from "../hooks/useMeetingNotifications";
import { useMeetingStore } from "../stores/meetingStore";

const BORDER = "#2C2E33";
const REFRESH_MS = 60_000;

function timeUntil(startEpoch: number): string {
	const mins = Math.round((startEpoch * 1000 - Date.now()) / 60000);
	if (mins <= 0) return "now";
	if (mins < 60) return `in ${mins} min`;
	const h = Math.floor(mins / 60);
	const m = mins % 60;
	return m ? `in ${h}h ${m}m` : `in ${h}h`;
}

function clockTime(startEpoch: number): string {
	return new Date(startEpoch * 1000).toLocaleTimeString([], {
		hour: "numeric",
		minute: "2-digit",
	});
}

function attendeeNames(m: Meeting): string {
	const names = m.attendees.filter((a) => !a.isCurrentUser).map((a) => a.name);
	return names.join(", ");
}

function MeetingRow({ m }: { m: Meeting }) {
	const recording = useMeetingStore((s) => s.recording);
	const startRecording = useMeetingStore((s) => s.startRecording);
	const isRecordingThis = recording?.meeting?.id === m.id;

	const onRecord = async () => {
		if (recording) return; // one capture at a time
		try {
			const path = await invoke<string>("start_meeting_capture");
			startRecording(m, path, Date.now());
		} catch (e) {
			notifications.show({
				title: "Couldn't start recording",
				message: String(e),
				color: "gray",
			});
		}
	};

	const names = attendeeNames(m);
	return (
		<Group align="flex-start" wrap="nowrap" gap="sm" py={6}>
			<Calendar size={16} style={{ marginTop: 2, opacity: 0.5, flex: "0 0 auto" }} />
			<Box style={{ flex: 1, minWidth: 0 }}>
				<Text size="xs" c="dimmed">
					{timeUntil(m.startEpoch)} · {clockTime(m.startEpoch)}
				</Text>
				<Text size="sm" fw={600} lineClamp={1}>
					{m.title}
				</Text>
				{names && (
					<Text size="xs" c="dimmed">
						{names}
					</Text>
				)}
			</Box>
			<UnstyledButton
				onClick={onRecord}
				disabled={!!recording}
				aria-label={`Record ${m.title}`}
				style={{ flex: "0 0 auto", opacity: recording ? 0.4 : 1 }}
			>
				<Group gap={4} wrap="nowrap">
					<Circle size={12} fill={isRecordingThis ? "#FA5252" : "none"} color="#FA5252" />
					<Text size="xs" c="dimmed">
						{isRecordingThis ? "Recording" : "Record"}
					</Text>
				</Group>
			</UnstyledButton>
		</Group>
	);
}

export function UpcomingMeetings() {
	const [data, setData] = useState<Upcoming | null>(null);

	useEffect(() => {
		let cancelled = false;
		const load = async () => {
			try {
				const res = await getUpcomingMeetings();
				if (!cancelled) setData(res);
			} catch {
				if (!cancelled) setData({ authorized: false, meetings: [], error: "unavailable" });
			}
		};
		load();
		const id = setInterval(load, REFRESH_MS);
		return () => {
			cancelled = true;
			clearInterval(id);
		};
	}, []);

	// While loading, render nothing (avoids a flash); the dashboard has other rows.
	if (!data) return null;

	if (!data.authorized) {
		return (
			<div className="dash-card">
				<p className="dash-card-title">Up next</p>
				<div className="dash-empty">
					<CalendarOff size={22} className="dash-empty-icon" />
					<Text size="sm" fw={600}>
						Calendar not connected
					</Text>
					<Text size="xs" c="dimmed">
						Grant Calendar access so Tambourine can record your meetings.
					</Text>
				</div>
			</div>
		);
	}

	// Don't show meetings already in the past.
	const upcoming = data.meetings.filter((m) => m.endEpoch * 1000 > Date.now()).slice(0, 4);

	return (
		<div className="dash-card">
			<p className="dash-card-title">Up next</p>
			{upcoming.length === 0 ? (
				<div className="dash-empty">
					<Calendar size={22} className="dash-empty-icon" />
					<Text size="sm" fw={600}>
						No meetings scheduled
					</Text>
					<Text size="xs" c="dimmed">
						Tambourine starts recording 5 min before each one.
					</Text>
				</div>
			) : (
				<Stack gap={0}>
					{upcoming.map((m, i) => (
						<Box
							key={m.id}
							style={i > 0 ? { borderTop: `1px solid ${BORDER}` } : undefined}
						>
							<MeetingRow m={m} />
						</Box>
					))}
				</Stack>
			)}
		</div>
	);
}
