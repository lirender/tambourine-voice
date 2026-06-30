// EventKit calendar helper for Tambourine.
//
// Reads upcoming meetings (with attendees) from the macOS Calendar (which
// aggregates Google / iCloud / Exchange accounts) and prints them as JSON.
// The Rust side shells out to this and uses the result to drive pre-meeting
// prep (5 min before) and post-meeting "end session?" prompts.
//
// Build: swiftc -O calendar_helper.swift -o calendar_helper
// Usage: calendar_helper [--hours N]   (default: next 12 hours)
//        calendar_helper --request     (trigger the TCC permission prompt)

import EventKit
import Foundation

struct AttendeeJSON: Codable {
    let name: String
    let email: String?
    let isOrganizer: Bool
    let isCurrentUser: Bool
}

struct MeetingJSON: Codable {
    let id: String
    let title: String
    let start: String       // ISO-8601
    let end: String         // ISO-8601
    let startEpoch: Double
    let endEpoch: Double
    let location: String?
    let notes: String?
    let organizer: String?
    let attendees: [AttendeeJSON]
}

struct OutputJSON: Codable {
    let authorized: Bool
    let meetings: [MeetingJSON]
    let error: String?
}

let iso: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

func emit(_ out: OutputJSON) -> Never {
    let enc = JSONEncoder()
    enc.outputFormatting = [.prettyPrinted, .sortedKeys]
    if let data = try? enc.encode(out), let s = String(data: data, encoding: .utf8) {
        print(s)
    }
    exit(out.error == nil ? 0 : 1)
}

func attendees(of event: EKEvent) -> [AttendeeJSON] {
    guard let participants = event.attendees else { return [] }
    return participants.map { p in
        // EKParticipant.url is "mailto:foo@bar.com"; name may be nil.
        let urlStr = p.url.absoluteString
        let email = urlStr.hasPrefix("mailto:") ? String(urlStr.dropFirst("mailto:".count)) : nil
        return AttendeeJSON(
            name: p.name ?? email ?? "Unknown",
            email: email,
            isOrganizer: p.participantRole == .chair,
            isCurrentUser: p.isCurrentUser
        )
    }
}

func listMeetings(store: EKEventStore, hours: Double) {
    let now = Date()
    let end = now.addingTimeInterval(hours * 3600)
    let predicate = store.predicateForEvents(withStart: now.addingTimeInterval(-3600),
                                             end: end, calendars: nil)
    let events = store.events(matching: predicate)

    let meetings: [MeetingJSON] = events.compactMap { ev in
        // Skip all-day blocks and events with no other people.
        if ev.isAllDay { return nil }
        let att = attendees(of: ev)
        return MeetingJSON(
            id: ev.eventIdentifier ?? UUID().uuidString,
            title: ev.title ?? "(no title)",
            start: iso.string(from: ev.startDate),
            end: iso.string(from: ev.endDate),
            startEpoch: ev.startDate.timeIntervalSince1970,
            endEpoch: ev.endDate.timeIntervalSince1970,
            location: ev.location,
            notes: ev.notes,
            organizer: ev.organizer?.name,
            attendees: att
        )
    }
    .sorted { $0.startEpoch < $1.startEpoch }

    emit(OutputJSON(authorized: true, meetings: meetings, error: nil))
}

// ---- main ----
let args = CommandLine.arguments
var hours = 12.0
if let i = args.firstIndex(of: "--hours"), i + 1 < args.count, let h = Double(args[i + 1]) {
    hours = h
}
let requestOnly = args.contains("--request")

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
var grantError: Error?

let handler: (Bool, Error?) -> Void = { ok, err in
    granted = ok
    grantError = err
    sem.signal()
}

if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents(completion: handler)
} else {
    store.requestAccess(to: .event, completion: handler)
}
sem.wait()

if let e = grantError {
    emit(OutputJSON(authorized: false, meetings: [], error: e.localizedDescription))
}
if !granted {
    emit(OutputJSON(authorized: false, meetings: [], error: "Calendar access not granted"))
}
if requestOnly {
    emit(OutputJSON(authorized: true, meetings: [], error: nil))
}
listMeetings(store: store, hours: hours)
