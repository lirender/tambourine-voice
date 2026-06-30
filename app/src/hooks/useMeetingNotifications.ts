/**
 * Listens for calendar-driven meeting events from the Rust backend and surfaces
 * pre-meeting prep + post-meeting "end session?" prompts.
 *
 * - `meeting-prep` (~5 min before): notify the user, list attendees, and offer
 *   to start recording.
 * - `meeting-ended`: prompt to end the recording session and kick off
 *   transcription + diarization.
 *
 * Event names/payloads mirror src-tauri/src/calendar/mod.rs.
 */

import { notifications } from "@mantine/notifications";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { useEffect } from "react";
import {
	AppEvents,
	type Meeting,
	type MeetingEventPayload,
} from "../lib/events";

export interface UpcomingMeetings {
	authorized: boolean;
	meetings: Meeting[];
	error: string | null;
}

export async function getUpcomingMeetings(): Promise<UpcomingMeetings> {
	return invoke<UpcomingMeetings>("get_upcoming_meetings");
}

export async function requestCalendarAccess(): Promise<boolean> {
	return invoke<boolean>("request_calendar_access");
}

function attendeeNames(meeting: Meeting): string {
	const names = meeting.attendees
		.filter((a) => !a.isCurrentUser)
		.map((a) => a.name);
	if (names.length === 0) return "no other attendees";
	if (names.length <= 4) return names.join(", ");
	return `${names.slice(0, 4).join(", ")} +${names.length - 4} more`;
}

type MeetingHandlers = {
	/** Called when the user accepts the pre-meeting prep (e.g. start recording). */
	onStartRecording?: (meeting: Meeting) => void;
	/** Called when the user confirms ending the session after a meeting. */
	onEndSession?: (meeting: Meeting) => void;
};

export function useMeetingNotifications(handlers: MeetingHandlers = {}) {
	useEffect(() => {
		const unlistens: Array<() => void> = [];

		listen<MeetingEventPayload>(AppEvents.meetingPrep, (event) => {
			const { meeting, seconds_until_start } = event.payload;
			const mins = Math.max(0, Math.round(seconds_until_start / 60));
			notifications.show({
				id: `prep-${meeting.id}`,
				title: `Starting in ${mins} min: ${meeting.title}`,
				message: `With ${attendeeNames(meeting)}. Click to start recording.`,
				color: "blue",
				autoClose: 60_000,
				onClick: () => handlers.onStartRecording?.(meeting),
			});
		}).then((u) => unlistens.push(u));

		listen<MeetingEventPayload>(AppEvents.meetingEnded, (event) => {
			const { meeting } = event.payload;
			notifications.show({
				id: `ended-${meeting.id}`,
				title: `Meeting ended: ${meeting.title}`,
				message: "Click to end the recording session and transcribe.",
				color: "orange",
				autoClose: 15000,
				onClick: () => handlers.onEndSession?.(meeting),
			});
		}).then((u) => unlistens.push(u));

		return () => {
			for (const u of unlistens) u();
		};
	}, [handlers.onStartRecording, handlers.onEndSession]);
}
