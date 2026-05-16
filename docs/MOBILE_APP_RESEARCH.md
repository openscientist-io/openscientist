# Mobile App Research

The right question is not "can we get OpenScientist onto a phone" but "what
does a scientist do with OpenScientist when they aren't at their desk?"
Most of the desk-bound version maps cleanly onto a phone, and most of that
mapping is boring. The interesting part is everything that becomes possible
*because* the device is a phone — the camera, the microphone, the
geolocation, the share sheet, the lock-screen glance, the watch on the
wrist, the always-on push pipe — and what those unlock for the way science
actually gets done. This document explores that in scenes, and then talks
about what it would take to build the best possible version: a native
iOS-first client (with iPad, Apple Watch, and Vision Pro targets), with
the FastAPI surface extended to feed it.

## Vignettes

### A soil sample at the edge of a burn scar

A field ecologist is two kilometers up a fire road in Colorado, kneeling
beside a soil core she just pulled. The phone is in her pocket. She opens
OpenScientist, taps the floating "Capture" button, photographs the core
from three angles, and dictates: "Top ten centimeters charcoal-black, no
visible mycelial mat, smells faintly sweet — different from last week's
unburned control at site B." The app stamps the photos with EXIF
geolocation, altitude, and a timestamp, transcribes the voice memo
on-device, and stages it all as an attachment to her active job, which
has been running for three days and is partway through an iteration
investigating post-fire microbial succession. When she gets back to a
cell signal it syncs in the background. The agent picks the observations
up on its next iteration and uses them to narrow its hypothesis about
fungal-bacterial ratios — something it could not have done from the CSV
of metagenomic counts alone.

None of this requires the user to think about "uploading evidence to a
job." It just feels like *making an observation*, the way scientists
already do, except the observation is now legible to the agent.

### The gel that wasn't supposed to look like that

A bench scientist runs a Western blot at 6pm, takes it out of the
developer at 7, and is staring at a band that's twice as intense as it
should be in lane 4. He pulls out his phone, snaps the gel under the
lightbox, and says: "GAPDH loading control looks fine, but the target
band in the LPS-stimulated condition is way brighter than the predicted
fold change from the RNA-seq. Either translation is being dramatically
upregulated post-transcriptionally, or there's a loading error I'm not
seeing." The agent — which has been working on his transcriptomics
analysis for the last hour and a half — receives the image, runs Vision
framework densitometry locally to estimate band intensities, and adds
"post-transcriptional regulation" to the hypothesis tracker as a
candidate explanation for the discrepancy. By the time he gets home,
the agent has searched PubMed for known post-transcriptional regulators
of his target and queued a follow-up analysis.

The unlock here is not the photo. It's the *moment of observation*. He
would never have walked back to his desk, opened a browser, navigated
to the job page, and uploaded a JPG — by the time he did, he'd have
forgotten the verbal hypothesis. The phone collapses that distance to
zero.

### The push notification on the bus

A computational biologist is on the 41 going home. Her phone buzzes.
The Dynamic Island shows a small molecular flask icon and a line of
text: "Iteration 7 needs your input — should I prioritize the
metabolomics or the proteomics signal?" She long-presses, the Live
Activity expands, and she sees a one-paragraph summary of why the agent
is stuck: two pathways are converging on the same gene set, and it
can't decide which lens to take first. She taps "metabolomics, but
flag the proteomics overlap" — voice would also have worked — and the
job resumes. She closes the app before her stop.

This is what `AWAITING_FEEDBACK` *should* feel like. The state exists in
the schema today; the ntfy notification already fires; but at the
moment, the only place a user can act on it is back at a laptop. The
"co-investigator" mode is being designed for a workflow that no current
client supports.

### A poster at a conference

