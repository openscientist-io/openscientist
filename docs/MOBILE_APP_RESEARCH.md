# Mobile App Research

Scope: what would it take to ship an OpenScientist mobile app, and what does
"mobile" specifically unlock that the current web UI can't?

## TL;DR

A mobile companion is a much smaller lift than a full mobile-first rewrite,
because the existing FastAPI surface (`src/openscientist/api/endpoints/`) and
ntfy.sh push pipeline already cover the boring 80%. The interesting question
is not "can we render the existing pages on a phone" — NiceGUI is already
responsive-ish — but "what would a phone-native client let scientists do
that they cannot do today?" Candidates: in-field photo capture of samples /
gels / instruments, voice-dictated research questions and observations, share
sheet ingest of PDFs/papers, glanceable job status (widgets, watch app), and
mid-run feedback while away from the desk. The right shape is probably an
**Expo / React Native client** consuming the REST API, with one new SSE
endpoint for live timeline and one new multimodal upload endpoint.

## What's already in place

- **REST API at `/api/v1`** with bearer-token auth (`src/openscientist/api/`):
  jobs CRUD, status, cancel, report download, artifacts ZIP, shares, skills,
  API keys, health.
- **API key issuance flow** (`pages/api_keys.py`, `endpoints/keys.py`): plaintext
  shown once, hashed at rest.
- **OAuth providers**: GitHub, Google, ORCID, plus mock for dev
  (`src/openscientist/auth/`).
- **Push notifications via ntfy.sh** (`src/openscientist/ntfy.py`, `User.ntfy_topic`):
  job started / completed / failed / cancelled / awaiting-feedback all
  already wired; users get a personal topic they subscribe to.
- **`AWAITING_FEEDBACK` job state** and `coinvestigate` investigation mode
  (`src/openscientist/job/types.py`) — the data model can already park a job
  waiting on a human.
- **Job chat** (`src/openscientist/job_chat.py`) — conversational follow-up
  exists, but only as an internal service consumed by the NiceGUI page, not
  exposed via REST.
- **File upload** on `POST /jobs` — multipart, data-files-only today; no
  image/audio path.

## Existing issues that a mobile app would partially or fully unlock

| # | Issue | Mobile angle |
|---|-------|-------------|
| #4 | Conversational support during/after analysis | Phone chat is the natural shape; the "branch on a finding while away from desk" UX wants a mobile thread, not a browser tab. |
| #15, #113 | Thumbs up/down feedback on report items | One-tap rating is mobile-native; doomed to under-use behind a desktop click. |
| #22 | MOTD banner | Trivially becomes a push notification. |
| #34 | Track and display data files submitted by users | Camera roll / share-sheet ingest fits the same model. |
| #42 | Long-query readability | Mobile forces a collapsible/segmented redesign that probably helps desktop too. |
| #45 | Real-time streaming of agent progress | A mobile app that doesn't stream feels broken; this becomes a hard requirement, not a nice-to-have. |
| #55 | Support API calls (Jupyter, etc.) | Mobile is "yet another API consumer"; whatever we build for one helps the other. |
| #84 | Download artifacts | Native share sheet → Files / iCloud / Drive. |
| #86 | Publish to Zenodo/GitHub | Share-sheet flow on mobile is the obvious UI. |
| #91 | Reviewer agent | "Give me critical feedback on this finding" as a voice prompt while commuting. |

No existing issue is *blocked* on mobile, but several would land better in
that medium.

## Mobile-specific unlocks (what desktop can't do)

Grouped by what the phone has that a browser tab doesn't.

### Camera

- **In-field environmental sampling.** Soil/water/plant photos with EXIF
  geotag + timestamp, attached to a job as evidence. The agent can already
  reason about images via multimodal models; the gap is the upload pipeline.
- **Lab bench capture.** Western blots, gels, plates, instrument screens,
  Coomassie stains, microscopy snapshots — photograph rather than retype.
  Especially useful for capturing instrument readouts that don't have a
  digital export path.
- **Document / paper capture.** Snap a printed paper or a poster at a
  conference, OCR + summarize into a finding or a new job seed.
- **QR / barcode → sample ID.** Scan a tube/plate barcode to attach data
  to the right job.

### Voice

- **Voice-dictated research questions.** Lower friction than typing on a
  phone keyboard; particularly relevant for the "broad, open-ended query"
  framing #99 is asking for.