She's at ASBMB. A poster two booths over makes her stop. The author has
stepped away. She opens her camera, frames the title and abstract, and
shares it directly into OpenScientist. The app OCRs it on-device,
recognizes the structure of a scientific abstract, and offers: "Start a
new job seeded with this work?" — pre-filling a research question along
the lines of "Replicate or extend the finding from <author>, <year>:
that <conclusion>, using <dataset>." She edits one phrase, attaches
her own competing dataset from Files (which is a folder synced from
the lab's S3 bucket), and starts the job before the poster's author
has come back. By the time she's at the next session, the agent is
two iterations deep.

The share sheet, the on-device OCR, and the Files integration are all
things a browser cannot do. Each one individually is small; together
they shorten the path from "this is interesting" to "the agent is on
it" from a 20-minute desk session to 90 seconds standing in an aisle.

### A glance during a one-on-one

A PI is in a one-on-one with her postdoc. She doesn't want to pull out
her phone, but she glances at her Apple Watch and sees the
complication: three jobs running, one needs attention, two completed
overnight, the latest report has a confidence score of 0.82. That's
all she needs to know that her morning isn't on fire and she can stay
present in the meeting.

A scientist's relationship with a long-running agent is fundamentally
ambient. They want it in their peripheral vision, not in a tab they
have to remember to refresh. The desktop UI is the wrong shape for this
— it demands an active foreground session. The watch and the lock
screen are the right shape.

### N-of-1 in the wild

A neurologist with an autoimmune condition wants to know whether her
flare-ups correlate with sleep architecture, HRV, or both. She points
OpenScientist at her own HealthKit data — six years of Apple Watch
sleep, heart rate, HRV, and the symptom journal she keeps in a third-
party app that exports to Health. The job runs across that
longitudinal record, with the data never leaving her device until a
summary is uploaded with her consent. She is the patient, the
investigator, and the cohort.

This is a use case that *only exists* in the mobile world. You can't
do it from a browser because the data isn't reachable from a browser.
HealthKit is a private, on-device store. The agent has to come to the
data, not the other way around.

### Vision Pro: walking through a structure

A structural biologist's overnight Phenix job finishes a model
comparison. She picks up her Vision Pro, puts it on, and walks around
the superposed structure at 1:1 scale in her living room. The agent's
findings appear as floating annotations attached to the relevant
residues: "Loop displacement 3.2Å, hypothesis: stabilizes the ATP
binding pocket — see iteration 4." She pinches a residue, asks "what
did the comparison say about this side chain," and the agent's chat
opens with the context pre-loaded.

This is the most speculative vignette, but it's the one where the
mobile (or rather post-mobile) platform genuinely does something a
laptop physically cannot: render a protein at the scale and parallax
your visual cortex evolved to parse.

## What changes about the agent itself

Worth saying out loud: a phone client is not a window onto the same
agent. It changes what the agent is. Today, the agent's perception of
the world is "the user uploaded these CSVs and typed this question."
With a phone in the loop, the agent's perception expands to "the user
is at a bench, just photographed a gel, and verbally hypothesized X,"
or "the user is in a field site at this elevation, has logged six
soil cores in the last hour, and described two of them as 'unusual.'"

This is qualitatively different evidence. It's situated, time-stamped,
sensory, and partial — the way real scientific observation is. The
agent that gets this kind of input can do things the agent without it
cannot, in roughly the same way that a postdoc who hears the PI mutter
"that doesn't look right" while walking past their bench does things
that a postdoc reading the same data from home cannot.

The mobile app is, in this sense, less a client and more a sensor
package.

## Building the best possible version

If we're not optimizing for cost, the answer is native, Apple-first,
multi-target.

### The app

A SwiftUI iOS app that ships against the iPhone, iPad, and Apple Watch
simultaneously, with a Vision Pro target as a Phase 3 deliverable.
SwiftUI handles all three in one codebase well enough that the cost
delta over "iPhone only" is not as large as it used to be, especially
with an army of developers.

Concretely:

- **iPhone target.** The primary client. Tabbed navigation (Jobs,
  Capture, Chat, You). The "Capture" tab is the home for the camera,
  voice memo, and share-sheet flow — designed so the most common
  action when opening the app while away from a desk is a single tap.
  PDF report viewing uses PDFKit; chat uses a streaming response
  rendered into a TextKit 2 view. Pull-to-refresh is a backup; the
  primary live-update path is SSE.

- **iPad target.** Split view with the report on one side and a chat
  / hypothesis tracker on the other. Apple Pencil for marking up the
  PDF; PencilKit ink becomes annotations attached to the finding,
  which the agent can read on the next iteration ("the user circled
  this passage and wrote 'check this'"). Stage Manager friendly.

- **Apple Watch target.** Complications (status glance + last
  finding's confidence). Notification actions for thumbs-up/down on
  findings and quick replies to "awaiting feedback" prompts. No data
  entry beyond voice; this is a glance/ack surface.

- **Vision Pro target (Phase 3).** Primary use case is reviewing
  structural-biology reports in 3D, with findings as anchored
  spatial annotations. Secondary use case is a "wall of jobs" — all
  active jobs as floating cards, each showing a live iteration
  graph. Speculative; revisit after iPhone v1 lands.

### Apple platform integrations to lean on

- **App Intents.** Every meaningful action — "start a new job,"
  "ask the agent about my last finding," "what did iteration 7
  conclude" — is exposed as an App Intent. Siri can invoke them.
  Shortcuts can chain them. Apple Intelligence's contextual
  awareness can surface them. Spotlight indexes them.

- **WidgetKit.** Small (status of one job), medium (last finding
  with a sparkline of iteration progress), large (the running
  hypotheses panel). Lock-screen and Standby-mode widgets too.

- **ActivityKit / Live Activities.** A running job is a Live
  Activity. Dynamic Island shows current iteration; long-press
  expands to the agent's current action; tap goes to chat. This
  is the single most "this is what mobile is for" feature on the
  list.

- **Vision framework.** On-device OCR for paper/poster capture,
  document detection, barcode scanning for sample IDs, and
  rudimentary densitometry on gel images.

- **Speech framework.** On-device transcription for voice memos
  and dictated research questions. No round trip to the server
  for transcription cost or latency.

- **HealthKit.** Read-only authorization with granular scopes. The
  agent never sees raw data; it sees the summaries the user
  approves to upload.

- **CoreLocation + SensorKit.** Geotagging for field observations.
  SensorKit gives access to ambient light, motion, and orientation
  for studies where that's actual signal.

- **FileProvider.** Native pickers for iCloud Drive, Google Drive,
  Dropbox, S3-backed providers, OneDrive, lab-internal NAS via
  WebDAV. Solves the "my data is too big to upload via UI" problem
  for the common case.

- **PushKit + APNs.** Replaces ntfy.sh for native push. Critical
  alerts allowed for "job needs feedback" so it can break through
  Focus modes the user has opted in. We keep ntfy.sh as a fallback
  for Android and web.

- **AVFoundation.** Camera with manual control for the bench-photo
  workflow (gels under lightboxes need exposure that auto-mode
  gets wrong). Voice memo recording with on-device transcription
  in parallel.

- **CoreML + Vision.** Small on-device models for "is this a
  reasonable gel image / is the lighting okay / is the focus
  acceptable" before we waste bandwidth uploading a blurry photo.

- **FaceTime SharePlay.** Two PIs review the same report together
  on a video call, with synchronized scrolling and shared
  annotations. Real workflow for collaborative grant writing.

- **AirDrop.** Findings, reports, and job links are first-class
  Transferable types so they show up in the system share sheet
  with AirDrop targets.

### What the backend has to grow to support this

A native client is a forcing function for an honest REST surface. The
shape of the work, roughly:

- A real streaming channel — Server-Sent Events on
  `/api/v1/jobs/{id}/events` — that emits iteration starts,
  findings, status transitions, and a heartbeat. SSE survives
  mobile network transitions better than WebSocket and is dead
  simple on the iOS side via URLSession.

- Chat as a first-class API: `POST /api/v1/jobs/{id}/chat` for
  user turns, with the response streamed back on the same SSE
  channel as the timeline so the client gets one ordered feed.
  `GET /api/v1/jobs/{id}/chat` for paginated history.

- Mid-run feedback: `POST /api/v1/jobs/{id}/feedback` to satisfy
  the `AWAITING_FEEDBACK` state and resume the job with user
  input.

- A general attachments endpoint:
  `POST /api/v1/jobs/{id}/attachments` accepting images, audio,
  arbitrary blobs, with a `kind` tag (observation, gel, poster,
  voice-memo, sample-photo) and a metadata payload (geolocation,
  timestamp, on-device transcript). The agent's iteration loop
  reads new attachments on each pass.

- OAuth with PKCE that issues a long-lived API key on successful
  device login, bound to a device identifier. Today's flow —
  generate a key in the web UI, copy-paste into the app — is
  obviously wrong for phone-first.

- Device registration: `POST /api/v1/devices` to bind an APNs
  token (and Watch complication push token) to a session, so the
  server can wake the right device.

- Lightweight list projections: `GET /api/v1/jobs` should default
  to a `JobSummary` shape (id, title, status, iteration counters,
  updated_at, last-finding-headline) so scrolling 50 jobs costs
  kilobytes, not megabytes.

- Resumable uploads. Field captures might be 20MB videos or
  collections of 4K photos taken off the grid; the upload has to
  survive losing signal mid-transfer.

- Per-attachment retention controls (some users will have HIPAA
  or institutional concerns about uploaded gels, posters, or
  health data). At minimum a "delete this attachment" endpoint
  and a UI for it; ideally policy-bound expiry.

### A note on Android

If we're picking one platform for best-possible, iOS wins. The
target user (academic and industry scientists) skews heavily
iPhone/Mac, the platform integrations are more developed (Live
Activities, App Intents, HealthKit, Vision Pro), and the
deployment story is faster (TestFlight beats Play Internal
Testing for clinical-academic beta cycles). Android is a Phase
3 conversation, ideally as a Kotlin Multiplatform sibling rather
than a Flutter or React Native compromise — if we built it native
on iOS, building it native on Android preserves the bar.

### Rough timeline with abundant resources

- **Months 1–2.** Backend hardening (SSE, chat-over-REST,
  feedback RPC, attachments, OAuth-PKCE, device registration).
  This is also the work that satisfies the Python SDK request
  and benefits the web UI.
- **Months 2–4.** iPhone v1 in TestFlight. Jobs list, job
  detail with streaming timeline, chat, push, camera + voice
  capture, share-sheet ingest, Files picker integration.
- **Months 4–5.** Live Activities, Widgets, App Intents,
  Apple Watch companion.
- **Months 5–6.** iPad-native layout with Pencil. HealthKit
  integration with a pilot N-of-1 user.
- **Months 6+.** Vision Pro target. Android via KMP.

## What this is not

It's not a port of the web UI. The web UI is the right shape for
"a user sits down for 45 minutes to launch a job and read a
report." The mobile app is the right shape for "a scientist
lives their day, occasionally observes something, occasionally
gets pinged, occasionally glances at progress." Those are
different products that happen to share a backend. The mobile
one is the one that puts an autonomous research agent in the
peripheral vision of a working scientist — which is, ultimately,
where you want it.