- **Voice memos as findings.** "Hypothesis: the upregulation of GENE_X in
  cluster 3 might be a stress response artifact, check the heatshock pathway"
  — transcribe and post into the job's chat / hypothesis tracker (#70).
- **Hands-free lab walk-through.** Siri-style "OpenScientist, what did the
  last iteration find?" while gloved up at a bench.

### Geolocation + sensors

- **Field studies.** Tag observations with lat/lng, altitude, accelerometer
  (e.g. depth/orientation for sample collection). Could feed environmental
  metadata directly into a job's knowledge state.
- **HealthKit / Google Fit / SensorKit.** For N-of-1 biomedical questions
  (sleep, HRV, glucose if user has a CGM) — a research question can pull
  the user's own longitudinal data as input.

### Background + glanceable

- **Lock-screen widgets / Live Activities** showing job iteration progress.
  Jobs run for tens of minutes to hours; this is exactly the shape iOS
  Live Activities and Android Glance were built for.
- **Apple Watch / Wear OS app**: status glance, push acknowledgment,
  thumbs-up/down on findings.
- **Background sync**: keep last N reports on-device so you can read them
  on a plane.

### OS integrations

- **Share sheet ingest.** "Share to OpenScientist" from Safari/Mail/Slack
  to seed a new job with a paper PDF or dataset link. Covers #65 ("ignore
  these sources") as the inverse: "include this source".
- **Files / Drive / iCloud / Dropbox pickers.** Solves #33 (S3/GCS data
  retrieval) for the common case of "the data is in my Drive already".
- **Apple Shortcuts / Tasker.** "Every Monday at 9am, summarize jobs that
  completed over the weekend." Trivial to expose since the API already
  exists.
- **Native push notifications.** Already half-built via ntfy.sh; could
  either keep ntfy and surface its push or move to APNs/FCM tokens bound
  to the user record.

### Mid-run interaction

- The `AWAITING_FEEDBACK` state + ntfy `notify_job_awaiting_feedback`
  already exist. A mobile app makes this *usable* — the user gets a push,
  taps the notification, lands on a feedback screen, types/dictates a
  reply, the job resumes. On desktop, the user is usually closed out of
  the tab when this happens.

## Backend gap analysis

What the existing REST surface lacks for a viable mobile client:

1. **Real-time progress stream.** Today the web UI relies on NiceGUI's
   internal websocket; mobile needs an explicit SSE or WebSocket endpoint.
   This is issue #45 and would land on `/api/v1/jobs/{id}/stream` or
   similar. SSE is cheaper to implement and survives mobile network
   transitions better than WS.
2. **Multimodal upload endpoint.** `POST /jobs` accepts data files; needs
   either a generalized `attachments[]` field or a separate
   `POST /jobs/{id}/attachments` for images / audio / OCR'd documents
   added *after* a job exists. The job's knowledge-state already takes
   arbitrary blobs.
3. **Chat-over-REST.** `job_chat` is internal — needs a thin
   `POST /jobs/{id}/chat` + paginated `GET /jobs/{id}/chat/messages` and
   ideally streamed responses on the same SSE channel.
4. **Mid-run feedback RPC.** `AWAITING_FEEDBACK` exists in the schema but
   has no public endpoint to *provide* the feedback and resume. Needs
   `POST /jobs/{id}/feedback`.
5. **Mobile-friendly auth.** OAuth-with-PKCE → automatic API key issuance,
   so the user doesn't have to copy-paste a `name:secret` string from the
   web UI into a mobile app. Today's flow is unusable on a phone.
6. **Device registration for push.** If we move off ntfy.sh to APNs/FCM,
   a `POST /devices` endpoint that binds an APNs/FCM token to a session.
   If we stay on ntfy.sh, no backend change needed but the app must
   subscribe to the user's topic.
7. **Voice transcription.** Either client-side (iOS Speech / Android
   SpeechRecognizer — free, on-device, no backend cost) or server-side
   via a new `POST /transcribe` endpoint. On-device is the right default.
8. **Lightweight list payloads.** `GET /jobs` returns the full
   `JobResponse`; mobile lists want a smaller `JobSummary` projection
   (id, title, status, iter/max-iter, updated_at) so a 50-job scroll
   doesn't move megabytes.

## Implementation approaches

Three serious options, ranked by total cost-to-value.

### Option A — PWA on top of the existing NiceGUI app (cheapest)

Add a service worker + web manifest to the existing app. Pin a few
mobile-friendly pages (jobs list, job detail, chat, push acknowledge).
Browser-side `getUserMedia` covers camera and microphone. Web Push
covers notifications on Android; on iOS, web push works since 16.4 but
is gimped (no badges, weaker reliability).

- ✅ Smallest engineering investment; reuses the existing UI.
- ✅ No app store review cycle.
- ❌ No share sheet on iOS. No Live Activities. No widgets. No
  HealthKit. Weak background behavior. Camera/voice UX feels webby,
  not native.
- ❌ The "real unlocks" above (widgets, share sheet, watch, sensors,
  shortcuts) are off the table.

**When this is right:** if mobile is purely a "view my running jobs on the
go" use case and we don't care about the field/lab/voice unlocks.

### Option B — Expo + React Native client against the REST API (recommended)

A new repo (or `mobile/` directory) that consumes `/api/v1`. Expo handles
push, camera, file system, share intent, share sheet, voice (via
`expo-speech` / native speech APIs), background fetch, and most of the
sensor APIs. Watch and widgets are reachable via Expo modules but require
some native code.

- ✅ Single codebase for iOS + Android.
- ✅ Unlocks camera, voice, share sheet, push, background — i.e.
  everything in the "Mobile-specific unlocks" section above except
  Live Activities and Watch (which need extra native modules but are
  reachable).
- ✅ Forces the backend gaps in §"Backend gap analysis" to get filled,
  which benefits all API consumers (#55).
- ❌ New stack for the team (TS + RN) if everyone is Python-native.
- ❌ App store review cycle, signing certs, TestFlight, Play Console.
- ❌ ~2–4 months of FTE for a credible v1.

**When this is right:** if we want the unlocks and are willing to staff
it. This is the default recommendation.

### Option C — Fully native (Swift + Kotlin)

Two codebases, best fidelity on each platform, full access to Live
Activities, Watch, complications, widgets, App Intents, HealthKit.

- ✅ Best possible UX, especially for sensor-heavy / watch-heavy
  workflows.
- ❌ Roughly 2× the engineering cost of Option B for marginal gain
  unless we're sensor-heavy.

**When this is right:** if the field/sensor/watch story becomes the
primary product thesis.

## Suggested phased roadmap

1. **Phase 0 — API hardening (no mobile client yet).**
   - Add SSE stream endpoint (#45).
   - Add `/jobs/{id}/chat` and `/jobs/{id}/feedback` endpoints.
   - Add OAuth-PKCE → API-key issuance flow.
   - Add `JobSummary` projection on `GET /jobs`.
   - Outcome: improves the web UI, unblocks the Python SDK request in
     #55, and is the prerequisite for any mobile shape.

2. **Phase 1 — PWA polish (1–2 weeks).**
   - Service worker + manifest. Validates whether mobile traffic
     materializes at all before sinking real money into a native app.
   - Web Push subscription parallel to ntfy.sh.

3. **Phase 2 — Expo client v1 (2–3 months).**
   - Auth (OAuth-PKCE), job list/detail, chat, push, cancel, report
     viewer (PDF), feedback-while-awaiting flow.
   - Camera attachment upload to a job.
   - Voice-to-text research-question entry (on-device).

4. **Phase 3 — Mobile-native unlocks (incremental).**
   - Share-sheet ingest (paper → new job).
   - Lock-screen widget / Live Activity for running job.
   - Watch app (status glance + thumbs).
   - Shortcuts / App Intents.
   - HealthKit / SensorKit for N-of-1 biomedical questions, if a
     willing pilot user wants this.

## Open questions for the team

- Who is the target user — bench scientist (camera/voice/lab matters),
  computational biologist (probably fine with the web UI), or
  field researcher (geolocation/sensors matter most)? Different
  answers change which Phase-3 items move up.
- Do we want to stay on ntfy.sh for push, or move to APNs/FCM? ntfy
  is operationally simpler but loses badge counts and the iOS
  notification-extension surface.
- App-store distribution vs. enterprise/TestFlight-only? Affects review
  surface for the "agent that runs arbitrary code on uploaded data"
  description.
- Do we want a single repo with a `mobile/` directory, or a separate
  `openscientist-mobile` repo? Single-repo means easier API-contract
  coupling; separate means independent release cadence.
